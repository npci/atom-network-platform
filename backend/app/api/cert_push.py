# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Push a change's test cases to the cert-agent environment.

Single endpoint — POST /api/changes/{change_id}/cert-push — reads the
test-case JSON for this change and forwards it to cert-agent's LLM
import endpoint.

Lookup order:
  1. Engine-produced cert-simulator-contract JSON
       <ARTIFACTS_DIR>/excel_engine/workbooks/**/<*.json>
     where the file's `change_request_id` matches.
  2. Synthesised contract from the change's `cert_test_cases` Product Kit
     document — the markdown table is parsed into minimal `test_cases[]`
     stubs that cert-agent's LLM enriches into its flat payload schema.

Either path produces the same shape:
    { feature_name, change_request_id, test_cases[] }
which is POSTed to http://cert-agent:8000/api/llm-agent/upload-test-cases-json.

Cert-agent streams SSE; we parse the final `complete` event and return
`{created, skipped, errors, total}` plus `feature_name` so the UI can
deep-link to the per-flow tab on the simulator UI.

When neither lookup succeeds the endpoint returns 412 with a hint to
run the engine or seed the cert_test_cases product-kit doc — which is
exactly what `seed_phase_a.py` already does.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import DbDep, CurrentUser, authenticate_ws
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.error_taxonomy import client_safe_message
from app.models.change_request import ChangeRequest
from app.models.product_kit import ProductKitDocument, ProductKitDocType


logger = logging.getLogger(__name__)
router = APIRouter(tags=["cert-push"])


# Cert-agent base URL — sourced from `settings.cert_agent_url` so deployments
# can swap the docker-network hostname for a host-IP / FQDN when cert-agent
# runs outside the compose network (e.g. Ubuntu native install on the host).
# Default keeps the docker-network hostname for back-compat. Read at call
# time (not module-import time) so env overrides during test/restart pick up
# without needing a fresh import.
def _cert_agent_url() -> str:
    from app.core.config import settings
    return settings.cert_agent_url.rstrip("/")


# ── Markdown table → test-case stubs ──────────────────────────────────────────

def _parse_md_table(content: str) -> list[dict]:
    """Parse a `| TC ID | Scenario | Expected |` markdown table.

    Returns a list of minimal TestCaseStub-shaped dicts. Cert-agent's
    LLM fills in api_type / role / test_data from these, so we only need
    test_id / scenario / expected_status / response_code / coverage_tag
    + the `rendered` description+steps blocks.

    Robust to:
      - extra columns we don't recognise (ignored)
      - alignment dividers (---:|:---: rows)
      - prose text between table sections
    """
    out: list[dict] = []
    headers: list[str] = []
    for ln in content.splitlines():
        stripped = ln.strip()
        if not stripped.startswith("|"):
            headers = []  # reset between sections
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not headers:
            headers = [c.lower() for c in cells]
            has_id_col       = any(h in headers for h in ("tc id", "id", "test id"))
            has_scenario_col = any(h in headers for h in ("scenario", "description", "test case"))
            if not (has_id_col and has_scenario_col):
                headers = []
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # alignment divider
        if not headers:
            continue  # the most recent header line was a non-TC table — skip its data

        row = dict(zip(headers, cells + [""] * (len(headers) - len(cells))))
        tid = row.get("tc id") or row.get("id") or row.get("test id") or f"TC-{len(out) + 1}"
        scenario = row.get("scenario") or row.get("description") or row.get("test case") or ""
        expected = row.get("expected") or row.get("result") or row.get("outcome") or ""
        # Mixed-init source support: operators add an "Initiated By" /
        # "Initiator" column to their cert_test_cases markdown to mark
        # which side fires each TC. When the column is absent the row
        # emits an empty string and the cert-push endpoint's
        # default_initiated_by query param takes over.
        init_raw = (
            row.get("initiated by") or row.get("initiator")
            or row.get("initiated") or row.get("trigger") or ""
        ).strip().upper()
        if init_raw in ("BANK", "B"):
            initiated_by = "BANK"
        elif init_raw in ("NPCI", "N"):
            initiated_by = "NPCI"
        else:
            initiated_by = ""

        # Pull a network response code out of the expected cell — operator
        # convention is "RC=00", "RC: U03", or "Response code U03".
        m = re.search(r"(?:RC|response\s*code|code)[:=\s-]+([A-Z0-9]+)", expected, re.I)
        rc = m.group(1) if m else ""

        ex_low = expected.lower()
        if "deemed" in ex_low or "auto-release" in ex_low or "auto release" in ex_low:
            status = "Deemed"
        elif "partial" in ex_low and "captur" in ex_low:
            status = "Partial"
        elif rc and rc.upper() != "00" or any(
            kw in ex_low for kw in ("reject", "decline", "fail", "denied")
        ):
            status = "Failure"
        else:
            status = "Success"

        coverage = (
            "deemed" if status == "Deemed"
            else "partial" if status == "Partial"
            else "decline" if status == "Failure"
            else "happy_path"
        )

        out.append({
            "test_id": tid,
            "apis": [],
            "api_type": "Pay",
            "entities": ["Payer PSP", "Payee PSP"],
            "approval_type": "Non-Pre Approved",
            "payer_handle": "VPA",
            "payee_handle": "VPA",
            "scenario_summary": scenario,
            "expected_status": status,
            "response_code": rc.upper() if rc else "",
            "coverage_tag": coverage,
            "scope": "v2.0",
            "txn_initiated_by": (
                "Bank" if initiated_by == "BANK"
                else "NPCI" if initiated_by == "NPCI"
                else ""
            ),
            "psp_as": "Payer",
            # Canonical field forwarded to cert-agent. Empty when the
            # markdown table didn't carry the column — cert-agent applies
            # the per-upload default_initiated_by query param to fill it.
            "initiated_by": initiated_by,
            "rendered": {
                "test_id": tid,
                "details_block": "",
                "description_block": scenario,
                "steps_block": expected,
            },
        })
    return out


