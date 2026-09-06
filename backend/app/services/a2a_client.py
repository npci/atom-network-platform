# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2A Client — Send Tasks from the Authority Platform to partner agents.

Slice 8 of the unified A2A SDK refactor. The legacy hand-rolled POST
wire is gone — every outbound call now goes through the SDK's JSON-RPC
`tasks/send` (`app.a2a_common.client.send_a2a_message`).

`partner_agents.protocol_version` is retained on the row for audit /
analysis (so dashboards can show which wire each row was delivered on,
once historical legacy rows are queried), but the dispatcher no longer
branches on it.

Auth model (post Slice 3 of A2A security hardening):

  cert_engine partner — Bearer JWT obtained via the `/a2a/auth`
                        handshake (signed with the platform-wide
                        `settings.secret_key`). Cache lives in
                        `app.a2a_common.auth`. The cert-agent's
                        CertAgentAuthMiddleware (Slice 2.5) verifies.

  Every other partner with `jwt_signing_secret` set — Bearer JWT
                        minted by `create_partner_outbound_token` and
                        signed with that partner's per-partner secret.
                        The partner-side auth middleware verifies with
                        the symmetric value stored as
                        the partner's stored JWT-signing secret.

  Partners with `jwt_signing_secret` NULL — no Authorization header
                        attached. Back-compat for partners that have
                        not yet onboarded the per-partner secret (the
                        partner-side middleware accepts unsigned calls
                        until the partner deploys Slice 3 inbound
                        validation).
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.a2a_common.auth import fetch_bearer_jwt
from app.a2a_common.client import send_a2a_message
from app.a2a_common.protocol import SENDER_AUTHORITY, make_envelope
from app.core.security import create_partner_outbound_token
from app.services.change_communication_wire import to_wire_v1_1
from app.models.base import generate_uuid, utcnow
from app.models.phase_c import (
    A2ADirection, A2AMessage, A2ATaskType, PartnerAgent,
)

logger = logging.getLogger(__name__)

# T3 (THREAT_MODEL.md) — partner IDs for which the tls_verification_disabled
# security event has already been logged this process lifetime, so repeated
# calls to a partner with ssl_verify=False don't flood the log. Process-
# local by design (matches SdkHmacMiddleware's `_warned_no_secret` pattern):
# a restart re-warns once, which is the correct behavior for a condition an
# operator should be able to rediscover after a deploy, not permanently
# suppress.
_warned_tls_verify_disabled: set[str] = set()


# ── Delivery retry policy ────────────────────────────────────────────────────
# A send used to be one-shot: any failure meant the partner never got the message and
# nothing retried. Failures are now scheduled for retry with exponential backoff, swept by
# `a2a.retry_failed_deliveries` (see app/services/celery_tasks.py).
MAX_DELIVERY_ATTEMPTS = 5
# Backoff per attempt number (minutes): 1 → 5 → 15 → 60. Index is attempts-1, clamped.
_RETRY_BACKOFF_MINUTES = (1, 5, 15, 60)


def _error_code_for(exc: Exception) -> str:
    """Best triage label for a failed send, ≤40 chars (the column width).

    An HTTP error becomes `http_503`; anything else falls back to the exception class.
    Distinguishing 4xx from 5xx matters: 5xx/timeouts are worth retrying, a 401 is not.

    The status has to be dug out of three shapes, because the A2A SDK does NOT expose a
    `.response`: it raises `A2AClientError(message, data)` with the code only in the text
    ("HTTP Error 401: Client error '401 Unauthorized' for url …"). Relying on `.response`
    alone silently mislabelled every SDK HTTP failure as its class name — which
    `is_retryable()` then treated as transient, so genuine 401s were retried.
    """
    # 1. httpx / requests style — exc.response.status_code
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(code, int):
        return f"http_{code}"[:40]
    # 2. a plain .status_code on the exception
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return f"http_{code}"[:40]
    # 3. the A2A SDK's data dict, when it carries one
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        for k in ("status_code", "status", "code"):
            v = data.get(k)
            if isinstance(v, int) and 100 <= v <= 599:
                return f"http_{v}"[:40]
    # 4. last resort — parse the message. Anchor on an explicit marker first so a port
    #    or id in the URL can't be mistaken for a status.
    import re as _re
    text = str(exc)
    m = _re.search(r"(?:HTTP\s+Error|status(?:\s+code)?)[\s:]*([1-5]\d{2})\b", text, _re.I)
    if not m:
        m = _re.search(r"\b([45]\d{2})\s+(?:Unauthorized|Forbidden|Not Found|Bad Request|"
                       r"Conflict|Unprocessable|Too Many|Internal Server|Bad Gateway|"
                       r"Service Unavailable|Gateway Timeout)\b", text, _re.I)
    if m:
        return f"http_{int(m.group(1))}"[:40]
    return type(exc).__name__[:40]


