# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin-only endpoints for syncing Phase A test cases into cert-agent's
`tc_store`.

POST /api/changes/{change_id}/cert-simulator/diff
POST /api/changes/{change_id}/cert-simulator/apply
GET  /api/changes/{change_id}/cert-simulator/log
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.deps import DbDep, AdminUser
from app.core.error_taxonomy import client_safe_detail
from app.models.cert_sync import CertSimulatorSyncLog
from app.services.tc_store_sync import (
    apply_diff, compute_diff, fetch_existing_subset, find_cert_engine_partner,
    parse_rendered_plan, register_flow_on_engine, write_diff_view_log,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cert-simulator-sync"])


# ── Schemas ───────────────────────────────────────────────────────────────

class _Decision(BaseModel):
    tc_id: str
    action: str          # "add" | "update" | "delete" | "skip"


class _FlowRegistration(BaseModel):
    """A new flow that the operator wants cert-agent to learn about before
    we apply this change's TCs. Submitted from the diff modal's 'Unknown flows'
    section — see SyncDiffModal.jsx."""
    flow_code:            str
    api_request:          str
    api_response:         str
    request_xml_template: str = ""
    simulator_endpoint:   str = "/execute"
    expected_resp_codes:  list[str] = ["00"]
    role:                 str = ""
    description:          str = ""


class _ApplyBody(BaseModel):
    decisions: list[_Decision] = []
    # New flows to register before applying TC decisions. Each is POSTed to
    # cert-agent's /api/flows; failures bubble up as 502s and the user retries.
    flow_registrations: list[_FlowRegistration] = []


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("/changes/{change_id}/cert-simulator/diff")
async def diff_test_cases(change_id: str, db: DbDep, user: AdminUser):
    """Compute the diff between Phase A's latest cert_test_cases output and
    cert-agent's current TCs tagged with subset=cr-{change_id_short}.

    Returns: {added, changed, removed, plan_count, existing_count, skipped_parse}.
    Writes a `diff_view` audit row.
    """
    cert_engine = find_cert_engine_partner(db)
    if not cert_engine:
        raise HTTPException(
            status_code=503,
            detail="No active cert_engine partner is registered. Register one in Admin → Partners.",
        )

    parsed = parse_rendered_plan(change_id, db, cert_engine=cert_engine)
    if not parsed.ok and not parsed.unknown_apis and parsed.skipped and parsed.skipped[0]["tc_id"] == "*":
        # No artifacts on disk for this change yet
        raise HTTPException(
            status_code=409,
            detail=parsed.skipped[0]["reason"],
        )

    existing = fetch_existing_subset(cert_engine, change_id)
    diff = compute_diff(parsed.ok, existing)

    unknown_apis_payload = [
        {"api": api, "tc_ids": tc_ids}
        for api, tc_ids in sorted(parsed.unknown_apis.items())
    ]

    # Phase 3b — LLM-draft unknown APIs that Phase A didn't pre-author.
    proposed_flow_defs = list(parsed.proposed_flow_defs)
    already_authored = {fd.get("api_request") for fd in proposed_flow_defs if fd.get("api_request")}
    for u in unknown_apis_payload:
        api = u["api"]
        if not api or api in already_authored:
            continue
        try:
            from app.services.xml_template_resolver import resolve_or_generate
            xml, source, _ = await resolve_or_generate(
                db=db,
                api_name=api,
                flow_code="".join(c for c in api.replace("Req", "", 1).upper() if c.isalnum()) or "CUSTOM",
                direction="",
                role="",
                description=f"Auto-detected from CR (used by {len(u['tc_ids'])} TC{'s' if len(u['tc_ids']) != 1 else ''}).",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("xml_template_generator failed for api=%s: %s", api, exc)
            continue
        if not xml or source is None:
            continue
        proposed_flow_defs.append({
            "api_request":  api,
            "api_response": "Resp" + api[3:] if api.startswith("Req") else f"Resp{api}",
            "flow_code":    "".join(c for c in api.replace("Req", "", 1).upper() if c.isalnum()) or "CUSTOM",
            "request_xml_template": xml,
            "simulator_endpoint": "/execute",
            "expected_resp_codes": ["00"],
            "role":          "",
            "description":   f"Auto-drafted by xml_template_generator (source={source}).",
            "source":        f"llm-draft ({source})",
            "confidence":    "llm-draft",
        })
    db.commit()

    summary = {
        "added":          len(diff.added),
        "changed":        len(diff.changed),
        "removed":        len(diff.removed),
        "plan_count":     len(parsed.ok),
        "existing_count": len(existing),
        "skipped_parse":  len(parsed.skipped),
        "unknown_apis":   len(unknown_apis_payload),
        "proposed_flow_defs": len(parsed.proposed_flow_defs),
    }
    write_diff_view_log(
        db,
        change_id=change_id,
        cert_engine=cert_engine,
        actor_user_id=user.id,
        summary=summary,
    )

    return {
        "added":          diff.added,
        "changed":        diff.changed,
        "removed":        diff.removed,
        "plan_count":     len(parsed.ok),
        "existing_count": len(existing),
        "skipped_parse":  parsed.skipped,
        "unknown_apis":   unknown_apis_payload,
        "proposed_flow_defs": proposed_flow_defs,
        "cert_engine_partner": {"id": cert_engine.id, "name": cert_engine.name, "endpoint_url": cert_engine.endpoint_url},
    }


@router.post("/changes/{change_id}/cert-simulator/apply")
def apply_test_cases(change_id: str, body: _ApplyBody, db: DbDep, user: AdminUser):
    """Execute the operator's per-row decisions against cert-agent's CRUD.

    Body: {decisions: [{tc_id, action: "add"|"update"|"delete"|"skip"}]}
    Writes an `apply` audit row that the Cert Status timeline reads as a
    `test_suite_registered` event.
    """
    cert_engine = find_cert_engine_partner(db)
    if not cert_engine:
        raise HTTPException(
            status_code=503,
            detail="No active cert_engine partner is registered.",
        )

    # Step 1: register any new flows the operator declared in the modal.
    # Done before re-parsing so those flows become known and the corresponding
    # previously-skipped TCs now resolve to a flow.
    flow_results: list[dict] = []
    registered_flow_codes: set[str] = set()
    for fr in body.flow_registrations:
        try:
            row = register_flow_on_engine(cert_engine, fr.model_dump())
            flow_results.append({"flow_code": row.get("flow_code"), "status": "ok"})
            if row.get("flow_code"):
                registered_flow_codes.add(row["flow_code"])
        except Exception as e:
            # SCR #6: `flow_results` is returned to the caller as
            # response["flow_registrations"]. `e` here is typically an httpx
            # transport failure against the cert-engine, whose text carries the
            # internal hostname, port and TLS detail. Full text is on the
            # logger.exception line above.
            logger.exception("flow registration failed: %s", fr.flow_code)
            flow_results.append({"flow_code": fr.flow_code, "status": "failed",
                                 "error": client_safe_detail(e)})

    # Step 2: re-parse so the apply uses the latest plan (avoids races where the
    # operator opened the diff modal a long time ago and the engine re-ran)
    # AND picks up the freshly-registered flows from step 1.
    parsed = parse_rendered_plan(change_id, db, cert_engine=cert_engine)
    if not parsed.ok and parsed.skipped and parsed.skipped[0]["tc_id"] == "*":
        raise HTTPException(status_code=409, detail=parsed.skipped[0]["reason"])

    parsed_by_id = {p.tc_id: p for p in parsed.ok}

    # Step 3: implicit-add for TCs that resolve to a flow we just registered but
    # weren't in the operator's decisions list. Reasoning: at /diff time the modal
    # showed those TCs only inside the unknown_apis section (no checkbox), since
    # they had no flow yet. The operator's act of registering the flow IS the
    # consent for those TCs to apply. Without this auto-promotion they'd silently
    # remain skipped — a confusing footgun.
    explicit_decisions = {d.tc_id: d.action for d in body.decisions}
    auto_added: list[str] = []
    if registered_flow_codes:
        for p in parsed.ok:
            if p.tc_id not in explicit_decisions and p.flow in registered_flow_codes:
                explicit_decisions[p.tc_id] = "add"
                auto_added.append(p.tc_id)

    result = apply_diff(
        cert_engine,
        change_id,
        parsed_by_id,
        [{"tc_id": tc_id, "action": action} for tc_id, action in explicit_decisions.items()],
        db,
        actor_user_id=user.id,
    )
    return {
        "applied":            result.applied,
        "skipped":            result.skipped,
        "failed":             result.failed,
        "log_id":             result.log_id,
        "subset":             f"cr-{change_id[:8]}",
        "flow_registrations": flow_results,
        # TCs whose flow was just registered → auto-applied. Surface for transparency.
        "auto_added":         auto_added,
    }


@router.get("/changes/{change_id}/cert-simulator/log")
def get_sync_log(change_id: str, db: DbDep, _: AdminUser, limit: int = 50):
    """Return the sync log rows (most recent first) for the 'Last synced'
    status line on the Phase A cert_test_cases page."""
    rows = db.scalars(
        select(CertSimulatorSyncLog)
        .where(CertSimulatorSyncLog.change_request_id == change_id)
        .order_by(CertSimulatorSyncLog.created_at.desc())
        .limit(limit)
    ).all()
    from app.models.user import User
    user_cache: dict[str, str] = {}
    def actor_name(uid):
        if not uid: return None
        if uid in user_cache: return user_cache[uid]
        u = db.get(User, uid)
        user_cache[uid] = u.username if u else uid
        return user_cache[uid]

    out = []
    for r in rows:
        out.append({
            "id":         r.id,
            "operation":  r.operation,
            "summary":    r.summary or {},
            "actor":      actor_name(r.actor_user_id),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    latest_apply = next((r for r in out if r["operation"] == "apply"), None)
    return {
        "rows": out,
        "latest_apply": latest_apply,
        "count": len(out),
    }