def _normalise_initiated_by(cases: list[dict]) -> None:
    """In-place: ensure every row has `initiated_by` set when the source
    used the older `txn_initiated_by` field. Cert-agent's LLM prompt
    only looks at `initiated_by` (plus a small set of common aliases) —
    explicit normalisation here keeps engine-produced artifacts and
    md-parsed stubs on the same wire shape.
    """
    for c in cases:
        if c.get("initiated_by"):
            continue
        raw = str(c.get("txn_initiated_by") or "").strip().upper()
        if raw in ("BANK", "B"):
            c["initiated_by"] = "BANK"
        elif raw in ("NPCI", "N"):
            c["initiated_by"] = "NPCI"
        # else leave empty — cert-agent applies the upload default


# ── Engine artifact lookup ────────────────────────────────────────────────────

def _find_engine_artifact(change_id: str) -> dict | None:
    """Scan the excel-engine output dirs for a JSON whose
    `change_request_id` matches. Returns the parsed contract dict, or None.
    """
    base = Path(settings.artifacts_dir or "/app/artifacts") / "excel_engine"
    candidates: list[Path] = []
    for sub in ("workbooks", "artifacts"):
        d = base / sub
        if d.exists():
            candidates.extend(d.rglob("*.json"))

    # Newest first. rglob yields filesystem order, so after a regenerate the
    # scan could return the change's OLD artifact (stale/partial cases) just
    # because its filename sorts earlier. The most recent run must win.
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for jp in candidates:
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("cert_push.skip_bad_json path=%s err=%r", jp, exc)
            continue
        if (
            isinstance(data, dict)
            and data.get("change_request_id") == change_id
            and isinstance(data.get("test_cases"), list)
            and data["test_cases"]
        ):
            logger.info("cert_push.found_artifact change=%s path=%s tcs=%d",
                        change_id, jp, len(data["test_cases"]))
            return data
    return None


# ── Artifact resolution (shared by REST + WS) ─────────────────────────────────

