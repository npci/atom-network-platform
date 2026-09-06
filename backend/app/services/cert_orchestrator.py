# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cert orchestrator — wired into the readiness-declaration handler.

Flow when partner declares ready with role + test_data:
  1. Resolve partner.cert_agent_bank_id (mapping NPCI partner → cert-agent
     bank short-code). Skip with structured warning if missing.
  2. Derive the cert-agent flow code from the change title (same algo
     as `app.api.cert_push`).
  3. GET cert-agent's test-case list, filter by flow + role-prefix
     (PR_/PE_/RE_/BE_) matching the declared role.
  4. PUT each matched test case with merged test_data (partner's
     fields overwrite, others preserved — keeps amount/currency
     defaults intact).
  5. POST cert-agent /api/llm-agent/run (sync — gives us a structured
     summary back). Persist run_id + per-TC results into cert_runs +
     cert_test_results.
  6. Flip assignment.status: CERTIFIED on all-PASS, else CERTIFYING
     (admin can act on triage triggers).

Runs as a fire-and-forget background task — partner's HTTP call
returns immediately on the readiness ack; the cert run unfolds async.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.domain.contract import cert_vocabulary_of
from app.core.domain.registry import get_active_pack
from app.services import cert_modes as _modes
from app.models.base import generate_uuid
from app.models.change_request import ChangeRequest
from app.models.phase_c import (
    PartnerAgent,
    ChangePartnerAssignment, AssignmentStatus,
    CertRun, CertRunStatus, CertTestResult, CertDirection, CertTestStatus,
    A2ATaskType,
)
from app.a2a_common import protocol as _proto
from app.services.assignment_status import set_status


logger = logging.getLogger(__name__)


# Base URL for cert-agent's HTTP API. Sourced from `settings.cert_agent_url`
# (env CERT_AGENT_URL) so deployments where cert-agent runs outside the
# docker network — e.g. Ubuntu native install on the host — can point
# this at host.docker.internal:8000 or a real IP/FQDN without code change.
# Resolved at import time; env changes require a backend restart (the
# Settings instance reads .env once at process start anyway).
_CERT_AGENT_URL = settings.cert_agent_url.rstrip("/")


