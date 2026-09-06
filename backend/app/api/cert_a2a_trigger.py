# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin surface for firing Part B (certification) A2A messages by hand.

Internal testing tool. Two modes, both AdminUser-gated:

  * **send**             — NPCI originates a cert message. Target is either a
                           registered partner (uses its stored endpoint + secrets,
                           so JWT + HMAC are exercised) or an arbitrary base URL
                           for a locally-run cert stack.
  * **simulate-inbound** — NPCI plays the *bank* and posts a Bank→NPCI message at
                           its own `/a2a-rpc/rpc`. Goes through the real ingress
                           (HMAC -> JWT -> session -> partner status -> executor),
                           which is the point: it tests the receive path, not a
                           handler called in isolation.

Payload templates come from `a2a_common.cert_tasks` (the vendored spec builders),
so the operator edits a conformant shape rather than typing one from scratch.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.a2a_common import cert_tasks as T
from app.a2a_common.protocol import (
    CERT_ORCHESTRATOR_AGENT_ID,
    SENDER_AUTHORITY,
    A2ATaskType as ProtoTaskType,
    make_envelope,
)
from app.core.deps import AdminUser, DbDep
from app.core.security import A2A_ACCESS_TOKEN_TTL_S, create_partner_token
from app.models.base import generate_uuid, utcnow
from app.models.phase_c import A2ASession, PartnerAgent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/cert-a2a", tags=["cert-a2a"])


# Placeholder arguments per task type. Enough to produce a valid, obviously-fake
# payload the operator edits before sending — never plausible-looking real data.
# The example case id and role derive from the ACTIVE domain pack's cert
# vocabulary (genericisation sweep) — a library-network deployment shows its
# own role/prefix here, not a payments bank's.
from app.core.domain.contract import cert_vocabulary_of as _cv_of
from app.core.domain.registry import get_active_pack as _gap

_vocab = _cv_of(_gap())
_EX_ROLE = next(iter(_vocab.role_prefixes), "ROLE_A")
_EX_CASE = f"{_vocab.role_prefixes.get(_EX_ROLE, 'TC_')}1"

_TEMPLATE_ARGS: dict[str, dict[str, Any]] = {
    T.CERT_CONFIG_REQUEST: {},
    T.CERT_SETUP_NOTIFICATION: {
        "simulator": {"endpoint": "https://cert-sim.authority.example/rpc",
                      "protocol_version": "1.0", "credentials_ref": "cflow://creds/REPLACE"},
        "suite_version": "REPLACE", "subset": "Subset-A",
        "case_list": [{"case_id": _EX_CASE, "sheet": "REPLACE", "initiator": "npci",
                       "api": "REPLACE", "scope": "REPLACE",
                       "expected_status": "SUCCESS", "authority_batch": {}}],
    },
    T.CERT_VERDICT_NOTIFICATION: {
        "case_id": _EX_CASE, "attempt": 1, "verdict": "real_defect",
        "reasoning": "REPLACE", "approver": None,
    },
    T.CERT_WAIVER_DECISION: {"case_id": _EX_CASE, "decision": "granted"},
    T.CERT_SIGNOFF_NOTIFICATION: {
        "documents": [], "suite_version": "REPLACE", "subset": "Subset-A",
        "case_outcomes": {"total": 0, "passed": 0, "waived": 0, "failed": 0},
    },
    T.CERT_CONFIG_SUBMISSION: {
        "bank_identity": {"bank_name": "REPLACE", "nbin": "REPLACE", "ifsc": "REPLACE"},
        "network": {"host": "REPLACE", "port": 443},
        "security": {"tls_tier": "mtls"},
        "roles": [_EX_ROLE], "requested_subset": "Subset-A",
    },
    T.CERT_TEST_PREPARATION: {"case_data": {_EX_CASE: {"ready": True}}},
    T.CERT_VERDICT_DISPUTE: {
        "case_id": _EX_CASE, "attempt": 1, "disputed_verdict": "real_defect",
        "bank_position": "REPLACE",
    },
    T.CERT_WAIVER_REQUEST: {"case_id": _EX_CASE, "category": "infeasible", "reason": "REPLACE"},
    T.CERT_FIX_NOTIFICATION: {"fixed_case_ids": [_EX_CASE], "fix_summary": "REPLACE"},
    T.CERT_CASE_RESULT: {"case_id": _EX_CASE, "attempt": 1, "reporter": "npci", "status": "passed"},
    T.CERT_STATUS_REQUEST: {},
    T.CERT_STATUS_REPORT: {
        "overall_state": "RUNNING", "stage": "REPLACE",
        "counts": {"total": 0, "passed": 0, "failed": 0, "pending": 0},
    },
    T.CERT_RUN_ABORT: {"reason": "REPLACE", "category": "other"},
}