def is_retryable(error_code: str | None) -> bool:
    """Transient failures only. A 4xx (bad request, unauthorised, not found) will fail
    identically on every retry — retrying it just burns attempts and hides a real
    misconfiguration — so only 5xx, 408/429, and transport-level errors are retried."""
    if not error_code:
        return True                      # unknown shape — give it a chance
    if error_code.startswith("http_"):
        try:
            code = int(error_code.split("_", 1)[1])
        except (ValueError, IndexError):
            return True
        return code >= 500 or code in (408, 429)
    return True                          # timeouts, connect errors, TLS, DNS…


def _record_attempt(message: A2AMessage) -> None:
    """Bump the attempt counter and schedule (or stop) the next retry. Never raises."""
    try:
        from datetime import timedelta
        message.attempts = (message.attempts or 0) + 1
        if message.status == "delivered":
            message.next_retry_at = None
            return
        message.last_error_at = utcnow()
        # ITA-5: a tunnelled exchange is NEVER scheduled for retry — replaying
        # it is a duplicate business call on the far side, not a redelivery.
        # Excluded at scheduling time AND in the sweep query (defense in
        # depth); the tunnel does its own bounded retry or none at all.
        from app.a2a_common.integration_contract import TUNNEL_TASK_TYPES
        if (message.task_type or "") in TUNNEL_TASK_TYPES:
            message.next_retry_at = None
            return
        if not is_retryable(message.error_code) or message.attempts >= MAX_DELIVERY_ATTEMPTS:
            # Give up scheduling; the row stays `delivery_failed` and is still visible in
            # /admin/a2a-logs and manually resendable.
            message.next_retry_at = None
            return
        idx = min(message.attempts - 1, len(_RETRY_BACKOFF_MINUTES) - 1)
        message.next_retry_at = utcnow() + timedelta(minutes=_RETRY_BACKOFF_MINUTES[idx])
    except Exception:  # noqa: BLE001 — bookkeeping must never break the send
        logger.exception("failed to record delivery attempt")


# ── Partner type / URL helpers ───────────────────────────────────────────────


def _is_cert_engine(partner: PartnerAgent) -> bool:
    types = partner.partner_type or []
    if isinstance(types, str):
        types = [types]
    return "cert_engine" in types


def _agent_card_url(partner: PartnerAgent) -> str:
    """Path used by the admin "Test Connectivity" UI to verify a partner
    is reachable.

    The SDK protocol mandates the card at `/.well-known/agent-card.json`
    relative to the partner's endpoint. The endpoint_url is whatever
    the operator registered — direct service URL for east-west traffic
    (`http://partner_backend:8001`) or an external host with whatever
    path prefix that deployment uses (`https://bank.example.com/a2a`).
    Either way, the card lives at `{endpoint_url}/.well-known/agent-card.json`.

    Pre-Slice-1 of the security hardening this helper hardcoded a
    `/a2a-partner/` prefix for non-cert partners, which broke direct-
    backend endpoint URLs. Removed — operators control the prefix via
    endpoint_url itself. cert-engine is handled the same way; the
    branch existed for the same prefix bug.
    """
    base = (partner.endpoint_url or "").rstrip("/")
    return f"{base}/.well-known/agent-card.json"