def _flow_for(change: ChangeRequest) -> str:
    """Flow code for this change. Prefer the engine artifact's feature_name
    when an artifact exists — it's what cert-push uses, and what the
    cert-agent rows are filed under. Falls back to the CR title.

    Without this, a CR title like '"Smart Split & Auto-Collect" for UPI'
    derives to SMART_SPLIT_AUTO_COLLECT_FOR_UPI, while the docgen-produced
    cert_test_cases doc carries feature_name='UPI Smart Split & Auto-Settle
    Certification Annexure' → UPI_SMART_SPLIT_AUTO_SETTLE_CERTIFICATION_ANNEXURE.
    The orchestrator would look for TCs under the title-derived flow,
    miss them, and trigger a redundant LLM auto-push every cycle.
    """
    import json as _json
    from pathlib import Path
    base = Path(settings.artifacts_dir or "/app/artifacts") / "excel_engine"
    for sub in ("workbooks", "artifacts"):
        d = base / sub
        if not d.exists():
            continue
        for jp in d.rglob("*.json"):
            try:
                data = _json.loads(jp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (isinstance(data, dict)
                and data.get("change_request_id") == change.id
                and (data.get("feature_name") or "").strip()):
                fn = data["feature_name"]
                return re.sub(r"[^A-Z0-9]+", "_", fn.upper()).strip("_") or "UNKNOWN_FLOW"
    name = change.title or "UNKNOWN_CHANGE"
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "UNKNOWN_FLOW"


# Keyword → certificate "Certified Product" label. Best-effort tick based on
# the derived flow; unmatched flows leave the product checklist blank for NPCI
# ops to complete in the downloaded doc (the cert is bank-stamped manually
# anyway per the certificate's own Note #3).
def _build_signoff_meta(
    partner, change, *, flow: str, role: str, run_id: str,
    passed: int, total: int, signoff_at: str,
    modes=None,
) -> dict:
    """Map run/partner/change data onto the certificate header fields.

    Derived fields are filled here; everything we don't store falls back to
    `cert_signoff_doc.STATIC_DEFAULTS` (version strings, blank checkboxes for
    ops to complete). Scope is mapped from the cert role.

    `modes` (I-6b/§3.6.3) puts WHAT WAS ON THE OTHER END on the certificate:
    passing against a simulator proves message shape and wiring, passing
    against a deployed application proves the application, and a certificate
    that records them identically claims more than was verified.
    """
    # The ecosystem's own words, from the active pack — never constants here
    # (the engine must not carry one domain's certificate taxonomy).
    vocab = cert_vocabulary_of(get_active_pack())
    # signoff_at is an ISO timestamp; show just the date portion.
    date_str = (signoff_at or "").split("T")[0]
    return {
        "handle":               partner.cert_agent_bank_id or "",
        "bank_name":            partner.name or "",
        "certification_id":     run_id or "",
        "date_of_certification": date_str,
        "script_documented_date": date_str,
        "certified_product":    vocab.product_for(flow),
        "scope":                vocab.scope_for(role),
        "testcase_version":     change.title or flow,
        "rounds":               1,
        "final_result":         ["PASSED"],
        "online_certification": ["PASSED"],
        "npci_mode":            (modes.npci if modes else _modes.SIMULATOR),
        "partner_mode":         (modes.partner if modes else _modes.SIMULATOR),
        "evidence":             (modes or _modes.RunModes()).evidence(),
    }


async def _get_existing_test_cases(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        f"{_CERT_AGENT_URL}/api/certification/test-cases",
        params={"enabled_only": "false"},
    )
    r.raise_for_status()
    return r.json()


async def _patch_test_case(
    client: httpx.AsyncClient,
    tc_id: str,
    merged_test_data: dict,
) -> None:
    """PUT the test case with merged test_data. Cert-agent's update
    endpoint is a full PUT, so we send the whole row back unchanged
    except for test_data."""
    r = await client.get(f"{_CERT_AGENT_URL}/api/certification/test-cases/{tc_id}")
    r.raise_for_status()
    row = r.json()
    row["test_data"] = merged_test_data
    # Cert-agent's PUT requires the same TestCaseUpdate shape. Strip
    # server-generated fields; everything else passes through.
    for k in ("created_at", "updated_at"):
        row.pop(k, None)
    r2 = await client.put(
        f"{_CERT_AGENT_URL}/api/certification/test-cases/{tc_id}",
        json=row,
    )
    r2.raise_for_status()


def _filter_partner_test_data(role: str, test_data: dict) -> dict:
    """Keep only fields relevant to the role. Prevents a Payer-side payload
    from accidentally overwriting payee VPAs etc.

    The role→fields map is PACK vocabulary (the field names are the
    ecosystem's), so this reads it from the active domain rather than
    carrying one domain's field list in the engine.
    """
    return cert_vocabulary_of(get_active_pack()).filter_test_data(role, test_data)


async def orchestrate_cert_run(
    change_id: str,
    partner_id: str,
    role: str,
    test_data: dict,
    test_data_per_case: dict | None = None,
    dispatch_meta: dict | None = None,  # C-6 round audit; recorded by the engine path
) -> dict:
    """Configure cert-agent test cases with partner data and trigger
    a certification run. Returns a summary dict; never raises (errors
    surface via assignment status + log lines).

    Slice 3 — `test_data_per_case` is a `{tc_id: {field: value, ...}}`
    dict the partner submits via the new per-TC form. Per-case values
    win over the flat `test_data` fallback; both win over whatever the
    TC already had in its tc_store row. The merge order is:
        existing  →  scoped_flat  →  per_case[tc_id]
    so partners can leave `test_data` blank and supply only the per-TC
    block when they have specific values per scenario.
    """
    # This function is now the cert-agent REST implementation only. It used to
    # begin by delegating to the precert engine when precert_engine_enabled was
    # set; that selector moved UP to services/certification_dispatch, which
    # resolves the harness the active domain pack declares.
    #
    # The branch could not stay here: packs/network/certification.py calls this
    # function, so resolving the harness inside it would recurse.
    db: Session = SessionLocal()
    test_data_per_case = test_data_per_case or {}
    summary = {
        "change_id":  change_id,
        "partner_id": partner_id,
        "role":       role,
        "skipped":    False,
        "skip_reason": None,
        "tcs_patched": 0,
        "tcs_patched_per_case": 0,
        "run_id":      None,
        "passed":      0,
        "failed":      0,
        "elapsed_ms":  0,
    }
    try:
        partner = db.get(PartnerAgent, partner_id)
        change  = db.get(ChangeRequest, change_id)
        if not partner or not change:
            summary["skipped"] = True
            summary["skip_reason"] = "partner or change not found"
            return summary
        bank_id = (partner.cert_agent_bank_id or "").strip()
        if not bank_id:
            summary["skipped"] = True
            summary["skip_reason"] = (
                f"partner '{partner.name}' has no cert_agent_bank_id mapping — "
                "set it on Admin → Partners"
            )
            logger.warning("cert_orchestrate.skip change=%s partner=%s reason=%s",
                           change_id, partner_id, summary["skip_reason"])
            return summary
        flow = _flow_for(change)
        # Role prefixes are pack vocabulary now
        # (`cert_vocabulary_of(pack).role_prefixes`); this path deliberately
        # does not partition by role — kept explicit rather than implied.
        prefix = ""
        scoped_test_data = _filter_partner_test_data(role, test_data)

        logger.info(
            "cert_orchestrate.start change=%s partner=%s bank=%s flow=%s role=%s prefix=%r",
            change_id, partner_id, bank_id, flow, role, prefix,
        )

        _headers = {"X-Internal-Token": settings.cert_agent_internal_token}
        async with httpx.AsyncClient(timeout=180.0, headers=_headers) as http:
            # 1) Find cert-agent TCs for this flow + role prefix.
            tcs = await _get_existing_test_cases(http)
            total_in_flow = sum(
                1 for t in tcs if t.get("flow", "").upper() == flow.upper()
            )

            # If the flow has zero TCs in cert-agent yet, push them now
            # via the same path the Push-to-Cert button uses. Makes
            # Declare Ready a one-button experience: partner side fires
            # one click, NPCI side seeds the simulator + patches + runs.
            if total_in_flow == 0:
                logger.info(
                    "cert_orchestrate.auto_push flow=%s — no TCs in cert-agent, pushing first",
                    flow,
                )
                from app.api.cert_push import push_to_cert
                try:
                    pushed = await push_to_cert(change_id, db, None)
                    logger.info(
                        "cert_orchestrate.auto_push_done sent=%d created=%d source=%s flow=%s",
                        pushed.get("test_cases_sent", 0),
                        pushed.get("created", 0),
                        pushed.get("source"),
                        pushed.get("flow"),
                    )
                except Exception as exc:  # noqa: BLE001
                    summary["skipped"] = True
                    summary["skip_reason"] = f"auto-push failed: {exc}"
                    logger.warning(
                        "cert_orchestrate.auto_push_failed flow=%s err=%s",
                        flow, exc,
                    )
                    return summary

                # The artifact may carry a different `feature_name` from
                # the CR title (e.g. the docgen pipeline calls it
                # "UPI Smart Split & Auto-Settle Certification Annexure"
                # while the CR title is just "Smart Split & Auto-Collect
                # for UPI"). cert-push uses the artifact's name to derive
                # the flow code; we trust that — it's where the rows
                # actually live in cert-agent. Without this override the
                # subsequent role-prefix match looks at the wrong flow
                # and returns 0 matches.
                pushed_flow = (pushed.get("flow") or "").strip()
                if pushed_flow and pushed_flow.upper() != flow.upper():
                    logger.info(
                        "cert_orchestrate.flow_override change=%s cr_title_flow=%s artifact_flow=%s",
                        change_id, flow, pushed_flow,
                    )
                    flow = pushed_flow

                # Re-fetch after the push so matching sees the new rows.
                tcs = await _get_existing_test_cases(http)
                total_in_flow = sum(
                    1 for t in tcs if t.get("flow", "").upper() == flow.upper()
                )

            matching = [
                t for t in tcs
                if (t.get("flow", "").upper() == flow.upper())
                and (not prefix or str(t.get("tc_id", "")).startswith(prefix))
            ]
            logger.info(
                "cert_orchestrate.matched flow=%s prefix=%r total_in_flow=%d matching=%d",
                flow, prefix,
                sum(1 for t in tcs if t.get("flow", "").upper() == flow.upper()),
                len(matching),
            )
            if not matching:
                summary["skipped"] = True
                summary["skip_reason"] = (
                    f"no test cases in cert-agent flow={flow} prefix={prefix or 'ANY'}"
                )
                return summary

            # 2) PATCH each matching TC with merged test_data.
            #    Merge order: existing → scoped_flat → per_case[tc_id]
            patched = 0
            per_case_hits = 0
            for tc in matching:
                tc_id = tc.get("tc_id", "")
                per_case_for_tc = test_data_per_case.get(tc_id) or {}
                # Strip empty values from the per-case dict so a blank
                # form field doesn't blow away a real one.
                per_case_for_tc = {
                    k: v for k, v in per_case_for_tc.items()
                    if v not in (None, "")
                }
                merged = {
                    **(tc.get("test_data") or {}),
                    **scoped_test_data,
                    **per_case_for_tc,
                }
                if not merged:
                    continue
                try:
                    await _patch_test_case(http, tc_id, merged)
                    patched += 1
                    if per_case_for_tc:
                        per_case_hits += 1
                except httpx.HTTPError as exc:
                    logger.warning(
                        "cert_orchestrate.patch_failed tc=%s err=%s",
                        tc_id, exc,
                    )
            summary["tcs_patched"] = patched
            summary["tcs_patched_per_case"] = per_case_hits

            # 3) Mark assignment CERTIFYING (orthogonal to set_status which
            # advances along the lifecycle — readiness handler already
            # set READY_FOR_CERTIFICATION, this advances to CERTIFYING).
            assignment = db.scalars(
                select(ChangePartnerAssignment).where(
                    ChangePartnerAssignment.change_request_id == change_id,
                    ChangePartnerAssignment.partner_id == partner_id,
                )
            ).first()
            if assignment:
                set_status(
                    assignment, AssignmentStatus.CERTIFYING, db,
                    actor_partner_id=partner_id,
                    reason=f"Cert run started for flow {flow} (bank={bank_id})",
                )

            # 4) Fire LLM-driven cert run (or use mock when env says so).
            #
            # MOCK_CERT_RUN=true short-circuits the cert-agent /api/llm-agent/run
            # HTTP call and synthesises an all-PASS run_data for every matched
            # TC. Use this when the Anthropic key is out of credits OR when
            # iterating on the UI / back-channel / signoff flow without
            # spending model tokens. Result shape mirrors the real
            # `/api/llm-agent/run` response so downstream code (persistence,
            # outbound CERT_TEST_RESPONSE, all-PASS signoff) is unchanged.
            #
            # Slice 4 — two-phase sequential execution.
            #   Phase A: every TC with initiated_by=NPCI runs through cert-agent
            #            via the deterministic REST path (same as before).
            #   Phase B: every TC with initiated_by=BANK runs via the bank-agent
            #            self-test endpoint so the request genuinely originates
            #            from the bank side (matches the demo narrative).
            # Phase B fires only after Phase A reaches a terminal state;
            # both results are stitched together for the rest of the
            # pipeline.
            authority_tc_ids = [
                t["tc_id"] for t in matching
                if (t.get("initiated_by") or "NPCI").upper() != "BANK"
            ]
            bank_tc_ids = [
                t["tc_id"] for t in matching
                if (t.get("initiated_by") or "").upper() == "BANK"
            ]
            tc_ids = [t["tc_id"] for t in matching]  # legacy var; kept for mock path + persistence
            summary["npci_tc_count"] = len(authority_tc_ids)
            summary["bank_tc_count"] = len(bank_tc_ids)
            from app.core.config import settings as _app_settings

            async def _direct_rest_run(subset_tc_ids: list[str], phase_label: str) -> dict:
                """Deterministic cert run path bypassing the LLM agent loop.

                cert-agent's /api/llm-agent/run lets Claude pick test cases
                via tools, but the model has been observed to end_turn
                early without polling get_run_status — returning empty
                results even while bank-simulator continues processing TCs
                async in the background. The direct REST flow takes the
                exact tc_ids we matched, runs them through the same
                runner, then polls /runs/{run_id} until terminal.

                Returns the same shape /api/llm-agent/run would have:
                  { run_id, passed, failed, results: [...] }.
                """
                # 1. Initiate — reserves a run_id (phase suffix for traceability)
                init = await http.post(
                    f"{_CERT_AGENT_URL}/api/certification/initiate",
                    json={"bank_id": bank_id, "feature": f"{flow}-{phase_label}"},
                )
                init.raise_for_status()
                rid = init.json().get("run_id")
                # 2. Run — submits the exact tc_ids for THIS phase only
                run = await http.post(
                    f"{_CERT_AGENT_URL}/api/certification/run",
                    json={"bank_id": bank_id, "run_id": rid,
                          "tc_ids": subset_tc_ids,
                          "initiated_by": phase_label.upper()},
                )
                run.raise_for_status()
                logger.info(
                    "cert_orchestrate.direct_run_submitted phase=%s run=%s tcs=%d",
                    phase_label, rid, len(subset_tc_ids),
                )
                # 3. Poll until COMPLETED (max ~120s)
                import asyncio
                terminal_results = None
                for _ in range(60):  # 60 × 2s = 120s
                    await asyncio.sleep(2.0)
                    s = await http.get(f"{_CERT_AGENT_URL}/api/certification/runs/{rid}")
                    if s.status_code != 200:
                        continue
                    body = s.json()
                    if body.get("status") == "COMPLETED":
                        terminal_results = body
                        break
                if not terminal_results:
                    logger.warning(
                        "cert_orchestrate.direct_run_timeout run=%s — falling back to last poll",
                        rid,
                    )
                    s = await http.get(f"{_CERT_AGENT_URL}/api/certification/runs/{rid}")
                    terminal_results = s.json() if s.status_code == 200 else {"results": []}

                results_list = terminal_results.get("results") or []
                passed = sum(1 for r in results_list if (r.get("status") or "").upper() == "PASS")
                failed = sum(1 for r in results_list if (r.get("status") or "").upper() == "FAIL")
                logger.info(
                    "cert_orchestrate.direct_run_complete run=%s pass=%d fail=%d total=%d",
                    rid, passed, failed, len(results_list),
                )
                return {
                    "run_id": rid,
                    "passed": passed,
                    "failed": failed,
                    "elapsed_ms": 0,
                    "results": results_list,
                }

            async def _bank_agent_run(subset_tc_ids: list[str]) -> dict:
                """Phase B execution path — bank-agent's self-test endpoint.

                The bank-agent has /api/self-test/run (its own demo button).
                Calling it from here means the request originates on the
                bank side; bank-agent then forwards to cert-agent via A2A
                streaming with a REST fallback.

                The REST-fallback path returns BEFORE TC callbacks land
                (executions[*].status='SENT'). To get real verdicts we
                always poll cert-agent's /api/certification/runs/{rid}
                until COMPLETED — same polling block as _direct_rest_run.
                That covers both transports (the polled cert-agent state
                is authoritative either way).
                """
                bank_url = getattr(_app_settings, "bank_agent_url", "") or "http://bank-agent:8003"
                bank_url = bank_url.rstrip("/")
                # Unique per orchestrator invocation so cert-agent doesn't
                # accumulate dupes when the same change is re-triggered
                # (run_id is the dedup key in cert-agent's executions store).
                from uuid import uuid4 as _uuid4
                rid = f"BANK-{change_id[:8]}-{_uuid4().hex[:6].upper()}"
                # 1. Fire bank-agent self-test (may return SENT, that's fine)
                try:
                    r = await http.post(
                        f"{bank_url}/api/self-test/run",
                        json={"tc_ids": subset_tc_ids, "run_id": rid},
                    )
                    r.raise_for_status()
                    body = r.json() or {}
                    rid = body.get("run_id") or rid
                except httpx.HTTPError as exc:
                    logger.warning(
                        "cert_orchestrate.bank_phase_failed bank_url=%s err=%s",
                        bank_url, exc,
                    )
                    return {"run_id": rid, "passed": 0, "failed": 0,
                            "results": [], "phase_error": str(exc)}

                # 2. Poll cert-agent until the BANK run reaches terminal.
                #    bank-agent fires via REST fallback (returns SENT
                #    immediately) OR A2A streaming (returns when complete).
                #    Either way cert-agent is the source of truth — poll
                #    its run-status endpoint same as _direct_rest_run.
                import asyncio as _asyncio
                terminal_results = None
                for _ in range(60):  # 60 × 2s = 120s
                    await _asyncio.sleep(2.0)
                    try:
                        s = await http.get(f"{_CERT_AGENT_URL}/api/certification/runs/{rid}")
                    except httpx.HTTPError:
                        continue
                    if s.status_code != 200:
                        continue
                    poll_body = s.json()
                    if poll_body.get("status") == "COMPLETED":
                        terminal_results = poll_body
                        break
                if not terminal_results:
                    logger.warning(
                        "cert_orchestrate.bank_phase_timeout run=%s — using last poll",
                        rid,
                    )
                    try:
                        s = await http.get(f"{_CERT_AGENT_URL}/api/certification/runs/{rid}")
                        terminal_results = s.json() if s.status_code == 200 else {"results": []}
                    except httpx.HTTPError:
                        terminal_results = {"results": []}

                results_list = terminal_results.get("results") or []
                passed = sum(1 for r in results_list if (r.get("status") or "").upper() == "PASS")
                failed = sum(1 for r in results_list if (r.get("status") or "").upper() == "FAIL")
                logger.info(
                    "cert_orchestrate.bank_phase_polled run=%s pass=%d fail=%d total=%d",
                    rid, passed, failed, len(results_list),
                )
                return {
                    "run_id": rid,
                    "passed": passed,
                    "failed": failed,
                    "elapsed_ms": 0,
                    "results": results_list,
                }

            if getattr(_app_settings, "mock_cert_run", False):
                logger.info(
                    "cert_orchestrate.mock_run change=%s flow=%s tcs=%d (MOCK_CERT_RUN=true)",
                    change_id, flow, len(tc_ids),
                )
                run_data = {
                    "run_id": f"MOCK-{change_id[:8]}",
                    "passed": len(tc_ids),
                    "failed": 0,
                    "elapsed_ms": 0,
                    "results": [
                        {
                            "test_case_id":  tc_id,
                            "status":        "PASS",
                            "expected_resp_code": "00",
                            "actual_resp_code":   "00",
                        }
                        for tc_id in tc_ids
                    ],
                }
            else:
                # Phase A — authority-initiated batch through cert-agent.
                if authority_tc_ids:
                    authority_batch = await _direct_rest_run(authority_tc_ids, "NPCI")
                else:
                    authority_batch = {"run_id": None, "passed": 0, "failed": 0,
                                 "elapsed_ms": 0, "results": []}
                logger.info(
                    "cert_orchestrate.phase_a_done change=%s authority_pass=%d authority_fail=%d",
                    change_id, authority_batch["passed"], authority_batch["failed"],
                )
                summary["npci_run_id"] = authority_batch.get("run_id")
                summary["authority_passed"] = authority_batch["passed"]
                summary["authority_failed"] = authority_batch["failed"]

                # Phase B — BANK-initiated batch via bank-agent self-test.
                # Fires after Phase A reaches terminal. Demo policy:
                # continue even if Phase A had failures (the bank batch
                # still adds signal). If you'd rather block on NPCI
                # failures, gate the call on authority_batch["failed"] == 0.
                if bank_tc_ids:
                    bank_data = await _bank_agent_run(bank_tc_ids)
                else:
                    bank_data = {"run_id": None, "passed": 0, "failed": 0,
                                 "elapsed_ms": 0, "results": []}
                logger.info(
                    "cert_orchestrate.phase_b_done change=%s bank_pass=%d bank_fail=%d",
                    change_id, bank_data["passed"], bank_data["failed"],
                )
                summary["bank_run_id"] = bank_data.get("run_id")
                summary["bank_passed"] = bank_data["passed"]
                summary["bank_failed"] = bank_data["failed"]

                # Stitched run_data — combined results, NPCI run_id as
                # primary for back-compat (downstream cert_run persistence
                # uses one run_id; the per-phase ones live in summary).
                run_data = {
                    "run_id": authority_batch.get("run_id") or bank_data.get("run_id"),
                    "passed": authority_batch["passed"] + bank_data["passed"],
                    "failed": authority_batch["failed"] + bank_data["failed"],
                    "elapsed_ms": authority_batch.get("elapsed_ms", 0) + bank_data.get("elapsed_ms", 0),
                    "results": list(authority_batch.get("results", [])) + list(bank_data.get("results", [])),
                }
            summary["run_id"]     = run_data.get("run_id")
            summary["passed"]     = int(run_data.get("passed", 0) or 0)
            summary["failed"]     = int(run_data.get("failed", 0) or 0)
            summary["elapsed_ms"] = int(run_data.get("elapsed_ms", 0) or 0)
            results = run_data.get("results") or []

            # 5) Persist into cert_runs + cert_test_results.
            cert_run = CertRun(
                id=generate_uuid(),
                change_request_id=change_id,
                partner_id=partner_id,
                # Protocol v1 master cert id — stable per (change, partner) so
                # all attempts (run_number == cert_attempt) share one cflow_id.
                cflow_id=f"CFLOW-{change_id[:8]}-{partner_id[:8]}",
                run_number=int((run_data.get("run_id") or "0").split("-")[-1] or 0)
                           if (run_data.get("run_id") or "").split("-")[-1].isdigit()
                           else 1,
                # CertRunStatus is RUNNING | COMPLETED — pass/fail is
                # captured in the per-TC results, not the run-level status.
                status=CertRunStatus.COMPLETED,
                total=len(results),
                passed=summary["passed"],
                failed=summary["failed"],
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
            )
            db.add(cert_run)
            db.flush()
            # Map every TC to its initiated-by side so per-row direction
            # reflects the actual phase that ran it. Read from the
            # `matching` list we built when selecting TCs.
            init_by_tc = {
                t["tc_id"]: (t.get("initiated_by") or "NPCI").upper()
                for t in matching
            }
            for r in results:
                tc_id = r.get("test_case_id") or r.get("tc_id") or ""
                status_raw = (r.get("status") or "").upper()
                tc_status = (
                    CertTestStatus.PASS if status_raw == "PASS"
                    else CertTestStatus.FAIL if status_raw == "FAIL"
                    else CertTestStatus.SKIP
                )
                direction = (
                    CertDirection.PARTNER_TO_AUTHORITY
                    if init_by_tc.get(tc_id) == "BANK"
                    else CertDirection.AUTHORITY_TO_PARTNER
                )
                db.add(CertTestResult(
                    id=generate_uuid(),
                    cert_run_id=cert_run.id,
                    test_case_id=tc_id,
                    direction=direction,
                    status=tc_status,
                ))

            # 6) Flip assignment status if all passed.
            if assignment and summary["failed"] == 0 and summary["passed"] > 0:
                set_status(
                    assignment, AssignmentStatus.CERTIFIED, db,
                    actor_partner_id=partner_id,
                    reason=f"Cert run {summary['run_id']} all-PASS ({summary['passed']}/{len(results)})",
                )

            db.commit()
            logger.info(
                "cert_orchestrate.done change=%s partner=%s run=%s pass=%d fail=%d patched=%d",
                change_id, partner_id, summary["run_id"],
                summary["passed"], summary["failed"], patched,
            )

            # 7) Ship results back to the partner over A2A so the bank
            #    can see pass/fail per TC alongside the cert lifecycle
            #    stepper. Wire payload mirrors what cert-engine sends
            #    inbound (CERT_TEST_RESPONSE handler on NPCI), so the
            #    partner side can be a thin store-and-render. Wrapped
            #    in try/except: a wire failure must NOT roll back the
            #    DB writes above — the run still happened.
            try:
                from app.services.a2a_client import send_task_to_partner

                # Normalise each per-TC result to a flat shape the
                # partner UI can render without remapping.
                #
                # Slice 5 — stamp each wire row with `initiated_by` and
                # the phase it ran in (NPCI batch or BANK batch) so the
                # partner can show a per-phase breakdown on their signoff
                # viewer. Look-up uses the `matching` list we built when
                # selecting TCs for this run.
                init_by_tc = {
                    t["tc_id"]: (t.get("initiated_by") or "NPCI").upper()
                    for t in matching
                }
                wire_results = []
                for r in results:
                    tc_id_w = r.get("test_case_id") or r.get("tc_id") or ""
                    status_w = (r.get("status") or "").upper()
                    wire_results.append({
                        "test_case_id":  tc_id_w,
                        "status":        status_w,
                        # OC 193A txn id + sent timestamp ride along so the
                        # sign-off certificate's TXN ID / Date columns populate.
                        "txn_id":        r.get("txn_id"),
                        "sent_at":       r.get("sent_at"),
                        "expected_code": r.get("expected_resp_code") or r.get("expected_code"),
                        "actual_code":   r.get("actual_resp_code") or r.get("actual_code") or r.get("error_code"),
                        "error_message": r.get("error_message") or r.get("message"),
                        "initiated_by":  init_by_tc.get(tc_id_w, "NPCI"),
                        # correlation_id from cert-agent's get_run_status —
                        # partner UI uses it to open a per-TC log drawer.
                        "correlation_id": r.get("correlation_id"),
                    })

                # IMPORTANT: cert_run_id on the wire is the LOCAL CertRun.id
                # (UUID) so the partner's ACK echoes back something we can
                # look up. cert-agent's short RUN-xxx code is shipped as
                # `external_run_id` for cross-system audit, not as the key.
                results_payload = {
                    "cert_run_id":     cert_run.id,
                    "external_run_id": summary["run_id"],
                    "feature_name":  change.title or "",
                    "flow":          flow,
                    "bank_id":       bank_id,
                    "role":          role,
                    "total":         len(results),
                    "passed":        summary["passed"],
                    "failed":        summary["failed"],
                    "skipped":       int(run_data.get("skipped", 0) or 0),
                    "completed_at":  datetime.now(timezone.utc).isoformat(),
                    "results":       wire_results,
                    # Slice 4/5 — per-phase split so the partner can see
                    # which side drove which batch and whose failures
                    # they're looking at.
                    "phases": {
                        "npci": {
                            "run_id":  summary.get("npci_run_id"),
                            "total":   summary.get("npci_tc_count", 0),
                            "passed":  summary.get("authority_passed", 0),
                            "failed":  summary.get("authority_failed", 0),
                        },
                        "bank": {
                            "run_id":  summary.get("bank_run_id"),
                            "total":   summary.get("bank_tc_count", 0),
                            "passed":  summary.get("bank_passed", 0),
                            "failed":  summary.get("bank_failed", 0),
                        },
                    },
                }

                # Reuse partner-side DB session — send_task_to_partner
                # writes an outbound a2a_messages audit row.
                await send_task_to_partner(
                    partner=partner,
                    task_type=A2ATaskType.CERT_TEST_RESPONSE,
                    payload=results_payload,
                    db=db,
                    change_request_id=change_id,
                    cflow_id=cert_run.cflow_id,
                    cert_attempt=cert_run.run_number,
                )
                db.commit()
                logger.info(
                    "cert_orchestrate.results_sent change=%s partner=%s run=%s",
                    change_id, partner_id, summary["run_id"],
                )

                # 8) If the run was all-PASS, fire a completion signoff —
                #    the formal "you are certified" signal. Partner
                #    acknowledges it with CERT_ACKNOWLEDGEMENT subject=
                #    completion_signoff, which stamps cert_runs.
                #    completion_signed_off_at on NPCI and flips
                #    assignment.status to CERTIFIED.
                all_passed = (
                    summary["failed"] == 0
                    and summary["passed"] > 0
                    and summary["passed"] == len(results)
                )
                if all_passed:
                    signoff_at = datetime.now(timezone.utc)
                    # Certificate validity: 1 year, matching the A2A spec
                    # signoff convention (issued_at + 365d, advisory).
                    from datetime import timedelta as _td
                    valid_until = signoff_at + _td(days=365)
                    # _build_signoff_meta + the wire field both want an ISO string.
                    signoff_at_iso = signoff_at.isoformat()
                    signoff_payload = {
                        "cert_run_id":     cert_run.id,
                        "external_run_id": summary["run_id"],
                        "feature_name":  change.title or "",
                        "flow":          flow,
                        "bank_id":       bank_id,
                        "role":          role,
                        "total":         len(results),
                        "passed":        summary["passed"],
                        "completed_at":  results_payload["completed_at"],
                        "signoff_at":    signoff_at_iso,
                        "valid_until":   valid_until.date().isoformat(),
                        # Slice 5 — per-phase breakdown on the certificate
                        # so the partner's signoff doc shows the NPCI
                        # vs BANK split too.
                        "phases":        results_payload["phases"],
                        # Carry the per-TC results on the signoff envelope
                        # too. The partner handler preserves an existing
                        # cases list when the inbound has none — defensive
                        # belt + suspenders so the demo never loses the
                        # results table after signoff fires.
                        "results":       wire_results,
                        "signoff_message": (
                            f"Congratulations — {change.title or flow} "
                            f"certified. {summary['passed']}/{len(results)} "
                            f"test cases passed "
                            f"(NPCI {summary.get('authority_passed', 0)}/"
                            f"{summary.get('npci_tc_count', 0)}, "
                            f"BANK {summary.get('bank_passed', 0)}/"
                            f"{summary.get('bank_tc_count', 0)})."
                        ),
                    }

                    # Render the NPCI Certification Result certificate and
                    # pack it onto the wire the same way Product Kit docs ship
                    # (base64 of raw bytes + SHA-256 over raw for integrity).
                    # A render failure must not block the signoff signal — the
                    # status flip + cert_summary still reach the partner.
                    try:
                        import base64 as _b64, hashlib as _hashlib
                        from app.services.cert_signoff_doc import build_signoff_docx

                        doc_meta = _build_signoff_meta(
                            partner, change, flow=flow, role=role,
                            run_id=summary["run_id"], passed=summary["passed"],
                            total=len(results), signoff_at=signoff_at_iso,
                        )
                        doc_results = [
                            {
                                "test_id": w.get("test_case_id"),
                                "txn_id":  w.get("txn_id"),
                                "date":    (w.get("sent_at") or "").split("T")[0],
                                "status":  w.get("status"),
                            }
                            for w in wire_results
                        ]
                        raw = build_signoff_docx(doc_meta, doc_results)
                        fname = f"Certification_Result_{bank_id}_{summary['run_id']}.docx"
                        signoff_payload["signoff_docx_b64"]    = _b64.b64encode(raw).decode()
                        signoff_payload["signoff_filename"]    = fname
                        signoff_payload["signoff_sha256"]      = _hashlib.sha256(raw).hexdigest()
                        signoff_payload["signoff_size_bytes"]  = len(raw)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "cert_orchestrate.signoff_doc_failed change=%s err=%s",
                            change_id, exc,
                        )
                    try:
                        signoff_msg = await send_task_to_partner(
                            partner=partner,
                            # Protocol v1: renamed cert_completion_signoff →
                            # cert_signoff_notification (§7.11).
                            task_type=_proto.A2ATaskType.CERT_SIGNOFF_NOTIFICATION,
                            payload=signoff_payload,
                            db=db,
                            change_request_id=change_id,
                            cflow_id=cert_run.cflow_id,
                            cert_attempt=cert_run.run_number,
                        )
                        # The partner's synchronous SDK acceptance IS the
                        # non-repudiation ACK — stamp completion_signed_off_at
                        # (migration 0050) on confirmed delivery.
                        if signoff_msg is not None and signoff_msg.status == "delivered":
                            cert_run.completion_signed_off_at = datetime.now(timezone.utc)
                        db.commit()
                        logger.info(
                            "cert_orchestrate.signoff_sent change=%s partner=%s run=%s delivered=%s",
                            change_id, partner_id, summary["run_id"],
                            signoff_msg.status if signoff_msg else "none",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "cert_orchestrate.signoff_send_failed change=%s partner=%s err=%s",
                            change_id, partner_id, exc,
                        )
            except Exception as exc:  # noqa: BLE001
                # Non-fatal — partner can still pull via NPCI admin tools.
                logger.warning(
                    "cert_orchestrate.results_send_failed change=%s partner=%s err=%s",
                    change_id, partner_id, exc,
                )

            return summary
    except Exception as exc:  # noqa: BLE001
        logger.exception("cert_orchestrate.error change=%s partner=%s", change_id, partner_id)
        summary["skipped"] = True
        summary["skip_reason"] = f"orchestration error: {exc}"
        try:
            db.rollback()
        except Exception:
            pass
        return summary
    finally:
        db.close()