_BUILDERS = {
    T.CERT_CONFIG_REQUEST:       T.cert_config_request,
    T.CERT_SETUP_NOTIFICATION:   T.cert_setup_notification,
    T.CERT_VERDICT_NOTIFICATION: T.cert_verdict_notification,
    T.CERT_WAIVER_DECISION:      T.cert_waiver_decision,
    T.CERT_SIGNOFF_NOTIFICATION: T.cert_signoff_notification,
    T.CERT_CONFIG_SUBMISSION:    T.cert_config_submission,
    T.CERT_TEST_PREPARATION:     T.cert_test_preparation,
    T.CERT_VERDICT_DISPUTE:      T.cert_verdict_dispute,
    T.CERT_WAIVER_REQUEST:       T.cert_waiver_request,
    T.CERT_FIX_NOTIFICATION:     T.cert_fix_notification,
    T.CERT_CASE_RESULT:          T.cert_case_result,
    T.CERT_STATUS_REQUEST:       T.cert_status_request,
    T.CERT_STATUS_REPORT:        T.cert_status_report,
    T.CERT_RUN_ABORT:            T.cert_run_abort,
}


def _direction(task_type: str) -> str:
    if task_type in T.AUTHORITY_TO_PARTNER:
        return "npci_to_bank"
    if task_type in T.PARTNER_TO_AUTHORITY:
        return "bank_to_npci"
    return "either"


@router.get("/templates")
def cert_templates(_: AdminUser):
    """Spec-shaped starter payload for each of the 14 Part B messages."""
    out = []
    for tt in sorted(T.ALL_CERT_TASKS):
        direction = _direction(tt)
        out.append({
            "task_type": tt,
            "direction": direction,
            # NPCI can originate everything except the bank-only messages; those
            # are reachable through simulate-inbound instead.
            "sendable": direction in ("npci_to_bank", "either"),
            "payload": _BUILDERS[tt](**_TEMPLATE_ARGS[tt]),
        })
    return {"templates": out}


class CertSendRequest(BaseModel):
    task_type: str
    cflow_id: str
    cert_attempt: int = 1
    payload: dict
    partner_id: Optional[str] = None     # mode (a) — registered partner
    endpoint_url: Optional[str] = None   # mode (b) — arbitrary base URL
    # Skips agent-card discovery. Needed more often than not for local targets:
    # a stack may serve no card at all, or publish one naming `localhost`, which
    # from inside this container resolves to the backend rather than the target.
    rpc_url: Optional[str] = None
    change_id: Optional[str] = None


