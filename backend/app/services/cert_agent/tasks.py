# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Spec-shaped payload builders for the 14 A2A certification-lifecycle messages
(AtOM A2A Protocol Specification v1.0, Part B).

Each builder is a pure ``dict``-returning function so it is trivially testable
without the app graph. Field shapes follow the PDF §Part B payloads exactly;
optional/uncaptured fields default to ``None``/``[]`` — never fabricated.

Direction legend: A2P = Authority→Partner, P2A = Partner→Authority, E = Either.

── Canonical ─────────────────────────────────────────────────────────────────
THIS MODULE IS THE SOURCE OF TRUTH for the 14 builders. It began as a copy from
a standalone cert-agent prototype of the Authority's cert-orchestrator
role; that service was never wired into the platform (no compose service, no
caller, and its REST surface did not even match the paths this backend calls)
and has been removed from the repository. The shapes were the valuable half and
they live here.

``app/a2a_common/cert_tasks.py`` holds a second copy for the admin cert-trigger
surface. The two are currently identical in their 14 task-type constants and
must stay so. **Nothing enforces that** — it is not in ``packages/a2a-core/MANIFEST``
and ``tests/services/test_cert_agent_tasks.py`` only asserts the count is 14,
never that the copies agree. Changing a builder here means changing it there in
the same commit.