def _resolve_artifact_or_raise(
    change_id: str, cr: ChangeRequest, db: Session,
) -> tuple[dict, str]:
    """Return (artifact, source) for cert-push.

    Tries the engine JSON first, falls back to synthesizing from the
    cert_test_cases Product Kit doc. Raises HTTPException with the same
    status codes the REST endpoint used to return inline — the WS handler
    catches and converts the detail to an error frame.
    """
    artifact = _find_engine_artifact(change_id)
    if artifact:
        _normalise_initiated_by(artifact.get("test_cases", []))
        return artifact, "engine"

    cert_doc = db.scalars(
        select(ProductKitDocument)
        .where(
            ProductKitDocument.change_request_id == change_id,
            ProductKitDocument.doc_type == ProductKitDocType.CERT_TEST_CASES,
        )
        .order_by(ProductKitDocument.version.desc())
    ).first()
    if not cert_doc or not (cert_doc.content or "").strip():
        raise HTTPException(
            status_code=412,
            detail=(
                "No test cases available for this change. Run the test "
                "case engine, or seed a cert_test_cases Product Kit "
                "document."
            ),
        )
    cases = _parse_md_table(cert_doc.content)
    if not cases:
        raise HTTPException(
            status_code=422,
            detail=(
                "cert_test_cases doc exists but no markdown table rows "
                "could be parsed. Expected a `| TC ID | Scenario | "
                "Expected |` table."
            ),
        )
    _normalise_initiated_by(cases)
    return {
        "feature_name": cr.title or "Unnamed Change",
        "change_request_id": change_id,
        "test_cases": cases,
    }, "product_kit"


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/changes/{change_id}/cert-push")
async def push_to_cert(
    change_id: str,
    db: DbDep,
    _: CurrentUser,
    default_initiated_by: str = "NPCI",
):
    """Push this change's test cases to the cert-agent environment.

    Query params:
      default_initiated_by  "NPCI" (default) or "BANK". Applied by cert-agent
                            to rows whose source didn't carry an Initiated By
                            column. Per-row values always win.

    Returns:
        {
          feature_name, change_request_id, source ('engine'|'product_kit'),
          test_cases_sent, cert_agent_status,
          created, skipped, errors, total,
          flow,                           # cert-agent flow code (UI tab key)
          simulator_url                   # deep link to the per-flow tab
        }

    Legacy blocking REST endpoint — kept for back-compat. The new
    streaming WebSocket at /ws/changes/{change_id}/cert-push is the
    recommended path for any non-trivial test-case count (the 180s cap
    here trips at ~80 TCs).
    """
    cr = db.get(ChangeRequest, change_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change not found")

    artifact, source = _resolve_artifact_or_raise(change_id, cr, db)

    # Forward to cert-agent. Cert-agent streams SSE; we slurp the
    # full response (LLM mapping + N test-case POSTs) so the client
    # gets a single JSON summary. 180s timeout is comfortable headroom
    # for an LLM call + per-TC POSTs even on slow networks.
    cert_agent_url = _cert_agent_url()
    di = (default_initiated_by or "NPCI").strip().upper()
    if di not in ("NPCI", "BANK"):
        di = "NPCI"
    try:
        _headers = {"X-Internal-Token": settings.cert_agent_internal_token}
        async with httpx.AsyncClient(timeout=180.0, headers=_headers) as http:
            resp = await http.post(
                f"{cert_agent_url}/api/llm-agent/upload-test-cases-json"
                f"?default_initiated_by={di}",
                json=artifact,
            )
    except httpx.HTTPError as exc:
        # SCR #6: the previous message interpolated BOTH the internal
        # cert-agent URL (infrastructure topology) and the raw httpx error
        # (which can carry resolved hostnames, ports and TLS internals) into a
        # response body. Operators get all of it from the log line; the caller
        # gets the fact that the upstream is unreachable, which is the only
        # part they can act on.
        logger.warning(
            "cert-agent unreachable: url=%s error_type=%s error=%s",
            cert_agent_url, type(exc).__name__, exc,
        )
        raise HTTPException(
            status_code=502,
            detail="cert-agent is unreachable",
        )

    summary = {
        "created": 0, "skipped": 0, "errors": 0, "total": 0,
        "tc_results": [], "flow": "",
        "initiated_by_counts": {"NPCI": 0, "BANK": 0},
        "created_counts":      {"NPCI": 0, "BANK": 0},
    }
    if resp.status_code == 200:
        for line in resp.text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                evt = json.loads(line[5:].strip())
            except Exception:  # noqa: BLE001
                continue
            etype = evt.get("type")
            if etype == "flow_check":
                summary["flow"] = evt.get("flow_name", "")
            elif etype == "analysis":
                ib = evt.get("initiated_by_counts") or {}
                summary["initiated_by_counts"] = {
                    "NPCI": int(ib.get("NPCI", 0)),
                    "BANK": int(ib.get("BANK", 0)),
                }
            elif etype == "tc_progress":
                summary["tc_results"].append({
                    "tc_id": evt.get("tc_id"),
                    "status": evt.get("status"),
                    "code": evt.get("code"),
                    "message": evt.get("message"),
                    "initiated_by": evt.get("initiated_by"),
                })
            elif etype == "complete":
                for k in ("created", "skipped", "errors", "total"):
                    summary[k] = int(evt.get(k, 0))
                cc = evt.get("created_counts") or {}
                summary["created_counts"] = {
                    "NPCI": int(cc.get("NPCI", 0)),
                    "BANK": int(cc.get("BANK", 0)),
                }

    feature_name = artifact["feature_name"]
    flow = summary.get("flow") or re.sub(r"[^A-Z0-9]+", "_", feature_name.upper()).strip("_")

    # Slice 4 enabler: stamp every assignment for this change with the
    # cert-agent feature + flow that holds the pushed TCs. The
    # orchestrator later reads this back when the partner declares
    # readiness so it knows which feature to query in cert-agent.
    _persist_feature_mapping(db, change_id, feature_name, flow)

    logger.info(
        "cert_push change=%s source=%s feature=%r flow=%s sent=%d "
        "created=%d skipped=%d errors=%d npci=%d bank=%d",
        change_id, source, feature_name, flow,
        len(artifact["test_cases"]),
        summary["created"], summary["skipped"], summary["errors"],
        summary["created_counts"]["NPCI"], summary["created_counts"]["BANK"],
    )

    return {
        "feature_name":         feature_name,
        "change_request_id":    change_id,
        "source":               source,
        "test_cases_sent":      len(artifact["test_cases"]),
        "cert_agent_status":    resp.status_code,
        "default_initiated_by": di,
        "flow":                 flow,
        "simulator_url":        f"http://localhost:5173/test-cases?flow={flow}",
        **{k: summary[k] for k in (
            "created", "skipped", "errors", "total", "tc_results",
            "initiated_by_counts", "created_counts",
        )},
    }


def _persist_feature_mapping(
    db: Session, change_id: str, feature_name: str, flow: str,
) -> None:
    """Update every ChangePartnerAssignment for this change so its
    `acceptance_meta` carries the cert-agent feature + flow code.

    `acceptance_meta` is the existing JSON column on the assignment;
    we extend it rather than add a new column to keep the migration
    surface zero. The Slice 4 orchestrator reads
    `acceptance_meta['cert_agent_feature_name']` to scope its TC query.
    """
    try:
        from app.models.phase_c import ChangePartnerAssignment
    except Exception:  # noqa: BLE001
        return  # defensive — never block the push response on a model import quirk
    rows = db.scalars(
        select(ChangePartnerAssignment)
        .where(ChangePartnerAssignment.change_request_id == change_id)
    ).all()
    if not rows:
        return
    for row in rows:
        meta = dict(row.acceptance_meta or {})
        meta["cert_agent_feature_name"] = feature_name
        meta["cert_agent_flow"]         = flow
        row.acceptance_meta = meta
    db.commit()


# ── WebSocket: streaming cert-push ────────────────────────────────────────────
#
# Replaces the 180-s blocking POST above. Three nested timeouts (browser
# axios, this backend's httpx, the orchestrator's httpx) all expired
# simultaneously once a change had >80 test cases, because cert-agent
# spends ~1.5–2 s per TC (LLM mapping + cert-agent create POST + Playwright
# warm-up). The WebSocket flow:
#
#   1. accepts a token frame, authenticates the user;
#   2. surfaces any in-flight cert_push job for this change so a
#      reconnecting client can pick up where it left off (uses the same
#      replay_request protocol as BRD / Canvas);
#   3. on "start", resolves the artifact, creates a durable agent_jobs
#      row, then opens an httpx stream to cert-agent's SSE endpoint
#      with **no read timeout** — cert-agent decides when it's done;
#   4. translates each SSE event into a ws chunk (mirrored into Redis
#      via job_registry.ws_send_chunk for replay) plus, where useful,
#      an explicit `{type:"progress"}` frame and an update to the job's
#      current_stage / progress_pct fields;
#   5. completes the job with the same result payload the REST endpoint
#      returns, so the frontend's `result.created / .skipped / .flow /
#      .simulator_url` rendering keeps working unchanged once it
#      migrates to the WS path.

_EVENT_ICON = {
    "status":     "•",
    "flow_check": "▸",
    "analysis":   "✎",
    "tc_progress": "→",
    "complete":   "✓",
    "error":      "✗",
}


def _format_event_for_chunk(evt: dict) -> str:
    """Render an SSE event as a single human-readable line for the
    progress log the UI shows. Kept terse — the structured payload also
    travels via the explicit progress frames below, so this is just
    for the operator's eye.
    """
    etype = evt.get("type", "")
    icon = _EVENT_ICON.get(etype, "·")
    if etype == "tc_progress":
        idx   = evt.get("index")
        total = evt.get("total")
        tcid  = evt.get("tc_id", "")
        st    = evt.get("status", "")
        msg   = evt.get("message", "")
        pos   = f"[{idx}/{total}] " if idx and total else ""
        return f"{icon} {pos}{tcid}: {st} — {msg}\n"
    if etype == "flow_check":
        return f"{icon} {evt.get('message', evt.get('flow_name', ''))}\n"
    if etype == "complete":
        return (
            f"{icon} complete: "
            f"{evt.get('created', 0)} created / "
            f"{evt.get('skipped', 0)} skipped / "
            f"{evt.get('errors', 0)} errors\n"
        )
    if etype == "error":
        return f"{icon} error: {evt.get('message', '')}\n"
    return f"{icon} {evt.get('message', etype)}\n"


@router.websocket("/ws/changes/{change_id}/cert-push")
async def ws_cert_push(websocket: WebSocket, change_id: str):
    # Default-initiated-by lives in the query string so reconnect+replay
    # keeps using the same value the caller picked at job start. Falls
    # back to the Authority if absent or unrecognised.
    di_raw = websocket.query_params.get("default_initiated_by", "NPCI") or "NPCI"
    default_initiated_by = di_raw.strip().upper()
    if default_initiated_by not in ("NPCI", "BANK"):
        default_initiated_by = "NPCI"
    await websocket.accept()
    logger.info("WS cert-push connected: change=%s", change_id)
    db: Session = SessionLocal()

    from app.services import job_registry

    try:
        auth_msg = await websocket.receive_text()
        token = json.loads(auth_msg).get("token", "")
        user = authenticate_ws(websocket, db, token)
        if not user:
            logger.warning("WS cert-push auth failed: change=%s", change_id)
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            return
        logger.info("WS cert-push auth ok: change=%s user=%s", change_id, user.username)

        cr = db.get(ChangeRequest, change_id)
        if not cr:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Not found"}))
            return

        active = job_registry.get_active_jobs(
            db, change_request_id=change_id, module="cert_push",
        )
        if active:
            await websocket.send_text(json.dumps({
                "type": "active_jobs", "jobs": active,
            }))

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data.get("type") == "replay_request":
                rep_job_id = (data.get("job_id") or "").strip()
                rep_since  = int(data.get("since_seq") or 0)
                if not rep_job_id:
                    continue
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await websocket.send_text(json.dumps({
                    "type": "replay", "job_id": rep_job_id, "since_seq": rep_since,
                    "chunks": [{"seq": s, "text": t} for (s, t) in chunks],
                    "count": len(chunks),
                }))
                continue

            cmd = (data.get("message") or "").strip().lower()
            if cmd != "start":
                continue

            try:
                artifact, source = _resolve_artifact_or_raise(change_id, cr, db)
            except HTTPException as exc:
                await websocket.send_text(json.dumps({
                    "type": "error", "detail": str(exc.detail),
                    "status": exc.status_code,
                }))
                continue

            tc_total = len(artifact["test_cases"])
            registry_job_id = job_registry.create_job(
                db,
                change_request_id=change_id,
                module="cert_push",
                subtype=source,
                started_by_user_id=user.id,
                metadata={
                    "tc_count":     tc_total,
                    "feature_name": artifact["feature_name"],
                },
            )
            await websocket.send_text(json.dumps({
                "type":    "job_id",
                "job_id":  registry_job_id,
                "module":  "cert_push",
                "subtype": source,
                "tc_total": tc_total,
            }))
            job_registry.update_job(
                db, registry_job_id,
                current_stage=f"Pushing {tc_total} test case(s) to cert-agent",
            )

            cert_agent_url = _cert_agent_url()
            summary = {
                "created": 0, "skipped": 0, "errors": 0, "total": 0,
                "tc_results": [], "flow": "",
                "initiated_by_counts": {"NPCI": 0, "BANK": 0},
                "created_counts":      {"NPCI": 0, "BANK": 0},
            }
            tc_done = 0

            # Connect quickly (so a dead cert-agent fails fast), but never
            # cap the read — the whole point of this rewrite is that
            # cert-agent decides when it's done.
            timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
            _headers = {"X-Internal-Token": settings.cert_agent_internal_token}

            try:
                async with httpx.AsyncClient(timeout=timeout, headers=_headers) as http:
                    async with http.stream(
                        "POST",
                        f"{cert_agent_url}/api/llm-agent/upload-test-cases-json"
                        f"?default_initiated_by={default_initiated_by}",
                        json=artifact,
                    ) as resp:
                        if resp.status_code != 200:
                            body_bytes = await resp.aread()
                            raise RuntimeError(
                                f"cert-agent HTTP {resp.status_code}: "
                                f"{body_bytes.decode('utf-8', 'replace')[:300]}"
                            )
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            try:
                                evt = json.loads(line[5:].strip())
                            except Exception:
                                continue

                            etype = evt.get("type")

                            await job_registry.ws_send_chunk(
                                websocket, registry_job_id,
                                _format_event_for_chunk(evt),
                            )

                            if etype == "flow_check":
                                summary["flow"] = evt.get("flow_name", "")
                                job_registry.update_job(
                                    db, registry_job_id,
                                    current_stage=f"Flow: {summary['flow']}",
                                )
                                await websocket.send_text(json.dumps({
                                    "type":      "progress",
                                    "job_id":    registry_job_id,
                                    "event":     "flow_check",
                                    "flow_name": summary["flow"],
                                    "exists":    bool(evt.get("exists")),
                                    "existing_count": int(evt.get("existing_count", 0)),
                                }))
                            elif etype == "status":
                                # Cert-agent emits a `status` event at every
                                # phase change — flow lookup, each LLM batch
                                # ("AI mapping batch N/M…"), validation, create.
                                # Forward it so the UI's stage line moves
                                # during the multi-minute LLM phase instead of
                                # appearing frozen on the last flow_check msg.
                                msg = evt.get("message", "")
                                if msg:
                                    job_registry.update_job(
                                        db, registry_job_id,
                                        current_stage=msg,
                                    )
                                    await websocket.send_text(json.dumps({
                                        "type":    "progress",
                                        "job_id":  registry_job_id,
                                        "event":   "status",
                                        "message": msg,
                                        "step":    evt.get("step"),
                                        "done":    bool(evt.get("done", False)),
                                    }))
                            elif etype == "analysis":
                                ib = evt.get("initiated_by_counts") or {}
                                summary["initiated_by_counts"] = {
                                    "NPCI": int(ib.get("NPCI", 0)),
                                    "BANK": int(ib.get("BANK", 0)),
                                }
                                await websocket.send_text(json.dumps({
                                    "type":    "progress",
                                    "job_id":  registry_job_id,
                                    "event":   "analysis",
                                    "tc_count": int(evt.get("tc_count", 0)),
                                    "initiated_by_counts": summary["initiated_by_counts"],
                                }))
                            elif etype == "tc_progress":
                                tc_done += 1
                                summary["tc_results"].append({
                                    "tc_id":  evt.get("tc_id"),
                                    "status": evt.get("status"),
                                    "code":   evt.get("code"),
                                    "message": evt.get("message"),
                                    "initiated_by": evt.get("initiated_by"),
                                })
                                evt_total = int(evt.get("total") or tc_total or 1)
                                pct = min(99, int(tc_done * 100 / max(1, evt_total)))
                                job_registry.update_job(
                                    db, registry_job_id,
                                    progress_pct=pct,
                                    current_stage=(
                                        f"{tc_done}/{evt_total} "
                                        f"{evt.get('tc_id','')} → {evt.get('status','')}"
                                    ),
                                )
                                await websocket.send_text(json.dumps({
                                    "type":   "progress",
                                    "job_id": registry_job_id,
                                    "event":  "tc_progress",
                                    "tc_id":  evt.get("tc_id"),
                                    "status": evt.get("status"),
                                    "code":   evt.get("code"),
                                    "message": evt.get("message"),
                                    "index":  evt.get("index"),
                                    "total":  evt.get("total"),
                                }))
                            elif etype == "complete":
                                for k in ("created", "skipped", "errors", "total"):
                                    summary[k] = int(evt.get(k, 0))
                                cc = evt.get("created_counts") or {}
                                summary["created_counts"] = {
                                    "NPCI": int(cc.get("NPCI", 0)),
                                    "BANK": int(cc.get("BANK", 0)),
                                }
                            elif etype == "error":
                                raise RuntimeError(evt.get("message") or "cert-agent error")
            except Exception as exc:
                logger.exception("WS cert-push failed: change=%s", change_id)
                job_registry.fail_job(
                    db, registry_job_id, error=str(exc),
                    final_stage="Push failed",
                )
                await websocket.send_text(json.dumps({
                    "type":   "error",
                    # Same scrub fail_job() applies three lines above. The
                    # WebSocket bypassed that chokepoint, so a psycopg2 error
                    # put the full `[SQL: ...]` statement in the browser.
                    "detail": client_safe_message(str(exc)),
                    "job_id": registry_job_id,
                }))
                continue

            feature_name = artifact["feature_name"]
            flow = (
                summary.get("flow")
                or re.sub(r"[^A-Z0-9]+", "_", feature_name.upper()).strip("_")
            )

            # Persist feature mapping for orchestrator (Slice 4)
            _persist_feature_mapping(db, change_id, feature_name, flow)

            result_payload = {
                "feature_name":         feature_name,
                "change_request_id":    change_id,
                "source":               source,
                "test_cases_sent":      tc_total,
                "default_initiated_by": default_initiated_by,
                "flow":                 flow,
                "simulator_url":        f"http://localhost:5173/test-cases?flow={flow}",
                **{k: summary[k] for k in (
                    "created", "skipped", "errors", "total", "tc_results",
                    "initiated_by_counts", "created_counts",
                )},
            }

            logger.info(
                "ws_cert_push change=%s source=%s feature=%r flow=%s sent=%d "
                "created=%d skipped=%d errors=%d npci=%d bank=%d",
                change_id, source, feature_name, flow,
                tc_total, summary["created"], summary["skipped"], summary["errors"],
                summary["created_counts"]["NPCI"], summary["created_counts"]["BANK"],
            )

            job_registry.complete_job(
                db, registry_job_id,
                result=result_payload,
                final_stage=(
                    f"Pushed {summary['created']} test case(s)"
                    + (f" ({summary['errors']} error)" if summary["errors"] else "")
                ),
            )

            await websocket.send_text(json.dumps({
                "type":   "done",
                "job_id": registry_job_id,
                **result_payload,
            }))

    except WebSocketDisconnect:
        logger.info("WS cert-push disconnected: change=%s", change_id)
    except Exception:
        logger.exception("WS cert-push error: change=%s", change_id)
        try:
            await websocket.send_text(json.dumps({
                "type": "error", "detail": "Internal error",
            }))
        except Exception:
            pass
    finally:
        db.close()