@router.post("/send")
async def cert_send(body: CertSendRequest, db: DbDep, _: AdminUser):
    """Fire an authority-originated cert message at a partner or a raw endpoint."""
    if body.task_type not in T.ALL_CERT_TASKS:
        raise HTTPException(status_code=400, detail=f"Unknown cert task_type '{body.task_type}'")
    if body.task_type in T.PARTNER_TO_AUTHORITY:
        raise HTTPException(
            status_code=400,
            detail=f"'{body.task_type}' is Bank→NPCI — use /simulate-inbound instead.",
        )
    if bool(body.partner_id) == bool(body.endpoint_url):
        raise HTTPException(status_code=400, detail="Provide exactly one of partner_id or endpoint_url.")

    # Partner mode — reuse the production sender so JWT minting, HMAC signing,
    # retry bookkeeping and the audit row all behave exactly as in a real run.
    if body.partner_id:
        from app.services.a2a_client import send_task_to_partner

        partner = db.get(PartnerAgent, body.partner_id)
        if not partner:
            raise HTTPException(status_code=404, detail="Partner not found")
        # Must be the PROTOCOL enum, not the models one: the models `A2ATaskType`
        # predates Part B and carries none of the 14 cert types, and the sender
        # calls `.value` on whatever it is handed. Same pattern as cert_orchestrator.
        msg = await send_task_to_partner(
            partner=partner,
            task_type=ProtoTaskType(body.task_type),
            payload=body.payload,
            db=db,
            change_request_id=body.change_id,
            cflow_id=body.cflow_id,
            cert_attempt=body.cert_attempt,
            agent_id=CERT_ORCHESTRATOR_AGENT_ID,
        )
        return {
            "mode": "partner", "message_id": msg.id, "status": msg.status,
            "error_code": msg.error_code, "task_state": msg.task_state,
            "envelope": msg.payload,
        }

    # Raw-endpoint mode — no partner row, so no secrets and no audit row. This is
    # the "point it at my local cert stack" path; unsigned by design.
    from app.a2a_common.client import send_a2a_message

    mid = generate_uuid()
    envelope = make_envelope(
        body.task_type,
        message_id=mid,
        from_=SENDER_AUTHORITY,
        payload=body.payload,
        change_id=body.change_id,
        cflow_id=body.cflow_id,
        cert_attempt=body.cert_attempt,
        agent_id=CERT_ORCHESTRATOR_AGENT_ID,
        timestamp=utcnow().isoformat(),
    )
    base_url = body.endpoint_url.rstrip("/")

    # This route deliberately posts to an operator-supplied host — that is the
    # feature ("point it at my local cert stack") — so the target is not
    # restricted the way the partner registry is. Two INDEPENDENT guards run
    # over both the endpoint and the rpc override, since either can direct
    # traffic:
    #
    #   1. SSRF (ssrf_guard_mode) — can this URL reach somewhere it shouldn't?
    #   2. Cleartext (cleartext_policy_mode) — would it put plaintext on a
    #      network we don't control?
    #
    # They are deliberately separate settings: the answers point in opposite
    # directions (SSRF distrusts private targets, the cleartext rule permits
    # only private ones for http://), so one switch could not express both.
    from app.core.config import settings as _settings
    from app.core.ssrf_guard import (
        ClearTextBlocked, SsrfBlocked, check_cleartext_url, check_outbound_url,
        parse_allowlist,
    )
    _allow = parse_allowlist(_settings.ssrf_allowed_internal_hosts)
    _cleartext_allow = parse_allowlist(_settings.cleartext_allowed_hosts)
    for _label, _target in (("endpoint_url", base_url), ("rpc_url", body.rpc_url)):
        if not _target:
            continue
        try:
            check_outbound_url(_target, mode=_settings.ssrf_guard_mode, allowlist=_allow,
                               allow_private=_settings.ssrf_allow_private_networks,
                               context=f"cert_a2a_trigger {_label}")
        except SsrfBlocked as exc:
            return {"mode": "endpoint", "message_id": mid, "status": "refused",
                    "error_code": "SsrfBlocked", "target": _target,
                    "detail": (f"{_label} refused: {exc.reason}. Add the host to "
                               "SSRF_ALLOWED_INTERNAL_HOSTS if this is intended."),
                    "envelope": envelope}
        # Cleartext policy — a SEPARATE decision from SSRF above (see
        # app.core.ssrf_guard). This route posts operator-supplied certification
        # envelopes, so an `http://` target that is not loopback/RFC-1918 would
        # put UPI test transaction data on a network we do not control. Unlike
        # the SSRF check this one ENFORCES by default; the docker workflow is
        # unaffected because `cert-agent` resolves to a private address.
        try:
            check_cleartext_url(_target, mode=_settings.cleartext_policy_mode,
                                allowlist=_cleartext_allow,
                                context=f"cert_a2a_trigger {_label}")
        except ClearTextBlocked as exc:
            return {"mode": "endpoint", "message_id": mid, "status": "refused",
                    "error_code": "ClearTextBlocked", "target": _target,
                    "detail": (f"{_label} refused: {exc.reason}. Use an https:// URL, or add "
                               "the host to CLEARTEXT_ALLOWED_HOSTS if the link is trusted."),
                    "envelope": envelope}

    try:
        await send_a2a_message(
            base_url=base_url, context_id=body.cflow_id, task_id=mid, data=envelope,
            rpc_url=body.rpc_url or None,
        )
        return {"mode": "endpoint", "message_id": mid, "status": "delivered",
                "error_code": None, "target": body.rpc_url or base_url, "envelope": envelope}
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator verbatim
        logger.warning("cert_a2a_trigger send failed target=%s type=%s: %r",
                       base_url, body.task_type, exc)
        return {"mode": "endpoint", "message_id": mid, "status": "delivery_failed",
                "error_code": type(exc).__name__, "target": base_url,
                "detail": str(exc)[:500], "envelope": envelope}