def partner_verify(partner: PartnerAgent):
    """Resolve the httpx ``verify=`` value for outbound calls to this partner.

    Applied to BOTH the admin Test-connectivity probe and the real A2A card fetch
    so trusting a CA (or disabling verification) fixes connectivity end-to-end,
    not just the button. Precedence:

      1. Verification OFF (per-partner ``ssl_verify`` when set, else global
         ``settings.partner_tls_verify``) → return ``False`` (skip verification).
      2. Partner's own uploaded CA/cert PEM (``ca_cert_pem``) → an SSLContext that
         trusts exactly that CA.
      3. Global ``settings.partner_ca_bundle`` path → trust that bundle.
      4. Otherwise → ``True`` (default system/certifi trust).

    Returns anything httpx accepts as ``verify``: bool | str (path) | SSLContext.
    """
    from app.core.config import settings
    verify_on = partner.ssl_verify if partner.ssl_verify is not None else settings.partner_tls_verify
    if not verify_on:
        # T3 (THREAT_MODEL.md — "No telemetry when ssl_verify=False is used
        # for a partner"). TLS verification being off is a legitimate,
        # per-partner operational choice (self-signed certs) — but it also
        # removes MITM protection for that partner, so it should be a
        # VISIBLE, alertable condition rather than a silent config value.
        # Rate-limited to once per partner per process lifetime (same
        # pattern as SdkHmacMiddleware's `_warned_no_secret`), so a partner
        # making thousands of calls doesn't flood the log.
        if partner.id not in _warned_tls_verify_disabled:
            logger.warning(
                "SECURITY_EVENT event=tls_verification_disabled severity=medium "
                "partner_id=%s partner_name=%r decision=allowed "
                "detail=\"outbound calls to this partner skip TLS certificate "
                "verification — confirm this is an intentional self-signed-cert "
                "accommodation, not an oversight\"",
                partner.id, partner.name,
            )
            _warned_tls_verify_disabled.add(partner.id)
        return False
    pem = (getattr(partner, "ca_cert_pem", None) or "").strip()
    if pem:
        import ssl
        ctx = ssl.create_default_context()
        try:
            ctx.load_verify_locations(cadata=pem)
            return ctx
        except ssl.SSLError as e:  # malformed upload — don't hard-fail the call
            logger.warning("partner_verify: partner=%s ca_cert_pem invalid (%s) — "
                           "falling back to default trust", partner.id, e)
    if settings.partner_ca_bundle:
        return settings.partner_ca_bundle
    return True


def _auth_url(partner: PartnerAgent) -> str:
    """Bearer-JWT handshake URL for partners that require one."""
    base = (partner.endpoint_url or "").rstrip("/")
    return f"{base}/a2a/auth"


async def _bearer_jwt_if_needed(partner: PartnerAgent) -> Optional[str]:
    """Fetch a JWT for partners that require one (today: cert_engine).

    The shared cache in `app.a2a_common.auth` is keyed by partner.id —
    worker-restart re-handshakes cleanly without leaking tokens between
    processes.
    """
    if not _is_cert_engine(partner):
        return None
    if not partner.api_key:
        logger.warning("cert_engine partner has no api_key: id=%s", partner.id)
        return None
    return await fetch_bearer_jwt(
        partner_id=partner.id,
        auth_url=_auth_url(partner),
        api_key=partner.api_key,
    )


# ── Outbound entry point ─────────────────────────────────────────────────────