WHY THIS MATTERS: ``cert_orchestrator.orchestrate_cert_run_precert_engine``
currently hand-rolls its cert payloads, and they diverge from the spec in ways
``docs/A2A_spec_reconciliation.md`` never caught — that log has 16 sections and
every one is Part A. Known divergences the builders here fix:

  * ``cert_setup_notification`` omits the ``simulator`` block entirely, so the
    bank is never told where to send bank-initiated cases (spec: "URL the bank's
    stack calls for bank-initiated cases").
  * ``cert_case_result`` sends ``PASS``/``FAIL``/``SKIP``/``ERROR``; the spec
    vocabulary is ``passed``/``failed``/``error`` and has no SKIP at all.
  * ``attempt`` is always 1, and ``details`` (payload refs, latency_ms,
    executed_at) is not sent.

NOTHING CALLS THIS YET. Phase 1 vendors + tests the layer; the cert path is
rewritten around it in a later phase. Keep this file a pure mirror of the
prototype's shapes — behaviour belongs in the caller, not here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# ── task-type constants ───────────────────────────────────────────────────────
CERT_CONFIG_REQUEST       = "cert_config_request"        # A2P
CERT_CONFIG_SUBMISSION    = "cert_config_submission"     # P2A
CERT_SETUP_NOTIFICATION   = "cert_setup_notification"    # N2B
CERT_TEST_PREPARATION     = "cert_test_preparation"      # P2A
CERT_CASE_RESULT          = "cert_case_result"           # E
CERT_VERDICT_NOTIFICATION = "cert_verdict_notification"  # N2B
CERT_VERDICT_DISPUTE      = "cert_verdict_dispute"       # P2A
CERT_WAIVER_REQUEST       = "cert_waiver_request"        # P2A
CERT_WAIVER_DECISION      = "cert_waiver_decision"       # A2P
CERT_FIX_NOTIFICATION     = "cert_fix_notification"      # P2A
CERT_SIGNOFF_NOTIFICATION = "cert_signoff_notification"  # N2B
CERT_STATUS_REQUEST       = "cert_status_request"        # E
CERT_STATUS_REPORT        = "cert_status_report"         # E
CERT_RUN_ABORT            = "cert_run_abort"             # E

# Direction map — used by the executor to reject wrong-direction inbound.
AUTHORITY_TO_PARTNER = {
    CERT_CONFIG_REQUEST, CERT_SETUP_NOTIFICATION, CERT_VERDICT_NOTIFICATION,
    CERT_WAIVER_DECISION, CERT_SIGNOFF_NOTIFICATION,
}
PARTNER_TO_AUTHORITY = {
    CERT_CONFIG_SUBMISSION, CERT_TEST_PREPARATION, CERT_VERDICT_DISPUTE,
    CERT_WAIVER_REQUEST, CERT_FIX_NOTIFICATION,
}
EITHER = {CERT_CASE_RESULT, CERT_STATUS_REQUEST, CERT_STATUS_REPORT, CERT_RUN_ABORT}

ALL_CERT_TASKS = AUTHORITY_TO_PARTNER | PARTNER_TO_AUTHORITY | EITHER

# overall_state enum for cert_status_report (spec).
OVERALL_STATES = (
    "NOT_STARTED", "CONFIG_REQUESTED", "CONFIG_RECEIVED", "SETUP", "RUNNING",
    "TRIAGE_PENDING", "WAIVER_PENDING", "FIX_PENDING", "DISPUTE_PENDING",
    "COMPLETED", "ABORTED",
)

# The config fields the Authority asks the bank to populate (spec §cert_config_request).
DEFAULT_REQUIRED_FIELDS = [
    "bank_identity.nbin", "bank_identity.ifsc", "bank_identity.iin",
    "bank_identity.participant_code", "bank_identity.handle", "bank_identity.acquirer_id",
    "network.host", "network.port", "network.egress_cidrs",
    "security.ssl_signer.cert_ref", "security.ssl_signer.fingerprint_sha256",
    "security.ssl_client_cert.cert_ref", "security.ssl_client_cert.fingerprint_sha256",
    "security.hsm_certificate.cert_ref", "security.hsm_certificate.fingerprint_sha256",
    "roles", "requested_subset",
]
DEFAULT_OPTIONAL_FIELDS = ["supported_features", "contacts", "preferred_window"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Authority → Bank ──────────────────────────────────────────────────────────

def cert_config_request(
    *,
    mode: str = "initial",
    respond_by_days: int = 3,
    schema_version: str = "v1.0",
    required_fields: list[str] | None = None,
    optional_fields: list[str] | None = None,
    instructions: str = (
        "Upload SSL client cert, signer CA, and HSM cert to Cflow before responding."
    ),
) -> dict:
    """The Authority requests the bank's certification configuration (§cert_config_request)."""
    return {
        "mode": mode,                                   # initial | amendment
        "respond_by": (_now() + timedelta(days=respond_by_days)).isoformat(),
        "schema_version": schema_version,
        "required_fields": list(required_fields or DEFAULT_REQUIRED_FIELDS),
        "optional_fields": list(optional_fields or DEFAULT_OPTIONAL_FIELDS),
        "instructions": instructions,
    }


def cert_setup_notification(
    *,
    simulator: dict,
    suite_version: str,
    subset: str,
    case_list: list[dict],
) -> dict:
    """The Authority validated config, provisioned the simulator, mapped the suite
    (§cert_setup_notification). ``simulator`` = {endpoint, protocol_version,
    credentials_ref}; each ``case_list`` item = {case_id, sheet, initiator, api,
    scope, expected_status, authority_batch}."""
    return {
        "simulator": simulator,
        "suite_version": suite_version,
        "subset": subset,
        "case_list": list(case_list or []),
    }


def cert_verdict_notification(
    *,
    case_id: str,
    attempt: int,
    verdict: str,                    # real_defect | not_defect | waiver_eligible
    reasoning: str,
    evidence_refs: list[str] | None = None,
    spec_references: list[dict] | None = None,
    human_approved: bool = True,
    approver: dict | None = None,
) -> dict:
    """The Authority's triage classifies a failed result (§cert_verdict_notification)."""
    return {
        "case_id": case_id,
        "attempt": attempt,
        "verdict": verdict,
        "reasoning": reasoning,
        "evidence_refs": list(evidence_refs or []),
        "spec_references": list(spec_references or []),
        "human_approved": human_approved,
        "approver": approver,
    }


def cert_waiver_decision(
    *,
    case_id: str,
    decision: str,                   # granted | rejected
    approvers: list[dict] | None = None,
    conditions: str | None = None,
    valid_until: str | None = None,
) -> dict:
    """Risk + Product decision on a waiver request (§cert_waiver_decision)."""
    return {
        "case_id": case_id,
        "decision": decision,
        "approvers": list(approvers or []),
        "conditions": conditions,
        "valid_until": valid_until,
    }


def cert_signoff_notification(
    *,
    documents: list[dict],
    suite_version: str,
    subset: str,
    case_outcomes: dict,
    waived_case_ids: list[str] | None = None,
    signatory: dict | None = None,
    valid_until: str | None = None,
) -> dict:
    """Every case terminal, no open real_defect → the Authority issues sign-off
    (§cert_signoff_notification). ``case_outcomes`` = {total, passed, waived, failed}."""
    return {
        "documents": list(documents or []),
        "issued_at": _now().isoformat(),
        "valid_until": valid_until,
        "suite_version": suite_version,
        "subset": subset,
        "case_outcomes": case_outcomes,
        "waived_case_ids": list(waived_case_ids or []),
        "signatory": signatory,
    }


# ── Bank → Authority (builders provided for tests / simulating the bank) ──────

def cert_config_submission(
    *,
    bank_identity: dict,
    network: dict,
    security: dict,
    roles: list[str],
    requested_subset: str,
    supported_protocol_versions: list[str] | None = None,
    supported_features: list[str] | None = None,
    contacts: list[dict] | None = None,
    preferred_window: dict | None = None,
    renews_cflow_id: str | None = None,
) -> dict:
    """Bank responds with its configuration (§cert_config_submission).
    Certificate bodies are never inline — only cert_ref + fingerprint."""
    return {
        "renews_cflow_id": renews_cflow_id,
        "bank_identity": bank_identity,
        "network": network,
        "security": security,
        "roles": list(roles or []),
        "supported_protocol_versions": list(supported_protocol_versions or []),
        "supported_features": list(supported_features or []),
        "requested_subset": requested_subset,
        "contacts": list(contacts or []),
        "preferred_window": preferred_window,
    }


def cert_test_preparation(*, case_data: dict) -> dict:
    """Bank declares per-case data + readiness (§cert_test_preparation).
    Incremental: re-fire only changed cases; the receiver merges."""
    return {"case_data": dict(case_data or {})}


def cert_verdict_dispute(
    *,
    case_id: str,
    attempt: int,
    disputed_verdict: str,
    bank_position: str,
    evidence_refs: list[str] | None = None,
    requested_action: str = "re_triage",
) -> dict:
    """Bank disagrees with a verdict (§cert_verdict_dispute)."""
    return {
        "case_id": case_id,
        "attempt": attempt,
        "disputed_verdict": disputed_verdict,
        "bank_position": bank_position,
        "evidence_refs": list(evidence_refs or []),
        "requested_action": requested_action,
    }


def cert_waiver_request(
    *,
    case_id: str,
    category: str,                   # non_applicable | deferred | infeasible | policy
    reason: str,
    evidence_refs: list[str] | None = None,
    requested_by: dict | None = None,
) -> dict:
    """Bank cannot/will not pass a case in this release (§cert_waiver_request)."""
    return {
        "case_id": case_id,
        "category": category,
        "reason": reason,
        "evidence_refs": list(evidence_refs or []),
        "requested_by": requested_by,
    }


def cert_fix_notification(
    *,
    fixed_case_ids: list[str],
    fix_summary: str,
    change_refs: list[str] | None = None,
    verdict_refs: list[str] | None = None,
    ready_for_rerun: bool = True,
) -> dict:
    """Bank resolved real_defect verdicts; requests re-run (§cert_fix_notification)."""
    return {
        "fixed_case_ids": list(fixed_case_ids or []),
        "fix_summary": fix_summary,
        "change_refs": list(change_refs or []),
        "verdict_refs": list(verdict_refs or []),
        "ready_for_rerun": ready_for_rerun,
    }


# ── Either direction ──────────────────────────────────────────────────────────

def cert_case_result(
    *,
    case_id: str,
    attempt: int,
    reporter: str,                   # bank | npci
    status: str,                     # passed | failed | error
    details: dict | None = None,
) -> dict:
    """The executing side reports a test-case outcome (§cert_case_result)."""
    return {
        "case_id": case_id,
        "attempt": attempt,
        "reporter": reporter,
        "status": status,
        "details": details or {},
    }


def cert_status_request(
    *,
    scope: str = "full",             # full | summary | case | pending
    filters: dict | None = None,
) -> dict:
    """Poll the other side for current cert state (§cert_status_request)."""
    return {
        "scope": scope,
        "filters": filters or {"case_ids": [], "states": []},
    }


def cert_status_report(
    *,
    overall_state: str,
    stage: str,
    counts: dict,
    per_case: list[dict] | None = None,
    blocked_on: list[dict] | None = None,
    next_expected_action: dict | None = None,
    in_reply_to: str | None = None,
) -> dict:
    """Snapshot of cert state — reply to a request or unsolicited push
    (§cert_status_report)."""
    return {
        "in_reply_to": in_reply_to,
        "snapshot_at": _now().isoformat(),
        "overall_state": overall_state,
        "stage": stage,
        "counts": counts,
        "per_case": list(per_case or []),
        "blocked_on": list(blocked_on or []),
        "next_expected_action": next_expected_action,
    }


def cert_run_abort(
    *,
    reason: str,
    category: str,                   # subset_mismatch | environment_failure | ...
    initiated_by: dict | None = None,
) -> dict:
    """Either side cancels the cert run — terminal (§cert_run_abort)."""
    return {
        "reason": reason,
        "category": category,
        "initiated_by": initiated_by,
    }