class CertSimulateInboundRequest(BaseModel):
    task_type: str
    cflow_id: str
    cert_attempt: int = 1
    payload: dict
    partner_id: str            # the bank we impersonate
    change_id: Optional[str] = None


@router.post("/simulate-inbound")
async def cert_simulate_inbound(body: CertSimulateInboundRequest, db: DbDep, _: AdminUser):
    """Post a Bank→NPCI cert message at our own ingress, as the named partner.

    Mints a real A2A session for that partner rather than requiring its plaintext
    api_key (which is nulled after issue), then sends over the SDK client so the
    HMAC + JWT middleware and the executor all run for real.
    """
    if body.task_type not in T.ALL_CERT_TASKS:
        raise HTTPException(status_code=400, detail=f"Unknown cert task_type '{body.task_type}'")
    if body.task_type in T.AUTHORITY_TO_PARTNER:
        raise HTTPException(
            status_code=400,
            detail=f"'{body.task_type}' is NPCI→Bank — use /send instead.",
        )

    partner = db.get(PartnerAgent, body.partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    token = create_partner_token(partner.id)
    db.add(A2ASession(
        id=generate_uuid(),
        partner_id=partner.id,
        jwt_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=utcnow() + timedelta(seconds=A2A_ACCESS_TOKEN_TTL_S),
        created_at=utcnow(),
    ))
    db.commit()

    from app.a2a_common.client import send_a2a_message

    mid = generate_uuid()
    envelope = make_envelope(
        body.task_type,
        message_id=mid,
        from_=f"bank-{partner.id}",
        payload=body.payload,
        change_id=body.change_id,
        cflow_id=body.cflow_id,
        cert_attempt=body.cert_attempt,
        agent_id="bank.cert_agent.v1",
        timestamp=utcnow().isoformat(),
    )
    try:
        await send_a2a_message(
            base_url="http://localhost:8000",   # our own ingress, from inside this container
            context_id=body.cflow_id,
            task_id=mid,
            data=envelope,
            auth_header=f"Bearer {token}",
            hmac_secret=partner.signing_secret,
        )
        return {"mode": "simulate_inbound", "message_id": mid, "status": "accepted",
                "partner": partner.name, "envelope": envelope}
    except Exception as exc:  # noqa: BLE001
        logger.warning("cert_a2a_trigger simulate-inbound failed type=%s partner=%s: %r",
                       body.task_type, partner.id, exc)
        return {"mode": "simulate_inbound", "message_id": mid, "status": "rejected",
                "error_code": type(exc).__name__, "detail": str(exc)[:500],
                "partner": partner.name, "envelope": envelope}