async def resend_message(db: Session, message: A2AMessage) -> A2AMessage:
    """Re-attempt delivery of an EXISTING outbound A2AMessage row.

    Used by the retry sweeper (`a2a.retry_failed_deliveries`) and by the manual
    `POST /admin/a2a-logs/{id}/resend` endpoint. Deliberately re-attempts the SAME row
    rather than creating a new one, so `attempts` / `next_retry_at` accumulate on the
    original message and the audit trail stays a single record per logical send.

    The stored `payload` IS the exact wire envelope that was built for the first attempt,
    so the partner receives a byte-identical message (same `message_id`, which is also the
    A2A dedup key — a partner that did receive the first attempt can de-duplicate it).
    """
    partner = db.get(PartnerAgent, message.partner_id)
    if partner is None:
        message.error_code = "partner_missing"
        message.next_retry_at = None
        db.commit()
        return message
    if not partner.endpoint_url:
        message.status = "pending"
        message.next_retry_at = None      # nothing to retry until an endpoint is configured
        db.commit()
        return message

    # ── SSRF guard at resend time (F-001) ─────────────────────────────────
    # Same send-time guard as send_task_to_partner(). The endpoint_url was
    # validated at write time by _validate_endpoint_url() in partners.py,
    # but that check is mode-gated (ssrf_guard_mode="observe"/"off" would
    # let a bad URL through). This second check catches any that slipped
    # past the write-time guard before making outbound HTTP calls — including
    # the _bearer_jwt_if_needed() auth handshake below, which also hits the
    # endpoint URL.
    from app.core.config import settings as _a2a_settings
    from app.core.ssrf_guard import SsrfBlocked, check_outbound_url, parse_allowlist
    try:
        check_outbound_url(
            partner.endpoint_url,
            mode=_a2a_settings.ssrf_guard_mode,
            allowlist=parse_allowlist(_a2a_settings.ssrf_allowed_internal_hosts),
            allow_private=_a2a_settings.ssrf_allow_private_networks,
            context=f"a2a_resend partner={partner.id}",
            block_on_resolution_failure=_a2a_settings.ssrf_block_on_resolution_failure,
        )
    except SsrfBlocked as _ssrf_exc:
        message.status = "delivery_failed"
        message.task_state = "failed"
        message.error_code = "ssrf_blocked"
        message.response_body = {"error": str(_ssrf_exc)[:2000]}
        db.commit()
        logger.error(
            "A2A resend blocked by SSRF guard: task_id=%s partner=%s endpoint=%s reason=%s",
            message.id, partner.name, partner.endpoint_url, _ssrf_exc,
        )
        return message

    auth_header: Optional[str] = None
    if _is_cert_engine(partner):
        jwt_token = await _bearer_jwt_if_needed(partner)
        auth_header = f"Bearer {jwt_token}" if jwt_token else None
    elif partner.jwt_signing_secret:
        try:
            auth_header = "Bearer " + create_partner_outbound_token(
                partner_id=partner.id, signing_secret=partner.jwt_signing_secret)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resend token mint failed: partner=%s err=%s", partner.name, exc)

    started_ns = time.perf_counter_ns()
    try:
        # T4 (THREAT_MODEL.md) — per-partner bulkhead around the actual
        # outbound HTTP call, so one slow-but-not-timed-out partner cannot
        # consume a disproportionate share of the shared outbound
        # connection pool relative to other partners' resends/sends.
        # No-op (disabled) unless an operator sets
        # settings.partner_max_concurrent_calls > 0.
        from app.core.resilience import partner_bulkhead
        async with partner_bulkhead(partner.id):
            resp_dict = await send_a2a_message(
                base_url=(partner.endpoint_url or "").rstrip("/"),
                context_id=message.change_request_id or message.id,
                task_id=message.id,
                data=message.payload or {},
                auth_header=auth_header,
                hmac_secret=partner.signing_secret,
                # A12 — same correlation/change headers as the original send,
                # read back off the stored envelope so a resend is traceable to
                # the same conversation as the first attempt.
                correlation_id=(message.payload or {}).get("correlation_id"),
                change_id=message.change_request_id,
            )
        message.status = "delivered"
        message.task_state = "completed"
        message.error_code = None
        message.next_retry_at = None
        if resp_dict is not None:
            message.response_body = resp_dict
        logger.info("A2A resend DELIVERED: task_id=%s partner=%s attempt=%s",
                    message.id, partner.name, (message.attempts or 0) + 1)
    except Exception as exc:  # noqa: BLE001
        message.status = "delivery_failed"
        message.task_state = "failed"
        message.error_code = _error_code_for(exc)
        message.response_body = {"error": str(exc)[:2000]}
        logger.error("A2A resend FAILED: task_id=%s partner=%s err=%s",
                     message.id, partner.name, exc)
    finally:
        message.latency_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        _record_attempt(message)
        db.commit()

    # Tell a human once we have exhausted retries — otherwise a permanently undeliverable
    # message would go quiet again after the last attempt.
    if message.status != "delivered" and message.next_retry_at is None:
        try:
            from app.services.notifications import notify_delivery_failure
            notify_delivery_failure(db, message, partner,
                                    context=f"resend gave up after {message.attempts} attempts")
        except Exception:  # noqa: BLE001
            logger.exception("resend-exhausted notification failed")
    return message