async def orchestrate_cert_run_precert_engine(
    change_id: str,
    partner_id: str,
    role: str,
    test_data: dict,
    test_data_per_case: dict | None = None,
    dispatch_meta: dict | None = None,
) -> dict:
    """Engine-backed cert run driven as a FULL signed A2A conversation with the partner (P3):
    cert_config_request→submission, cert_setup_notification→test_preparation, per-case
    cert_case_result, and on failure cert_verdict_notification→waiver_request→waiver_decision,
    then cert_test_response + signoff. Every message rides a2a_common (signed + audited in
    a2a_messages); config + per-case test data come FROM the bank over A2A. The engine
    (connector/provisioner) still drives the REAL precert → bank-sim → precertdb. Selected by
    settings.precert_engine_enabled. Same contract as orchestrate_cert_run: fire-and-forget,
    never raises (errors surface via status + logs).
    """
    import asyncio

    db: Session = SessionLocal()
    _ = test_data_per_case  # accepted for signature-compat; scope is configured, not per-TC
    summary = {
        "change_id": change_id, "partner_id": partner_id, "role": role,
        "engine": True, "skipped": False, "skip_reason": None,
        "run_id": None, "passed": 0, "failed": 0, "elapsed_ms": 0,
    }
    try:
        partner = db.get(PartnerAgent, partner_id)
        change = db.get(ChangeRequest, change_id)
        if not partner or not change:
            summary["skipped"] = True
            summary["skip_reason"] = "partner or change not found"
            return summary

        flow = _flow_for(change)
        bank_id = (partner.cert_agent_bank_id or settings.precert_engine_psp_org_id or "").strip()
        logger.info(
            "precert_engine.start change=%s partner=%s bank=%s flow=%s role=%s subset=%s",
            change_id, partner_id, bank_id, flow, role, settings.precert_engine_subset,
        )

        # Advance the assignment READY_FOR_CERTIFICATION → CERTIFYING.
        assignment = db.scalars(
            select(ChangePartnerAssignment).where(
                ChangePartnerAssignment.change_request_id == change_id,
                ChangePartnerAssignment.partner_id == partner_id,
            )
        ).first()
        if assignment:
            set_status(
                assignment, AssignmentStatus.CERTIFYING, db,
                actor_partner_id=partner_id,
                reason=f"Engine cert run started (flow {flow}, subset {settings.precert_engine_subset})",
            )

        # ── The FULL cert lifecycle as a signed A2A conversation with the partner (P3).
        # Every step rides a2a_common (send_task_to_partner → audited in a2a_messages);
        # config + per-case test data come FROM the bank over A2A, results + verdicts go back.
        from app.services.a2a_client import send_task_to_partner
        cflow_id = f"CFLOW-{change_id[:8]}-{partner_id[:8]}"
        # C-3: ONE round value feeds everything downstream — the outbound
        # `cert_attempt`, each `cert_case_result.attempt`, `CertRun.run_number`,
        # `cert_case_specs.run_number` and `cert_flow_states.current_round`.
        # The first pass had THREE notions of the round with two frozen at 1;
        # since cert_case_specs is keyed on (cflow_id, run_number), that
        # mismatch strands every round after the first.
        _round = int(db.query(func.max(CertRun.run_number)).filter(
            CertRun.cflow_id == cflow_id).scalar() or 0) + 1
        # I-6b: per-run, explicit, NEVER inherited from the previous round —
        # `resolve` reads only what this dispatch asked for, so an operator
        # who ran round N against a real deployment does not silently get
        # round N+1 against it too (§3.6.4: real side effects don't reset).
        _run_modes = _modes.resolve((test_data or {}).get("modes"))
        # This harness drives precert's control API, which is a SIMULATOR
        # control surface; an application-mode side needs the trigger
        # contract (§3.6.1). The mode is still recorded so the evidence is
        # honest about what was actually on each end.
        _bound_pack_ref = (test_data or {}).get("pack_ref")
        # psycopg2 spells it `dbname`, not `database`.
        db_cfg = dict(host=settings.precert_engine_db_host, port=settings.precert_engine_db_port,
                      user=settings.precert_engine_db_user, password=settings.precert_engine_db_password,
                      dbname=settings.precert_engine_db_name)

        # Demo pacing — pause before each message so the conversation unfolds
        # step-by-step on screen instead of completing in one burst
        # (settings.precert_engine_demo_delay_seconds; 0 = full speed).
        _demo_delay = float(getattr(settings, "precert_engine_demo_delay_seconds", 0) or 0)

        async def _say(task_type, payload):
            if _demo_delay:
                await asyncio.sleep(_demo_delay)
            return await send_task_to_partner(
                partner=partner, task_type=task_type, payload=payload, db=db,
                change_request_id=change_id, cflow_id=cflow_id, cert_attempt=_round)

        # Track the spec's cert lifecycle alongside the conversation. One trigger per
        # message, validated by precert_engine.state_machine — which defined the
        # lifecycle but had nothing driving it until now.
        # C-0: the phase survives the process. Each dispatch still starts a
        # fresh FlowState at NOT_STARTED (no mid-phase resume — that belongs to
        # the loop); the stored row's phase is overwritten and its history
        # accumulates across rounds via the flow_store watermark.
        from app.services.cert_agent import flow_store
        from app.services.cert_agent.flow import FlowState
        from app.services.cert_agent.state_machine import Trigger as _Trg
        # `flow` (the cert-agent flow CODE from _flow_for) is about to be
        # SHADOWED by the FlowState; the results payload below needs the code,
        # not the state machine — this rebinding once put a FlowState repr on
        # the wire.
        _flow_code = flow
        flow = FlowState(cflow_id, persist=flow_store.persister(
            db, change_request_id=change_id, partner_id=partner_id, current_round=_round))
        flow.fire(_Trg.readiness_declared)          # NOT_STARTED -> CONFIG_REQUESTED

        # 1) CONFIG — ask the bank for its configuration.
        cfg_msg = await _say(_proto.A2ATaskType.CERT_CONFIG_REQUEST,
                             {"summary": "Please submit your certification configuration parameters."})
        cfg = (getattr(cfg_msg, "response_body", None) or {}).get("config", {}) or {}
        flow.fire(_Trg.config_submitted)            # -> CONFIG_RECEIVED
        psp = (cfg.get("psp_org_id") or partner.cert_agent_bank_id
               or settings.precert_engine_psp_org_id or "").strip()

        # ── PROVISION from the bank's own config, then resolve.
        # Per spec Appendix B, cert_setup_notification fires once NPCI "validated
        # the config, provisioned the simulator, mapped the suite" — so the bank's
        # submission is what onboards it (tbl_bank/tbl_psp) and maps the subset it
        # asked for (tbl_psp_subset*). Both writes are idempotent, so a re-run of an
        # already-onboarded bank changes nothing.
        def _provision():
            from app.services.cert_agent.setup import provision_from_config
            return provision_from_config(
                cfg, db_cfg=db_cfg,
                cert_name=settings.precert_engine_cert_name,
                default_subset=settings.precert_engine_subset,
                defaults={"psp_org_id": psp},
            )
        provisioned = await asyncio.to_thread(_provision)

        # Fall back to the READ-ONLY resolve when the bank sent nothing usable, so a
        # partner that only acks cert_config_request still certifies against a
        # hand-provisioned environment exactly as before.
        def _resolve():
            from app.services.precert_engine import NfiniteConnector, NfiniteProvisioner
            prov = NfiniteProvisioner(db_cfg)
            bank = prov.config_of(psp)
            if bank is None:
                return None, None, []
            subset = settings.precert_engine_subset
            assigned = prov.subsets_of(bank.bank_org_id)
            if assigned and subset not in assigned:
                subset = assigned[0]
            scope = NfiniteConnector(db=db_cfg).cases_in_subset(subset) if subset else []
            return bank, subset, scope

        if provisioned is not None:
            bank, subset, scope = provisioned.bank, provisioned.subset, provisioned.cases
            logger.info("precert_engine.provisioned psp=%s subset=%s changes=%s",
                        bank.psp_org_id, subset, provisioned.changes)
        else:
            bank, subset, scope = await asyncio.to_thread(_resolve)
        if bank is None or not scope:
            summary["skipped"] = True
            summary["skip_reason"] = f"bank {psp} not onboarded / empty scope"
            return summary

        # 2) SETUP — announce the scope with the full per-case case_list. Per the
        #    A2A spec (Appendix B, cert_setup_notification), each case carries its
        #    initiator, assigned api, expected_status and authority_batch (the scenario
        #    values NPCI owns) — all read straight from precertdb.
        def _build_case_list():
            from app.services.precert_engine import NfiniteConnector
            c0 = NfiniteConnector(db=db_cfg)
            return [c0.case_details(tc, cg) for tc, cg in scope]
        case_list = await asyncio.to_thread(_build_case_list)

        # C-1/§3.1: derive the GRADED scope from the registry delta. The
        # builder persists this round's request variants + assertion specs
        # (replacing any earlier rows for the same round), and the EXECUTED
        # set narrows to what it generated — an API the change did not touch
        # is not certified. `case_list` keeps the wire's own key names
        # (authority_batch, initiator: npci|bank); the neutral vocabulary lives on
        # the stored rows.
        from app.core.wire.registry import codec_for as _codec_for
        from app.services import cert_case_builder
        from app.services.cert_assertions import (
            assertion_failures as _assertion_failures,
            evaluate_specs as _evaluate_specs,
        )
        round_scope = cert_case_builder.derive_round_scope(
            db, change_id=change_id, cflow_id=cflow_id, run_number=_round,
            available_cases=case_list)
        if round_scope.empty:
            summary["skipped"] = True
            summary["skip_reason"] = f"no certifiable scope: {round_scope.result.summary()}"
            return summary
        scope = [(tc, cg) for tc, cg in scope if tc in round_scope.case_ids]
        case_list = round_scope.case_list
        variants_by_case = round_scope.variants_by_case
        specs_by_variant = round_scope.specs_by_variant

        case_ids = [c["case_id"] for c in case_list]
        initiator_of = {c["case_id"]: (c.get("initiator") or "npci") for c in case_list}
        # `simulator` + `suite_version` are ADDED to the existing keys rather than
        # replacing them: the partner handler reads `cases`, and the conversation UI
        # reads `summary`. Moving the payload wholesale to the spec shape is a
        # both-sides change (Phase 5), but the simulator address has to be on the
        # wire before bank-initiated execution can exist at all.
        from app.services.cert_agent.setup import simulator_block
        _onboarded = ("Onboarded and provisioned" if provisioned is not None
                      else "Certification environment provisioned")
        setup_msg = await _say(_proto.A2ATaskType.CERT_SETUP_NOTIFICATION,
            {"summary": f"{_onboarded}. Assigned test suite '{subset}' comprising {len(case_ids)} test case(s).",
             "subset": subset, "cases": case_ids, "case_list": case_list,
             "suite_version": settings.precert_engine_cert_name,
             # ITA-6: an alias declaration, never a raw URL — the partner's
             # stack calls its OWN tunnel ingress with this name (§3.3).
             # I-6b: the MODE selects which catalogue entry that name is —
             # `<x>_simulator` and `<x>_application` are two entries the
             # tunnel cannot tell apart, which is why mode needs no tunnel
             # change. `pack_ref` binds the contract (SIM §3.3) when one is
             # bound; None keeps the endpoint pack-free for the precert path,
             # which does not use packs.
             "simulator": simulator_block(
                 alias=_modes.alias_for(
                     "npci", _run_modes.npci,
                     simulator_alias=settings.integration_testing_simulator_alias,
                     application_alias=settings.integration_testing_application_alias),
                 cflow_id=cflow_id, pack_ref=_bound_pack_ref)})
        # Spec calls this `case_data` (cert_test_preparation); `test_data` was the
        # platform's name for it. Prefer the spec key, fall back to the old one so a
        # partner that has not been updated still supplies its values.
        _prep_reply = getattr(setup_msg, "response_body", None) or {}
        test_data_reply = _prep_reply.get("case_data") or _prep_reply.get("test_data") or {}
        flow.fire(_Trg.setup_completed)             # -> SETUP
        flow.fire(_Trg.run_started)                 # -> RUNNING

        # 3) RUN each case, streaming a cert_case_result; a failure triggers the
        #    verdict → waiver_request → waiver_decision exchange.
        from app.services.cert_agent import tasks as _ct
        from app.services.cert_agent.execution import (
            apply_assertion_failures, case_details_payload, is_ready,
            reporter_for, wire_status,
        )
        from app.services.precert_engine import CaseSpec, NfiniteConnector, SimulatorRejected, Unverifiable
        conn = NfiniteConnector(precert_url=settings.precert_engine_precert_url, db=db_cfg)

        # ITA I-6 (§3.4): with the tunnel enabled, partner-initiated cases
        # execute on the PARTNER's side — this side sends the one new
        # certification message, the START SIGNAL, naming exactly those case
        # ids plus the suite deadline and the callback alias, then executes
        # ONLY its own class. Their results arrive asynchronously as
        # bank-reported cert_case_result messages, which replace the
        # not-reported placeholders recorded below (the full join/deadline
        # wait is I-7; until it lands a run that ends first records them
        # honestly as not reported rather than silently passing). With the
        # tunnel off, the legacy behaviour — this side executes every case
        # against the simulator — is unchanged.
        _partner_owned: set[str] = set()
        if settings.integration_testing_enabled:
            _partner_owned = {tc for tc in case_ids
                              if reporter_for(initiator_of.get(tc, "npci")) == "bank"}
        if _partner_owned:
            await _say(_proto.A2ATaskType.CERT_EXECUTION_START, {
                "summary": (f"Begin partner-initiated execution: "
                            f"{len(_partner_owned)} case(s); report each via "
                            f"cert_case_result (reporter=bank)."),
                "case_ids": sorted(_partner_owned),
                "deadline_ms": int(float(settings.integration_testing_ingress_timeout_s) * 1000),
                "simulator_alias": settings.integration_testing_simulator_alias,
                "cert_context": {"cflow_id": cflow_id, "cert_attempt": _round,
                                 "initiator": "bank",
                                 # I-6b/§3.6.3: which end was real, carried to
                                 # the partner so BOTH sides record it.
                                 **_run_modes.as_dict()},
            })

        results: list[dict] = []
        passed = failed = 0
        for tc, cg in scope:
            if tc in _partner_owned:
                # The bank's report will replace this row (see
                # process_cert_case_result_report); until it does, the case is
                # NOT REPORTED — an ERROR-class placeholder, never a pass.
                results.append({"test_case_id": tc, "status": "ERROR",
                                "error_message": "not_reported — partner-initiated "
                                                 "case awaiting the bank's report"})
                continue
            prep = test_data_reply.get(tc) or {}
            _initiator = initiator_of.get(tc, "npci")
            # Spec (cert_test_preparation): `ready` on an authority-initiated case is what
            # PERMITS execution; on a bank-initiated one it is ignored. Absent means
            # execute — a bank that sends no flags (as ours does today) must still
            # certify, so the gate only bites on an explicit false.
            if not is_ready(prep, initiator=_initiator):
                executions = [(None, "SKIP", {"test_case_id": tc, "status": "SKIP",
                                              "error_message": "bank has not declared this case ready"})]
            else:
                # `case_data` minus its control key becomes the request overrides —
                # connector._fire merges them last, so the bank's values win.
                bank_overrides = {k: v for k, v in prep.items() if k != "ready"}
                executions = []
                # §3.1: one execution per VARIANT — a materially different
                # input combination is its own transaction; the assertion rows
                # that share it are free.
                for _variant in (variants_by_case.get(tc) or [None]):
                    overrides = {
                        **({} if _variant is None else
                           {k: str(v) for k, v in (_variant.input_data or {}).items()}),
                        **bank_overrides,
                    }
                    try:
                        r = await asyncio.to_thread(
                            conn.run_case,
                            CaseSpec(psp, subset, settings.precert_engine_cert_name, tc, cg, overrides=overrides))
                        st = ("PASS" if r.verdict.value == "PASSED"
                              else "FAIL" if r.verdict.value == "FAILED" else "SKIP")
                        row = {"test_case_id": tc, "status": st, "expected_resp_code": r.expected.code,
                               "actual_resp_code": r.observed.code, "txn_id": r.txnid}
                        if _variant is not None:
                            # C-2/C-3: grade every stored assertion against the
                            # captured exchange. Assertions only take a case
                            # DOWN (PASS→FAIL), never up — and ONLY failures
                            # travel; a missing capture SKIPs inside the engine
                            # rather than failing the partner for our data.
                            _outcomes = _evaluate_specs(
                                specs_by_variant.get(_variant.id, []),
                                request_body=r.request_payload,
                                response_body=r.response_payload,
                                actual_code=r.observed.code,
                                codec=_codec_for(_variant.wire_format))
                            _fails = _assertion_failures(_outcomes)
                            if _fails:
                                row["assertion_failures"] = _fails
                                st = apply_assertion_failures(st, _fails)
                                row["status"] = st
                    # These two are NOT errors and runner.run_certification has always
                    # classified them as SKIP; only this path lumped them under a bare
                    # `except` and reported ERROR, which read as "something broke" for a
                    # case that simply has no expected result to grade against.
                    except Unverifiable as exc:
                        st, row = "SKIP", {"test_case_id": tc, "status": "SKIP",
                                           "error_message": f"unverifiable: {exc}"}
                    except SimulatorRejected as exc:
                        st, row = "SKIP", {"test_case_id": tc, "status": "SKIP",
                                           "error_message": f"unsupported: {exc}"}
                    except Exception as exc:  # noqa: BLE001 — one bad case must not abort the run
                        st = "ERROR"
                        row = {"test_case_id": tc, "status": st, "error_message": str(exc)}
                    if _variant is not None:
                        row.setdefault("variant_id", _variant.variant_id)
                    executions.append((_variant, st, row))
            for _variant, st, row in executions:
                results.append(row)
                # Spec: cert_case_result is bidirectional — the bank reports its own
                # (initiator="bank") cases. NPCI executes against the simulator, then
                # the partner formally reports the outcome (its reply renders bank→NPCI).
                _reporter = reporter_for(_initiator)
                _wire = wire_status(st)
                _cr_summary = (f"Bank-initiated case {tc} executed against the simulator ({_wire}); awaiting bank report."
                               if _reporter == "bank" else f"Test case {tc}: {_wire}.")
                # Spec-shaped body from the vendored builder, plus the keys the partner
                # handler (`test_case_id`) and the conversation UI (`summary`,
                # expected/actual codes) read. `status` carries the SPEC vocabulary —
                # passed/failed/error — since two values cannot occupy one key; the
                # internal PASS/FAIL/SKIP/ERROR stays on `results` for cert_runs.
                await _say(_proto.A2ATaskType.CERT_CASE_RESULT, {
                    **_ct.cert_case_result(
                        case_id=tc, attempt=_round, reporter=_reporter, status=_wire,
                        details=case_details_payload(row, internal_status=st)),
                    "summary": _cr_summary, "test_case_id": tc,
                    "initiator": _initiator,
                    "expected_code": row.get("expected_resp_code"),
                    "actual_code": row.get("actual_resp_code"),
                })
                if st == "PASS":
                    passed += 1
                    continue
                if st != "FAIL":
                    continue
                failed += 1
                flow.fire(_Trg.case_failed)                  # -> TRIAGE_PENDING
                # C-5: CLASSIFY, then fire the matching triage trigger — the
                # first pass fired triaged_waiver_eligible before choosing, so
                # the phase was already WAIVER_PENDING and the real-defect
                # move was rejected as illegal. A field that broke its own
                # registry constraint is a REAL DEFECT (waiving it would
                # certify the violation) and the verdict carries the WHOLE
                # failure list so one round fixes everything; a response-code-
                # only mismatch stays waiver-eligible (a deployment nuance can
                # legitimately be waived). Demo runs are unaffected: no delta
                # → no specs → no assertion failures → waiver path as before.
                _fails = row.get("assertion_failures") or []
                if _fails:
                    flow.fire(_Trg.triaged_real_defect)      # -> FIX_PENDING
                    await _say(_proto.A2ATaskType.CERT_VERDICT_NOTIFICATION,
                        {"summary": (f"Test case {tc} broke {len(_fails)} registry "
                                     f"constraint(s); defect notice issued — fix and "
                                     f"notify for re-run."),
                         "test_case_id": tc, "classification": "real_defect",
                         "assertion_failures": _fails,
                         "expected_code": row.get("expected_resp_code"),
                         "actual_code": row.get("actual_resp_code")})
                    # The fix arrives later as cert_fix_notification;
                    # `fix_received` (the loop, C-6) returns the flow to
                    # RUNNING. No waiver exchange for a genuine violation.
                else:
                    flow.fire(_Trg.triaged_waiver_eligible)  # -> WAIVER_PENDING
                    v = await _say(_proto.A2ATaskType.CERT_VERDICT_NOTIFICATION,
                        {"summary": f"Test case {tc} is waiver-eligible: the requirement is not applicable to the partner's deployment.",
                         "test_case_id": tc, "classification": "waiver_eligible",
                         "expected_code": row.get("expected_resp_code"), "actual_code": row.get("actual_resp_code")})
                    wreq = getattr(v, "response_body", None) or {}
                    await _say(_proto.A2ATaskType.CERT_WAIVER_DECISION,
                        {"summary": f"Waiver granted for test case {tc}, subject to risk and product sign-off.",
                         "test_case_id": tc, "decision": "granted",
                         # Spec splits these: `category` is the enum the bank waives
                         # under, `reason` its prose. Prefer the enum, fall back to the
                         # older single `reason` field.
                         "reason": wreq.get("category") or wreq.get("reason") or "non_applicable"})
                    flow.fire(_Trg.waiver_granted)           # -> RUNNING

        # ITA-7: with partner-owned cases dispatched, the run is NOT terminal —
        # it awaits their bank-reported results (or the suite deadline). The
        # join (`services/cert_join.py`) owns every terminal action from here:
        # the flow's closing trigger, the run's COMPLETED flip, the assignment
        # flip and the signoff. Everything it needs is persisted.
        _awaiting = bool(_partner_owned)
        if _awaiting:
            summary["awaiting_partner_cases"] = sorted(_partner_owned)
        else:
            # With every failure waived, the run is terminal -> COMPLETED. With a
            # real defect open the flow is at FIX_PENDING and this trigger is HELD
            # (logged, phase kept) — the round honestly ends mid-fix, and
            # `fix_received` on the next fix notification resumes it.
            flow.fire(_Trg.all_cases_passed)
        summary["run_id"] = "ENG-" + cflow_id[-8:]
        summary["passed"] = passed
        summary["failed"] = failed
        summary["overall_state"] = flow.overall_state
        summary["phase_history"] = [e[1] for e in flow.history]

        # Persist cert_runs + cert_test_results — same shape as the cert-agent path.
        _meta = dispatch_meta or {}
        cert_run = CertRun(
            id=generate_uuid(),
            change_request_id=change_id,
            partner_id=partner_id,
            cflow_id=cflow_id,
            run_number=_round,
            # ITA-7: a run awaiting partner results stays RUNNING; the join
            # flips it COMPLETED on the last report or the suite deadline.
            status=CertRunStatus.RUNNING if _awaiting else CertRunStatus.COMPLETED,
            total=len(results),
            passed=summary["passed"],
            failed=summary["failed"],
            started_at=datetime.now(timezone.utc),
            completed_at=None if _awaiting else datetime.now(timezone.utc),
            # I-6b: what was actually on each end of this run.
            npci_mode=_run_modes.npci,
            partner_mode=_run_modes.partner,
            # C-6 round audit: WHO dispatched this round and the chain back to
            # what triggered it. Operator dispatch is the default behaviour.
            dispatched_by=_meta.get("dispatched_by") or "operator",
            previous_run_id=_meta.get("previous_run_id"),
            fix_notification_message_id=_meta.get("fix_notification_message_id"),
            # C-7: the coverage note exactly as this round was built.
            coverage={
                "summary": build.summary(),
                "fallback": build.fallback,
                "uncovered_apis": build.uncovered_apis,
                "unconstrained_fields": build.unconstrained_fields,
                "gaps": build.gaps,
                "variants": len(build.variants),
                "specs": len(build.specs),
            },
        )
        db.add(cert_run)
        db.flush()
        for r in results:
            status_raw = (r.get("status") or "").upper()
            tc_status = (
                CertTestStatus.PASS if status_raw == "PASS"
                else CertTestStatus.FAIL if status_raw == "FAIL"
                # I-6: ERROR is stored as ERROR (the enum always had it) — the
                # not-reported placeholder must be distinguishable from a
                # deliberately-skipped case, both for the bank-report upsert
                # and for the loop's FAIL-only counting.
                else CertTestStatus.ERROR if status_raw == "ERROR"
                else CertTestStatus.SKIP
            )
            _not_reported = (r.get("error_message") or "").startswith("not_reported")
            db.add(CertTestResult(
                id=generate_uuid(),
                cert_run_id=cert_run.id,
                test_case_id=r.get("test_case_id") or "",
                direction=(CertDirection.PARTNER_TO_AUTHORITY if _not_reported
                           else CertDirection.AUTHORITY_TO_PARTNER),
                status=tc_status,
                # ITA-7: the join tells a genuine execution ERROR from a
                # partner case still awaited by this marker; the bank's
                # report overwrites it (process_cert_case_result_report).
                actual_response=({"not_reported": True,
                                  "reason": r.get("error_message")}
                                 if _not_reported else None),
            ))
        if (not _awaiting and assignment
                and summary["failed"] == 0 and summary["passed"] > 0):
            # ITA-7: never certify while partner cases are unreported — a
            # placeholder is not a pass; the join re-decides at finalize.
            set_status(
                assignment, AssignmentStatus.CERTIFIED, db,
                actor_partner_id=partner_id,
                reason=f"Engine cert run {summary['run_id']} all-PASS ({summary['passed']}/{len(results)})",
            )
        db.commit()
        logger.info(
            "precert_engine.done change=%s partner=%s run=%s pass=%d fail=%d",
            change_id, partner_id, summary["run_id"], summary["passed"], summary["failed"],
        )

        # Ship results to the partner over A2A — same wire shape + signoff as the
        # cert-agent path. A wire failure must not roll back the DB writes above.
        # ITA-7: NOT while awaiting partner results — a mid-suite results
        # message with placeholders would read as a verdict; the join sends
        # the terminal notifications at finalize.
        if _awaiting:
            logger.info(
                "precert_engine.awaiting change=%s partner=%s run=%s pending=%d "
                "(suite deadline %ss from start)",
                change_id, partner_id, summary["run_id"], len(_partner_owned),
                settings.cert_suite_deadline_s,
            )
            return summary
        try:
            from app.services.a2a_client import send_task_to_partner

            wire_results = [
                {
                    "test_case_id": r.get("test_case_id"),
                    "status": (r.get("status") or "").upper(),
                    "txn_id": r.get("txn_id"),
                    "expected_code": r.get("expected_resp_code"),
                    "actual_code": r.get("actual_resp_code"),
                    "error_message": r.get("error_message"),
                    "initiated_by": "NPCI",
                }
                for r in results
            ]
            completed_at_iso = datetime.now(timezone.utc).isoformat()
            results_payload = {
                "cert_run_id": cert_run.id,
                "external_run_id": summary["run_id"],
                "feature_name": change.title or "",
                "flow": _flow_code, "bank_id": bank_id, "role": role,
                "total": len(results), "passed": summary["passed"], "failed": summary["failed"],
                "completed_at": completed_at_iso,
                "results": wire_results,
            }
            if _demo_delay:
                await asyncio.sleep(_demo_delay)
            await send_task_to_partner(
                partner=partner,
                task_type=A2ATaskType.CERT_TEST_RESPONSE,
                payload=results_payload,
                db=db,
                change_request_id=change_id,
                cflow_id=cert_run.cflow_id,
                cert_attempt=cert_run.run_number,
            )
            db.commit()

            all_passed = (
                summary["failed"] == 0 and summary["passed"] > 0
                and summary["passed"] == len(results)
            )
            if all_passed:
                signoff_at = datetime.now(timezone.utc)
                from datetime import timedelta as _td
                valid_until = signoff_at + _td(days=365)
                signoff_at_iso = signoff_at.isoformat()
                signoff_payload = {
                    "cert_run_id": cert_run.id,
                    "external_run_id": summary["run_id"],
                    "feature_name": change.title or "",
                    "flow": flow, "bank_id": bank_id, "role": role,
                    "total": len(results), "passed": summary["passed"],
                    "completed_at": completed_at_iso,
                    "signoff_at": signoff_at_iso,
                    "valid_until": valid_until.date().isoformat(),
                    "results": wire_results,
                    "signoff_message": (
                        f"Certification of '{change.title or flow}' has been completed successfully. "
                        f"{summary['passed']} of {len(results)} test cases passed."
                    ),
                }
                try:
                    import base64 as _b64
                    import hashlib as _hashlib
                    from app.services.cert_signoff_doc import build_signoff_docx

                    doc_meta = _build_signoff_meta(
                        partner, change, flow=flow, role=role,
                        run_id=summary["run_id"], passed=summary["passed"],
                        total=len(results), signoff_at=signoff_at_iso,
                        modes=_run_modes,
                    )
                    doc_results = [
                        {"test_id": w.get("test_case_id"), "txn_id": w.get("txn_id"),
                         "date": signoff_at_iso.split("T")[0], "status": w.get("status")}
                        for w in wire_results
                    ]
                    raw = build_signoff_docx(doc_meta, doc_results)
                    signoff_payload["signoff_docx_b64"] = _b64.b64encode(raw).decode()
                    signoff_payload["signoff_filename"] = (
                        f"Certification_Result_{bank_id}_{summary['run_id']}.docx"
                    )
                    signoff_payload["signoff_sha256"] = _hashlib.sha256(raw).hexdigest()
                    signoff_payload["signoff_size_bytes"] = len(raw)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("precert_engine.signoff_doc_failed change=%s err=%s", change_id, exc)
                try:
                    if _demo_delay:
                        await asyncio.sleep(_demo_delay)
                    signoff_msg = await send_task_to_partner(
                        partner=partner,
                        task_type=_proto.A2ATaskType.CERT_SIGNOFF_NOTIFICATION,
                        payload=signoff_payload,
                        db=db,
                        change_request_id=change_id,
                        cflow_id=cert_run.cflow_id,
                        cert_attempt=cert_run.run_number,
                    )
                    if signoff_msg is not None and signoff_msg.status == "delivered":
                        cert_run.completion_signed_off_at = datetime.now(timezone.utc)
                    db.commit()
                    logger.info(
                        "precert_engine.signoff_sent change=%s partner=%s run=%s delivered=%s",
                        change_id, partner_id, summary["run_id"],
                        signoff_msg.status if signoff_msg else "none",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("precert_engine.signoff_send_failed change=%s err=%s", change_id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "precert_engine.results_send_failed change=%s partner=%s err=%s",
                change_id, partner_id, exc,
            )

        return summary
    except Exception as exc:  # noqa: BLE001
        logger.exception("precert_engine.error change=%s partner=%s", change_id, partner_id)
        summary["skipped"] = True
        summary["skip_reason"] = f"engine orchestration error: {exc}"
        try:
            db.rollback()
        except Exception:
            pass
        return summary
    finally:
        db.close()