async def send_task_to_partner(
    partner: PartnerAgent,
    task_type: A2ATaskType,
    payload: dict,
    db: Session,
    change_request_id: Optional[str] = None,
    *,
    correlation_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    agent_run_id: Optional[str] = None,
    cflow_id: Optional[str] = None,
    cert_attempt: Optional[int] = None,
    timeout: Optional[float] = None,
) -> A2AMessage:
    """Send an A2A Task to a partner via the SDK JSON-RPC wire.

    `timeout` (seconds) overrides the transport default for this send. It is
    the MIDDLE LAYER of the integration-tunnel budget (ITA §6), which must
    shrink inward — ingress 105s > A2A send 90s > egress→target 60s — so that
    the innermost layer fails first and the operator sees the real cause
    instead of a generic outer 504. The transport's own default is 30s, BELOW
    the 60s target ceiling, so a tunnelled send that does not pass this would
    fail at the transport before a slow target ever answered. None keeps the
    transport default, which is right for every non-tunnel caller.

    Always returns the persisted `A2AMessage` row. Status reflects
    delivery outcome:
      'sent'              — initial state; replaced before return
      'pending'           — partner has no endpoint configured
      'delivered'         — SDK send completed without exception
      'delivery_failed'   — SDK exception (auth, transport, server)

    SDK lifecycle metadata is also persisted:
      task_id_a2a  — same as the audit-row `id` (we use it as the SDK
                     Task id so cross-references stay simple)
      task_state   — 'completed' on success, 'failed' on error
      protocol_ver — always 'a2a_sdk' post-Slice-8
    """
    # Build the A2A wire-level wrapper once; reuse for both the audit
    # row and the SDK send so what the admin UI displays as
    # `request_body` matches the structured Part the partner actually
    # receives (modulo the JSON-RPC + A2A Message envelopes the SDK
    # adds around it).
    #
    # Phase 1 (protocol v1): the envelope now carries protocol_version +
    # message_id (dedup key) + correlation_id + agent_id + timestamp. We
    # generate the audit-row id up front and reuse it as message_id so the
    # wire dedup key and the local audit id stay 1:1 (replacing the old
    # implicit "SDK Task id == audit id" coupling).
    # Protocol v1.1: change_communication ships the reconciled canonical wire
    # shape (product_kit[] + attachments[]), and threads on a stable
    # per-(change, partner) correlation_id so a partner's replies correlate to
    # the exact conversation. Single-emit — no legacy documents[] aliases. See
    # docs/A2A_v1_1_change_communication.md. The transform is idempotent.
    if str(getattr(task_type, "value", task_type)) == "change_communication":
        payload = to_wire_v1_1(payload)
        if correlation_id is None and change_request_id:
            correlation_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"a2a-corr:{change_request_id}:{partner.id}")
            )

    mid = generate_uuid()
    wire_data: dict = make_envelope(
        task_type,
        message_id=mid,
        from_=SENDER_AUTHORITY,
        payload=payload,
        change_id=change_request_id,
        cflow_id=cflow_id,
        cert_attempt=cert_attempt,
        correlation_id=correlation_id or change_request_id,
        agent_id=agent_id or "npci.platform.v1",
        agent_run_id=agent_run_id,
        timestamp=utcnow().isoformat(),
    )

    message = A2AMessage(
        id=mid,
        change_request_id=change_request_id,
        partner_id=partner.id,
        direction=A2ADirection.OUTBOUND,
        task_type=task_type,
        payload=wire_data,
        status="sent",
        protocol_ver="a2a_sdk",
        created_at=utcnow(),
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    logger.info(
        "A2A outbound task: task_id=%s partner=%s type=%s change=%s",
        message.id, partner.name, task_type.value, change_request_id,
    )

    if not partner.endpoint_url:
        message.status = "pending"
        db.commit()
        logger.info(
            "A2A task queued (no endpoint): task_id=%s partner=%s",
            message.id, partner.name,
        )
        return message

    # ── SSRF guard at send time (F-001) ─────────────────────────────────────
    # Re-check the endpoint_url at send time, not only at write time. A stale
    # or operator-modified endpoint_url that bypassed the write-time guard
    # (e.g. because ssrf_guard_mode was "observe" or "off" when it was set)
    # would otherwise be used for an outbound request without vetting.
    # This is deliberately a SECOND check, not a replacement for the write-time
    # guard: the write-time guard prevents a bad URL from being stored at all,
    # and this one catches any that slipped through.
    from app.core.config import settings as _a2a_settings
    from app.core.ssrf_guard import SsrfBlocked, check_outbound_url, parse_allowlist
    try:
        check_outbound_url(
            partner.endpoint_url,
            mode=_a2a_settings.ssrf_guard_mode,
            allowlist=parse_allowlist(_a2a_settings.ssrf_allowed_internal_hosts),
            allow_private=_a2a_settings.ssrf_allow_private_networks,
            context=f"a2a_send partner={partner.id}",
            block_on_resolution_failure=_a2a_settings.ssrf_block_on_resolution_failure,
        )
    except SsrfBlocked as _ssrf_exc:
        message.status = "delivery_failed"
        message.task_state = "failed"
        message.error_code = "ssrf_blocked"
        message.response_body = {"error": str(_ssrf_exc)[:2000]}
        db.commit()
        logger.error(
            "A2A send blocked by SSRF guard: task_id=%s partner=%s endpoint=%s reason=%s",
            message.id, partner.name, partner.endpoint_url, _ssrf_exc,
        )
        return message

    # Slice 3 of security hardening: every partner with a configured
    # jwt_signing_secret gets a Bearer JWT minted with that secret.
    # cert_engine remains a special case — it shares the platform-wide
    # secret via the /a2a/auth handshake (the cert-agent process trusts
    # The Authority's secret_key directly). Partners with neither configured
    # send no Authorization header (back-compat).
    auth_header: Optional[str] = None
    if _is_cert_engine(partner):
        jwt_token = await _bearer_jwt_if_needed(partner)
        if not jwt_token:
            message.status = "delivery_failed"
            message.task_state = "failed"
            message.error_code = "cert_engine_auth_failed"
            db.commit()
            logger.warning(
                "A2A delivery failed (cert_engine auth): task_id=%s partner=%s",
                message.id, partner.name,
            )
            return message
        auth_header = f"Bearer {jwt_token}"
    elif partner.jwt_signing_secret:
        # Per-partner signed token. No network round-trip; we mint it
        # locally because we hold the signing secret.
        try:
            token = create_partner_outbound_token(
                partner_id=partner.id,
                signing_secret=partner.jwt_signing_secret,
            )
            auth_header = f"Bearer {token}"
        except Exception as exc:  # noqa: BLE001
            # Don't fail the whole send — log and proceed without auth
            # so an unconfigured signing secret doesn't take down a
            # legitimate outbound message.
            logger.warning(
                "A2A token mint failed; sending unsigned: partner=%s err=%s",
                partner.name, exc,
            )

    # Slice 8 audit: stamp jwt_sub when we attached a Bearer (we know
    # the partner ID, which IS the sub claim post-Slice-2). caller_ip
    # is left null for outbound — it's a "where did the request come
    # from" signal that only makes sense inbound.
    if auth_header:
        message.jwt_sub = partner.id

    started_ns = time.perf_counter_ns()
    base_url = (partner.endpoint_url or "").rstrip("/")
    # Common send kwargs shared by the primary (card-discovery) attempt and the
    # endpoint fallback below.
    send_kwargs = dict(
        base_url=base_url,
        context_id=change_request_id or message.id,
        task_id=message.id,                 # reuse audit-row id as SDK Task id
        data=wire_data,
        auth_header=auth_header,
        # Slice 5 outbound HMAC envelope. When set, send_a2a_message
        # registers an httpx event hook that adds X-NPCI-Signature
        # over the request body. NULL = no envelope (back-compat).
        hmac_secret=partner.signing_secret,
        # Honour the per-partner ssl_verify / global partner_tls_verify
        # toggle on the ACTUAL send too — not just the card-fetch probe.
        # Without this, disabling verification fixed Test-connectivity but
        # the real message still failed on a self-signed partner cert.
        verify=partner_verify(partner),
        # A12 — propagate correlation/change identifiers as HTTP headers
        # (in addition to the envelope's own fields set above via
        # make_envelope). `wire_data["correlation_id"]` is exactly what the
        # envelope carries — reusing it (rather than re-deriving) guarantees
        # the header and the body agree, so an operator correlating via
        # either one lands on the same conversation.
        correlation_id=wire_data.get("correlation_id"),
        change_id=change_request_id,
    )
    # Only set when the caller asked: omitting the key leaves
    # `send_a2a_message`'s own default in force, so no existing caller's
    # behaviour changes.
    if timeout is not None:
        send_kwargs["timeout"] = float(timeout)
    try:
        # T4 (THREAT_MODEL.md) — per-partner bulkhead around BOTH the
        # primary and fallback outbound attempts, same rationale as the
        # resend path above. No-op unless an operator sets
        # settings.partner_max_concurrent_calls > 0.
        from app.core.resilience import partner_bulkhead
        async with partner_bulkhead(partner.id):
            # Slice 25 — capture partner's response so the admin A2A logs
            # UI can render it next to the request body. None is fine when
            # the partner emits no artifact (rare; most receivers send a
            # task-receipt acknowledgement).
            try:
                # Primary: SDK discovers the partner's card and sends to the
                # interface URL the card advertises.
                resp_dict = await send_a2a_message(**send_kwargs)
            except Exception as primary_exc:  # noqa: BLE001
                # Fallback: the card's advertised interface is unreachable or
                # misconfigured (e.g. a partner card publishing `http://<ip>:443`).
                # Retry against the endpoint configured in the Partner Registry +
                # the standard `/a2a-rpc/rpc` path. change_communication is
                # idempotent on change_id, so a duplicate that actually landed on
                # the first try is safely skipped by the receiver.
                fallback_rpc = f"{base_url}/a2a-rpc/rpc" if base_url else ""
                if not fallback_rpc:
                    raise
                logger.warning(
                    "A2A card-interface send failed for partner=%s (%s: %s) — "
                    "retrying via configured endpoint %s",
                    partner.name, type(primary_exc).__name__,
                    str(primary_exc)[:200], fallback_rpc,
                )
                resp_dict = await send_a2a_message(rpc_url=fallback_rpc, **send_kwargs)
                logger.info(
                    "A2A delivered via endpoint fallback: task_id=%s partner=%s url=%s",
                    message.id, partner.name, fallback_rpc,
                )
        # S6 (ARCHITECTURE_REVIEW_ACTIONS.md — outbound dependency response
        # validation "applies to LLM providers and partner replies alike").
        # A structurally malformed reply is still recorded (so the audit
        # trail is complete) but flagged via the security telemetry event
        # rather than silently trusted — the delivery itself already
        # succeeded (the exception path below covers transport/HTTP
        # failures), this only judges the SHAPE of what came back.
        from app.core.outbound_validation import validate_partner_response, log_validation_warnings
        _resp_check = validate_partner_response(resp_dict)
        if not _resp_check.ok:
            logger.warning(
                "SECURITY_EVENT event=malformed_dependency_response severity=medium "
                "context=a2a_partner_response partner_id=%s task_id=%s reason=%s",
                partner.id, message.id, _resp_check.reason,
            )
        else:
            log_validation_warnings(_resp_check, context=f"a2a_partner_response:{partner.id}",
                                    correlation_id=wire_data.get("correlation_id"))

        message.status = "delivered"
        message.task_id_a2a = message.id
        message.task_state = "completed"
        # A successful delivery clears any pending retry so the sweeper stops chasing it.
        message.next_retry_at = None
        if resp_dict is not None:
            message.response_body = resp_dict
        logger.info(
            "A2A SDK task delivered: task_id=%s partner=%s attempt=%s",
            message.id, partner.name, (message.attempts or 0) + 1,
        )
    except Exception as exc:  # noqa: BLE001
        message.status = "delivery_failed"
        message.task_state = "failed"
        # Prefer a real HTTP status when the SDK surfaced one — `type(exc).__name__` alone
        # collapsed every 4xx/5xx into "HTTPStatusError", which is useless for triage
        # (a 401 needs a credential fix, a 503 just needs a retry).
        message.error_code = _error_code_for(exc)
        # Slice 25 — record the failure shape on response_body too so
        # the admin UI shows something useful for failed sends.
        message.response_body = {"error": str(exc)[:2000]}
        logger.error(
            "A2A SDK delivery error: task_id=%s partner=%s err=%s",
            message.id, partner.name, exc,
        )
    finally:
        message.latency_ms = max(
            0, (time.perf_counter_ns() - started_ns) // 1_000_000
        )
        _record_attempt(message)
        db.commit()

    # Alert a human on any non-delivery. Previously this was logger.error only, so a bank
    # that never received its kit produced no signal anyone would see.
    if message.status != "delivered":
        try:
            from app.services.notifications import notify_delivery_failure
            _ctx = getattr(task_type, "value", None) or str(task_type)
            notify_delivery_failure(db, message, partner, context=_ctx)
        except Exception:  # noqa: BLE001 — alerting must never break the send path
            logger.exception("delivery-failure notification failed")

    return message


# ── Card-discovery helper (admin /partners/{id}/test) ────────────────────────


async def fetch_partner_agent_card(partner: PartnerAgent) -> Optional[dict]:
    """Fetch a partner's Agent Card from `/.well-known/agent-card.json`.

    Used by the admin "Test Connectivity" feature in the partners UI.
    A 200 response means the partner's SDK mount is healthy.
    """
    if not partner.endpoint_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=partner_verify(partner)) as client:
            resp = await client.get(_agent_card_url(partner))
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to fetch agent card: partner=%s error=%s",
            partner.name, exc,
        )

    return None
