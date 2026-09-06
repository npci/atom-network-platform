# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner Registry API — Admin endpoints for managing ecosystem partners."""
import hashlib
import logging
import os
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, func

from app.core import admin_action_audit
from app.core.admin_action_audit_middleware import mark_explicitly_recorded
from app.core.deps import DbDep, AdminUser
from app.core.error_taxonomy import client_safe_detail
from app.models.phase_c import PartnerAgent, PartnerType, PartnerStatus
from app.models.base import generate_uuid, utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/partners", tags=["partners"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreatePartnerRequest(BaseModel):
    name: str
    partner_types: list[str]  # ["payer_psp"], ["remitter"], ["payer_psp", "remitter"]
    endpoint_url: str | None = None


class UpdatePartnerRequest(BaseModel):
    name: str | None = None
    endpoint_url: str | None = None
    partner_types: list[str] | None = None
    # Cert-agent's bank_id for this partner (e.g. 'HDFC'). Required for
    # the readiness orchestrator to trigger cert runs against this
    # partner. Empty string clears the mapping.
    cert_agent_bank_id: str | None = None


class UpdateProtocolRequest(BaseModel):
    """Body for `PATCH /admin/partners/{id}/protocol` (Slice 6).

    Switches a partner's outbound A2A wire. Validated server-side
    against `_VALID_PROTOCOLS` so an arbitrary value can never sneak
    into `partner_agents.protocol_version`.
    """
    protocol_version: str


class UpdateTlsTierRequest(BaseModel):
    """Body for `PATCH /admin/partners/{id}/tls-tier` (Slice 6 of A2A
    security hardening). Switches the partner between :443 (jwt) and
    :8443 (mtls) ingresses."""
    tls_tier: str  # 'jwt' | 'mtls'


class UpdateCertFingerprintRequest(BaseModel):
    """Body for `PATCH /admin/partners/{id}/cert-fingerprint` (Slice 6).

    SHA-256 hex of the bank's client cert. Set to `null` to clear (use
    only when also flipping tls_tier off mtls — clearing while on mtls
    leaves the partner unable to call until a new fingerprint lands)."""
    client_cert_fingerprint: str | None


class UpdateSslVerifyRequest(BaseModel):
    """Body for `PATCH /admin/partners/{id}/ssl-verify`.

    Outbound TLS verification for calls to this partner's endpoint. `true` =
    verify (default), `false` = skip (self-signed / internal), `null` = inherit
    the global PARTNER_TLS_VERIFY setting."""
    ssl_verify: bool | None


class UploadCaCertRequest(BaseModel):
    """Body for `POST /admin/partners/{id}/ca-cert`.

    `ca_cert_pem` is the PEM text of the CA (or leaf) cert to trust for this
    partner's HTTPS endpoint. Empty/null clears the uploaded cert (falls back to
    the global PARTNER_CA_BUNDLE / system trust)."""
    ca_cert_pem: str | None


class UpdateMaxInlineAttachmentRequest(BaseModel):
    """Body for `PATCH /admin/partners/{id}/max-inline-attachment`.

    Per-partner cap (bytes) on the base64 (wire) size of a single kit attachment
    shipped inline in the A2A envelope; larger attachments are omitted so the
    kit send stays under this partner's ingress body-size limit. `null` inherits
    the global PARTNER_MAX_INLINE_ATTACHMENT_BYTES; `0` = no limit."""
    max_inline_attachment_bytes: int | None


class UpdateAllowedCidrsRequest(BaseModel):
    """Body for `PATCH /admin/partners/{id}/allowed-cidrs` (Slice 7).

    `allowed_cidrs` is a list of CIDR strings (`["10.0.0.0/8", ...]`)
    or null/empty to clear the allowlist (no IP enforcement)."""
    allowed_cidrs: list[str] | None


class UpdateRateLimitRequest(BaseModel):
    """Body for `PATCH /admin/partners/{id}/rate-limit` (Slice 7).

    Sets the partner's per-second rate cap. Stored on the row but not
    yet enforced per-partner by nginx (the flat baseline zone applies
    to everyone today); Slice 9 may wire an njs-driven dynamic
    override. Range: 1–10000."""
    rate_limit_rps: int


_VALID_PROTOCOLS = ("legacy", "a2a_sdk")
_VALID_TLS_TIERS = ("jwt", "mtls")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _mask_key(key: str) -> str:
    if not key or len(key) <= 12:
        return "****"
    return key[:8] + "****" + key[-4:]


def _normalize_types(types) -> list[str]:
    """Normalize partner_type to a list of strings."""
    if isinstance(types, list):
        return types
    if isinstance(types, str):
        return [types]
    if hasattr(types, 'value'):
        return [types.value]
    return ["payer_psp"]


def _validate_endpoint_url(url: str) -> None:
    """Reject endpoint URLs that point at internal/private networks (SSRF).

    Two independent checks:

    1. **Scheme** — production still requires https://. Unchanged behaviour.
    2. **Target** — delegated to ``app.core.ssrf_guard``, which resolves the
       hostname and classifies the resulting IPs instead of prefix-matching the
       URL string.

    The previous implementation kept a tuple of literal ``http://`` prefixes.
    Because every entry began with ``http://``, switching to ``https://``
    bypassed all 21 of them — ``https://169.254.169.254/latest/meta-data/`` (the
    cloud metadata service, which issues IAM credentials) was allowed in
    production. It also missed IPv6 (``[::1]``), integer notation
    (``2130706433``), and hostnames that merely resolve to a private address.
    Worse, the whole block was skipped when ``ENVIRONMENT`` was
    ``development``/``uat``/``staging``, and compose sets ``ENVIRONMENT=uat`` —
    so on the default stack it did nothing at all.

    The guard DEFAULTS TO ENFORCE MODE (``ssrf_guard_mode="enforce"``): a
    private, loopback or link-local target is refused with a 400. Internal
    partners that must keep working are named in
    ``SSRF_ALLOWED_INTERNAL_HOSTS``, or re-enabled wholesale with
    ``SSRF_ALLOW_PRIVATE_NETWORKS=true``. Set ``SSRF_GUARD_MODE=observe`` to
    downgrade it to logging-only during a staged rollout, or ``off`` to disable
    it entirely.
    """
    if not url:
        return

    # Check 1 — scheme. Preserved as-is, including the environment carve-out, so
    # this change introduces no new rejection on its own.
    env = os.environ.get("ENVIRONMENT", "production").lower()
    parsed = urlparse(url)
    if env not in ("development", "uat", "staging") and parsed.scheme != "https":
        raise HTTPException(
            status_code=400,
            detail="endpoint_url must use https:// in production.",
        )

    # Check 2 — target address. Mode-gated; raises only under enforce.
    from app.core.config import settings
    from app.core.ssrf_guard import SsrfBlocked, check_outbound_url, parse_allowlist

    try:
        check_outbound_url(
            url,
            mode=settings.ssrf_guard_mode,
            allowlist=parse_allowlist(settings.ssrf_allowed_internal_hosts),
            allow_private=settings.ssrf_allow_private_networks,
            context="partner endpoint_url",
            block_on_resolution_failure=settings.ssrf_block_on_resolution_failure,
        )
    except SsrfBlocked as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"endpoint_url refused: {exc.reason}. If this is a legitimate "
                "internal partner, add its host to SSRF_ALLOWED_INTERNAL_HOSTS."
            ),
        ) from exc


def _partner_response(p: PartnerAgent) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "partner_types": _normalize_types(p.partner_type),
        "partner_type": _normalize_types(p.partner_type)[0],  # backward compat — first type
        "endpoint_url": p.endpoint_url,
        "api_key_masked": _mask_key(p.api_key or ""),
        "status": p.status.value if hasattr(p.status, 'value') else p.status,
        # A2A wire selector — alembic 0033, Slice 5/6 of the SDK refactor.
        # 'legacy' (default) routes outbound through the hand-rolled POST;
        # 'a2a_sdk' routes through the SDK's JSON-RPC client.
        "protocol_version": getattr(p, "protocol_version", "legacy"),
        # Slice 6 — bank-tier mTLS state. tls_tier determines which
        # nginx ingress the partner uses; client_cert_fingerprint
        # is the pinned SHA-256 hex when tier=mtls.
        "tls_tier": getattr(p, "tls_tier", "jwt"),
        "client_cert_fingerprint": getattr(p, "client_cert_fingerprint", None),
        # Outbound TLS (the Authority → partner endpoint). ssl_verify=null → inherit the
        # global PARTNER_TLS_VERIFY. has_ca_cert flags an uploaded trust anchor
        # (the PEM itself is never returned in the list — see GET .../ca-cert).
        "ssl_verify": getattr(p, "ssl_verify", None),
        "has_ca_cert": bool(getattr(p, "ca_cert_pem", None)),
        # Per-partner inline-attachment cap (wire bytes); null → global default.
        "max_inline_attachment_bytes": getattr(p, "max_inline_attachment_bytes", None),
        # Slice 7 — network controls
        "allowed_cidrs": getattr(p, "allowed_cidrs", None),
        "rate_limit_rps": getattr(p, "rate_limit_rps", 100),
        "agent_card_url": p.agent_card_url,
        # Cert-agent short bank_id used by the readiness orchestrator.
        "cert_agent_bank_id": getattr(p, "cert_agent_bank_id", None),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_partners(db: DbDep, _: AdminUser):
    """List all registered partners."""
    partners = db.scalars(select(PartnerAgent).order_by(PartnerAgent.created_at.desc())).all()
    return [_partner_response(p) for p in partners]


@router.post("")
def create_partner(body: CreatePartnerRequest, db: DbDep, _: AdminUser):
    """Register a new partner. Auto-generates the three credentials
    that the A2A security stack consumes:

      * api_key             — plaintext returned ONCE; only the hash
                              is persisted; partner uses for /a2a/auth.
      * jwt_signing_secret  — Slice 3; per-partner HS256 secret for
                              outbound Bearer JWT identity proof.
      * signing_secret      — Slice 5; per-partner HMAC secret for the
                              X-NPCI-Signature envelope (payload
                              non-repudiation + replay protection).

    All three are returned ONCE in the response. The two `*_secret`
    values must be shipped to the partner OOB and installed on its
    side as `partner_settings.{npci_jwt_secret,npci_hmac_secret}`."""
    if body.endpoint_url:
        _validate_endpoint_url(body.endpoint_url)

    api_key = PartnerAgent.generate_api_key()
    jwt_signing_secret = PartnerAgent.generate_jwt_signing_secret()
    signing_secret = PartnerAgent.generate_signing_secret()

    valid_types = [t.value for t in PartnerType]
    types = [t for t in body.partner_types if t in valid_types] or ["payer_psp"]

    partner = PartnerAgent(
        id=generate_uuid(),
        name=body.name,
        partner_type=types,
        endpoint_url=body.endpoint_url,
        api_key=api_key,
        api_key_hash=_hash_key(api_key),
        jwt_signing_secret=jwt_signing_secret,
        signing_secret=signing_secret,
        status=PartnerStatus.ACTIVE,
    )
    db.add(partner)
    db.commit()
    db.refresh(partner)

    logger.info("Partner created: id=%s name='%s' types=%s", partner.id, partner.name, types)

    # Return all three secrets only on creation. The admin is
    # responsible for shipping `jwt_signing_secret` and `signing_secret`
    # to the partner via a secure channel for installation on their side.
    resp = _partner_response(partner)
    resp["api_key"] = api_key  # show full key once
    resp["jwt_signing_secret"] = jwt_signing_secret  # show full secret once
    resp["signing_secret"] = signing_secret  # show full secret once

    # Null the plaintext api_key now that the response is built. The hash
    # (`api_key_hash`) is kept for auth; the plaintext is no longer needed.
    partner.api_key = None
    db.commit()

    return resp


@router.put("/{partner_id}")
def update_partner(partner_id: str, body: UpdatePartnerRequest, db: DbDep, _: AdminUser):
    """Update partner details."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    if body.name is not None:
        partner.name = body.name
    if body.endpoint_url is not None:
        _validate_endpoint_url(body.endpoint_url)
        partner.endpoint_url = body.endpoint_url
    if body.partner_types is not None:
        valid_types = [t.value for t in PartnerType]
        partner.partner_type = [t for t in body.partner_types if t in valid_types] or partner.partner_type
    if body.cert_agent_bank_id is not None:
        # Empty string clears the mapping; non-empty trimmed string
        # writes the cert-agent short code (e.g. 'HDFC').
        partner.cert_agent_bank_id = body.cert_agent_bank_id.strip() or None
    partner.updated_at = utcnow()

    db.commit()
    logger.info("Partner updated: id=%s name='%s'", partner_id, partner.name)
    return _partner_response(partner)


@router.post("/{partner_id}/rotate-key")
def rotate_api_key(partner_id: str, db: DbDep, _: AdminUser):
    """Generate a new API key for the partner."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    new_key = PartnerAgent.generate_api_key()
    partner.api_key = new_key
    partner.api_key_hash = _hash_key(new_key)
    partner.updated_at = utcnow()
    db.commit()

    logger.info("Partner API key rotated: id=%s name='%s'", partner_id, partner.name)

    resp = _partner_response(partner)
    resp["api_key"] = new_key  # show full key once

    # Null the plaintext now that the response is built.
    partner.api_key = None
    db.commit()

    return resp


@router.post("/{partner_id}/rotate-jwt-secret")
def rotate_jwt_signing_secret(partner_id: str, db: DbDep, _: AdminUser):
    """Generate a new outbound-JWT signing secret for the partner.

    Slice 3 of A2A security hardening. The new secret is returned ONCE
    here; the admin must ship it to the partner. A 5-minute grace period
    allows the previous secret to remain valid while the partner installs
    the new one — the auth middleware tries the current secret first and
    falls back to the previous if `secret_rotated_at` is recent enough."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    new_secret = PartnerAgent.generate_jwt_signing_secret()
    partner.previous_jwt_signing_secret = partner.jwt_signing_secret
    partner.jwt_signing_secret = new_secret
    partner.secret_rotated_at = utcnow()
    partner.updated_at = utcnow()
    db.commit()

    logger.info(
        "Partner JWT signing secret rotated: id=%s name='%s'",
        partner_id, partner.name,
    )

    resp = _partner_response(partner)
    resp["jwt_signing_secret"] = new_secret  # show full secret once
    return resp


@router.post("/{partner_id}/rotate-hmac-secret")
def rotate_hmac_signing_secret(partner_id: str, request: Request, db: DbDep, admin: AdminUser):
    """Generate a new HMAC envelope secret for the partner.

    Slice 5 of A2A security hardening. The HMAC secret signs the
    X-NPCI-Signature header (payload non-repudiation), distinct from
    the JWT signing secret (identity). Returned ONCE; admin must ship
    it OOB so the partner installs the matching `npci_hmac_secret`.
    A 5-minute grace period allows the previous secret to remain valid
    while the partner installs the new one."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    old_version = partner.signing_secret_version or 1
    new_secret = PartnerAgent.generate_signing_secret()
    partner.previous_signing_secret = partner.signing_secret
    partner.signing_secret = new_secret
    partner.secret_rotated_at = utcnow()
    partner.updated_at = utcnow()
    # T2 (THREAT_MODEL.md) — monotonic version counter, so every
    # A2AMessage row persisted from this point forward records EXACTLY
    # which secret generation verified it, independent of future
    # rotations. Never reset, never reused.
    partner.signing_secret_version = old_version + 1
    db.commit()

    # T8 (THREAT_MODEL.md) — admin action audit. Never the secret VALUES
    # themselves (admin_action_audit.record redacts known secret field
    # names automatically) — only that a rotation happened and which
    # key-version transition it represents.
    admin_action_audit.record(
        db, user_id=admin.id, username=getattr(admin, "username", None),
        action="partner.rotate_hmac_secret", resource_type="partner_agent",
        resource_id=partner_id,
        before={"signing_secret_version": old_version},
        after={"signing_secret_version": partner.signing_secret_version},
        ip=request.client.host if request.client else None,
    )
    # This row is richer than what AdminActionAuditMiddleware could infer
    # (it carries the actual key-version transition), so suppress the
    # generic duplicate for this request.
    mark_explicitly_recorded(request)

    logger.info(
        "Partner HMAC signing secret rotated: id=%s name='%s'",
        partner_id, partner.name,
    )

    resp = _partner_response(partner)
    resp["signing_secret"] = new_secret  # show full secret once
    return resp


@router.delete("/{partner_id}")
def deactivate_partner(partner_id: str, db: DbDep, _: AdminUser):
    """Deactivate a partner (soft delete)."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    partner.status = PartnerStatus.INACTIVE
    partner.updated_at = utcnow()
    db.commit()

    logger.info("Partner deactivated: id=%s name='%s'", partner_id, partner.name)
    return {"deactivated": True, "id": partner_id}


@router.post("/{partner_id}/activate")
def activate_partner(partner_id: str, db: DbDep, _: AdminUser):
    """Re-activate a deactivated partner."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    partner.status = PartnerStatus.ACTIVE
    partner.updated_at = utcnow()
    db.commit()

    logger.info("Partner activated: id=%s name='%s'", partner_id, partner.name)
    return _partner_response(partner)


def _connect_hint(cause: BaseException) -> str:
    """Translate a connect failure into an explanation we author.

    SCR #6. The endpoint used to interpolate the raw `__cause__` into its
    response body. That string is OS-generated and can disclose the resolved
    IP, the proxy in play and libc/socket internals.

    Simply scrubbing it would gut the endpoint: distinguishing "connection
    refused" from "DNS failure" from "no route to host" is the entire reason
    an operator clicks Test. So instead of echoing the error we classify it by
    `errno` — a small integer, not a string — and return prose we wrote, which
    is both safe and more useful than the raw text. The unredacted cause is on
    the logger.warning line at the call site.
    """
    import errno as _errno
    import socket as _socket

    num = getattr(cause, "errno", None)

    if isinstance(cause, _socket.gaierror) or num in (
        getattr(_socket, "EAI_NONAME", None), getattr(_socket, "EAI_AGAIN", None),
    ):
        return ("the hostname could not be resolved — check the name is correct "
                "and that this service shares a network with the partner")
    if num == _errno.ECONNREFUSED:
        return ("the host is reachable but refused the connection — the partner "
                "service is likely not listening on that port")
    if num == _errno.EHOSTUNREACH:
        return "no route to the host — check firewall rules and network placement"
    if num == _errno.ENETUNREACH:
        return "the network is unreachable from this service"
    if num == _errno.ETIMEDOUT:
        return "the connection attempt timed out — a firewall may be dropping packets"
    if num == _errno.ECONNRESET:
        return "the connection was reset by the peer"
    return ("the connection could not be established — see the server log for "
            "the underlying transport error")


@router.post("/{partner_id}/test")
def test_partner_connectivity(partner_id: str, db: DbDep, _: AdminUser):
    """Test connectivity to partner's endpoint (fetch agent card)."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    if not partner.endpoint_url:
        return {"status": "error", "message": "No endpoint URL configured"}

    import httpx
    from app.services.a2a_client import _agent_card_url, partner_verify
    agent_card_url = _agent_card_url(partner)
    # Log the EXACT url the backend is about to probe (this GET runs server-side,
    # from inside the backend container — so the url must be reachable from THERE,
    # not from the operator's browser). Grep 'partner_test' to trace a failure.
    logger.info("partner_test: partner=%s (%s) probing agent card at %s (endpoint_url=%s)",
                partner.id, getattr(partner, "name", "?"), agent_card_url, partner.endpoint_url)

    # Re-check at the SINK, not only at the write boundary: the stored
    # endpoint_url may predate the guard, so validating only on create/update
    # would leave existing rows unchecked.
    #
    # Then PIN the resolved address. Validating a hostname and handing the
    # hostname to httpx means httpx resolves it again moments later — and an
    # attacker controlling the DNS answer can return a public address for our
    # check and 127.0.0.1 for the real fetch (DNS rebinding). `pin_url` resolves
    # once, vets that address, and returns a URL targeting it literally, with the
    # original hostname carried in the Host header.
    from app.core.config import settings as _settings
    from app.core.ssrf_guard import SsrfBlocked, check_outbound_url, parse_allowlist, pin_url
    _allow = parse_allowlist(_settings.ssrf_allowed_internal_hosts)
    _guard_kwargs = dict(
        mode=_settings.ssrf_guard_mode,
        allowlist=_allow,
        allow_private=_settings.ssrf_allow_private_networks,
        block_on_resolution_failure=_settings.ssrf_block_on_resolution_failure,
    )
    fetch_url, fetch_headers = agent_card_url, None
    try:
        check_outbound_url(agent_card_url, context=f"partner_test probe (partner={partner.id})",
                           **_guard_kwargs)
        pinned = pin_url(agent_card_url, context=f"partner_test probe (partner={partner.id})",
                         **_guard_kwargs)
        if pinned is not None:
            # Only pin plain HTTP. For https:// the certificate is issued to the
            # hostname, so fetching an IP literal would fail verification — a
            # self-inflicted outage. TLS is not rebinding-exploitable in the same
            # way: the attacker's rebound host still has to present a certificate
            # this client trusts, which is the F-002 control, not this one.
            if urlparse(agent_card_url).scheme == "http":
                fetch_url, fetch_headers = pinned.url, pinned.headers
                logger.info("partner_test: pinned %s to %s (Host: %s)",
                            agent_card_url, pinned.url, pinned.host_header)
    except SsrfBlocked as exc:
        return {"status": "error", "url": agent_card_url,
                "message": f"Refused to probe this endpoint: {exc.reason}"}

    try:
        resp = httpx.get(fetch_url, timeout=10.0, verify=partner_verify(partner),
                         headers=fetch_headers)
        logger.info("partner_test: partner=%s %s -> HTTP %d", partner.id, agent_card_url, resp.status_code)
        if resp.status_code == 200:
            partner.agent_card_url = agent_card_url
            db.commit()
            return {"status": "ok", "message": f"Agent card fetched from {agent_card_url}", "agent_card": resp.json()}
        return {"status": "warning", "url": agent_card_url, "http_status": resp.status_code,
                "message": f"Endpoint reachable but agent card returned {resp.status_code}"}

    except httpx.ConnectError as e:
        # ConnectError wraps the OS-level reason in __cause__ — this is the single
        # most useful diagnostic: 'Connection refused' (host up, nothing on that
        # port / service down), 'Name or service not known' / 'nodename nor servname'
        # (DNS — wrong service name or not on the backend's network), 'No route to
        # host' (firewall / wrong network). 'localhost'/'127.0.0.1' here means the
        # backend container itself, not the partner.
        cause = e.__cause__ or e
        logger.warning("partner_test: partner=%s CONNECT FAILED to %s — %r",
                       partner.id, agent_card_url, cause, exc_info=True)
        # SCR #6: this dict is the response body of POST /{partner_id}/test.
        # The raw `cause` is an OS-level error whose text can carry the
        # resolved address, the proxy in use and libc detail. The DIAGNOSIS is
        # what the operator needs, though — losing it would make this endpoint
        # useless — so the errno is translated into an explanation we author.
        # `agent_card_url` itself is safe: the admin just supplied it.
        return {"status": "error", "url": agent_card_url, "error_class": "ConnectError",
                "message": f"Cannot connect to {agent_card_url}: {_connect_hint(cause)}"}
    except httpx.TimeoutException as e:
        logger.warning("partner_test: partner=%s TIMEOUT to %s after 10s — %r",
                       partner.id, agent_card_url, e, exc_info=True)
        return {"status": "error", "url": agent_card_url, "error_class": "Timeout",
                "message": f"Timed out connecting to {agent_card_url} (10s) — "
                           "host reachable but slow, or a firewall is dropping packets."}
    except Exception as e:
        logger.warning("partner_test: partner=%s FAILED for %s — %r",
                       partner.id, agent_card_url, e, exc_info=True)
        # SCR #6: `type(e).__name__` is a bare class name and is safe to keep —
        # it is the fastest triage signal for the operator. The rendered
        # message is not, so it goes through the allowlist.
        return {"status": "error", "url": agent_card_url, "error_class": type(e).__name__,
                "message": f"{type(e).__name__}: {client_safe_detail(e)}"}


# ── A2A protocol switch (Slice 6) ────────────────────────────────────────────
#
# Operators flip individual partners between the legacy hand-rolled POST
# and the SDK JSON-RPC wire. The column already exists (alembic 0033) and
# `services/a2a_client.send_task_to_partner` reads it on every outbound
# call (Slice 5). This endpoint just makes the flip clickable from the UI.
#
# No JSON Schema enum on the request body because pydantic 2's
# `Literal['legacy', 'a2a_sdk']` would reject the legacy column default
# round-tripping in older clients; a runtime check is friendlier.

@router.patch("/{partner_id}/protocol")
def update_partner_protocol(
    partner_id: str,
    body: UpdateProtocolRequest,
    db: DbDep,
    _: AdminUser,
):
    """Flip a partner's outbound A2A wire between 'legacy' and 'a2a_sdk'.

    The next call to `send_task_to_partner` for this partner picks up
    the new value — no app restart needed. Idempotent: setting to the
    same value is a no-op (logs but does not write).
    """
    if body.protocol_version not in _VALID_PROTOCOLS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid protocol_version '{body.protocol_version}'. "
                f"Valid: {list(_VALID_PROTOCOLS)}"
            ),
        )

    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    old = getattr(partner, "protocol_version", "legacy")
    if old == body.protocol_version:
        logger.info(
            "Partner protocol unchanged: id=%s name='%s' protocol=%s",
            partner.id, partner.name, old,
        )
        return _partner_response(partner)

    partner.protocol_version = body.protocol_version
    db.commit()
    db.refresh(partner)

    logger.info(
        "Partner protocol updated: id=%s name='%s' %s → %s",
        partner.id, partner.name, old, body.protocol_version,
    )
    return _partner_response(partner)


# ── Slice 6 — mTLS tier + cert fingerprint ───────────────────────────────────


@router.patch("/{partner_id}/tls-tier")
def update_tls_tier(
    partner_id: str,
    body: UpdateTlsTierRequest,
    db: DbDep,
    _: AdminUser,
):
    """Switch a partner between the JWT-only ingress (:443) and the
    mTLS ingress (:8443). Account-holding banks (remitter/beneficiary)
    → 'mtls'; PSPs (payer/payee) and cert_engine stay on 'jwt'.

    Flipping to 'mtls' without first registering a
    `client_cert_fingerprint` is allowed but the partner will get
    `mtls_not_provisioned` 401s until the fingerprint lands. The two
    endpoints are independent on purpose — operators can stage a
    fingerprint before flipping the tier (or reverse the order
    during rotation)."""
    if body.tls_tier not in _VALID_TLS_TIERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid tls_tier '{body.tls_tier}'. "
                f"Valid: {list(_VALID_TLS_TIERS)}"
            ),
        )

    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    old = getattr(partner, "tls_tier", "jwt")
    if old == body.tls_tier:
        return _partner_response(partner)

    partner.tls_tier = body.tls_tier
    partner.updated_at = utcnow()
    db.commit()
    db.refresh(partner)

    logger.info(
        "Partner tls_tier changed: id=%s name='%s' %s → %s",
        partner.id, partner.name, old, body.tls_tier,
    )
    return _partner_response(partner)


@router.patch("/{partner_id}/cert-fingerprint")
def update_cert_fingerprint(
    partner_id: str,
    body: UpdateCertFingerprintRequest,
    db: DbDep,
    _: AdminUser,
):
    """Register / update the SHA-256 fingerprint of the bank's pinned
    client cert (Slice 6 of A2A security hardening). nginx forwards
    `$ssl_client_fingerprint` as `X-Client-Cert-Fingerprint`; the
    auth middleware compares against this value when
    `tls_tier == 'mtls'`.

    The fingerprint MUST be lowercase hex, exactly 64 chars (no colon
    separators — nginx already emits the unseparated form). Pass null
    to clear; only safe when the partner is back on tls_tier='jwt'.
    """
    fp = body.client_cert_fingerprint
    if fp is not None:
        fp = fp.strip().lower().replace(":", "")
        if len(fp) != 64 or not all(c in "0123456789abcdef" for c in fp):
            raise HTTPException(
                status_code=400,
                detail="client_cert_fingerprint must be 64-char lowercase hex (SHA-256).",
            )

    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    old = partner.client_cert_fingerprint
    partner.client_cert_fingerprint = fp
    partner.updated_at = utcnow()
    db.commit()
    db.refresh(partner)

    logger.info(
        "Partner cert fingerprint updated: id=%s name='%s' old=%s new=%s",
        partner.id, partner.name, _mask_fp(old), _mask_fp(fp),
    )
    return _partner_response(partner)


def _mask_fp(fp: str | None) -> str:
    """Show first/last 4 of a 64-char hex fingerprint for log lines —
    full value is in the DB column already, no point repeating."""
    if not fp:
        return "<none>"
    return f"{fp[:4]}…{fp[-4:]}"


# ── Outbound TLS — per-partner verify toggle + CA cert upload ────────────────


@router.patch("/{partner_id}/ssl-verify")
def update_partner_ssl_verify(
    partner_id: str, body: UpdateSslVerifyRequest, db: DbDep, admin: AdminUser,
):
    """Toggle outbound TLS verification for this partner's HTTPS endpoint.
    true = verify · false = skip (self-signed/internal) · null = inherit the
    global PARTNER_TLS_VERIFY. Applies to the Test probe AND real A2A calls."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    previous = partner.ssl_verify
    partner.ssl_verify = body.ssl_verify
    partner.updated_at = utcnow()
    db.commit()
    db.refresh(partner)
    if body.ssl_verify is False:
        # Same event name and severity a2a_client.partner_verify emits when it
        # ACTS on this flag, so one alert rule catches both the moment an admin
        # turns verification off and the first outbound call that skips it.
        # Without this the disable is only visible once traffic flows, which
        # can be long after the change that caused it.
        logger.warning(
            "SECURITY_EVENT event=tls_verification_disabled severity=medium "
            "partner_id=%s partner_name=%r actor=%r previous=%s "
            "decision=allowed detail=\"admin disabled outbound TLS certificate "
            "verification for this partner — prefer uploading the partner's CA "
            "via PATCH /partners/{id}/ca-cert over disabling verification\"",
            partner.id, partner.name, getattr(admin, "username", None), previous,
        )
    else:
        logger.info("Partner ssl_verify updated: id=%s name='%s' ssl_verify=%s",
                    partner.id, partner.name, body.ssl_verify)
    return _partner_response(partner)


@router.patch("/{partner_id}/max-inline-attachment")
def update_partner_max_inline_attachment(
    partner_id: str, body: UpdateMaxInlineAttachmentRequest, db: DbDep, _: AdminUser,
):
    """Set the per-partner inline-attachment size cap (base64/wire bytes). Kit
    attachments larger than this are omitted from the A2A envelope so a send
    stays under the partner's ingress body limit. null = inherit the global
    PARTNER_MAX_INLINE_ATTACHMENT_BYTES; 0 = no limit."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    if body.max_inline_attachment_bytes is not None and body.max_inline_attachment_bytes < 0:
        raise HTTPException(status_code=400, detail="max_inline_attachment_bytes must be >= 0")
    partner.max_inline_attachment_bytes = body.max_inline_attachment_bytes
    partner.updated_at = utcnow()
    db.commit()
    db.refresh(partner)
    logger.info("Partner max_inline_attachment_bytes updated: id=%s name='%s' value=%s",
                partner.id, partner.name, body.max_inline_attachment_bytes)
    return _partner_response(partner)


@router.post("/{partner_id}/ca-cert")
def upload_partner_ca_cert(
    partner_id: str, body: UploadCaCertRequest, db: DbDep, _: AdminUser,
):
    """Upload (or clear) the CA/cert PEM trusted for this partner's HTTPS
    endpoint. Validated as a real X.509 PEM before storing — a bad paste is
    rejected (400) rather than silently breaking every call to this partner.
    Empty body clears it (falls back to global PARTNER_CA_BUNDLE / system trust)."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    pem = (body.ca_cert_pem or "").strip()
    if pem:
        import ssl
        try:
            ssl.create_default_context().load_verify_locations(cadata=pem)
        except ssl.SSLError as e:
            # SCR #6: `ssl.SSLError` is a third-party type on no allowlist, and
            # its text is OpenSSL-generated — it renders as
            # "no start line: cadata does not contain a certificate
            #  (_ssl.c:4282)", disclosing library internals and a C source
            # line number. The operator only needs to know the PEM was
            # rejected; the detail is on the log line.
            logger.warning("partner ca_cert rejected: partner=%s error=%s",
                           partner_id, e)
            raise HTTPException(
                status_code=400,
                detail="Not a valid PEM certificate",
            )
    partner.ca_cert_pem = pem or None
    partner.updated_at = utcnow()
    db.commit()
    db.refresh(partner)
    logger.info("Partner ca_cert %s: id=%s name='%s' (%d bytes)",
                "uploaded" if pem else "cleared", partner.id, partner.name, len(pem))
    return _partner_response(partner)


@router.get("/{partner_id}/ca-cert")
def get_partner_ca_cert(partner_id: str, db: DbDep, _: AdminUser):
    """Return the stored CA/cert PEM so the UI can show/edit it. Admin-only."""
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    return {"partner_id": partner.id, "ca_cert_pem": partner.ca_cert_pem or ""}


# ── Slice 7 — CIDR allowlist + rate-limit override ───────────────────────────


@router.patch("/{partner_id}/allowed-cidrs")
def update_allowed_cidrs(
    partner_id: str,
    body: UpdateAllowedCidrsRequest,
    db: DbDep,
    _: AdminUser,
):
    """Replace the partner's allowed_cidrs list (Slice 7 of A2A
    security hardening). Each entry must parse as a CIDR via the
    stdlib `ipaddress` module; an unparseable entry is rejected at
    write time so the auth middleware never sees garbage. Pass null
    or an empty list to clear (= no IP enforcement)."""
    cidrs = body.allowed_cidrs or []
    if cidrs:
        import ipaddress
        for c in cidrs:
            try:
                ipaddress.ip_network(c, strict=False)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid CIDR {c!r}: {exc}",
                )

    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    partner.allowed_cidrs = cidrs or None
    partner.updated_at = utcnow()
    db.commit()
    db.refresh(partner)

    logger.info(
        "Partner allowed_cidrs updated: id=%s name='%s' count=%d",
        partner.id, partner.name, len(cidrs),
    )
    return _partner_response(partner)


@router.patch("/{partner_id}/rate-limit")
def update_rate_limit(
    partner_id: str,
    body: UpdateRateLimitRequest,
    request: Request,
    db: DbDep,
    admin: AdminUser,
):
    """Set the partner's per-second rate cap. Stored on the row;
    enforcement at nginx level is a flat zone today (Slice 9 may wire
    a per-partner override). The column makes the per-partner cap
    visible in the admin UI and unblocks Slice 9's dynamic-zone
    work."""
    if not (1 <= body.rate_limit_rps <= 10_000):
        raise HTTPException(
            status_code=400,
            detail="rate_limit_rps must be between 1 and 10000.",
        )

    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    old = getattr(partner, "rate_limit_rps", 100)
    if old == body.rate_limit_rps:
        return _partner_response(partner)

    partner.rate_limit_rps = body.rate_limit_rps
    partner.updated_at = utcnow()
    db.commit()
    db.refresh(partner)

    # T8 (THREAT_MODEL.md) — admin action audit for a representative
    # non-secret admin action (see rotate-hmac-secret above for the
    # secret-bearing case).
    admin_action_audit.record(
        db, user_id=admin.id, username=getattr(admin, "username", None),
        action="partner.rate_limit_update", resource_type="partner_agent",
        resource_id=partner_id,
        before={"rate_limit_rps": old}, after={"rate_limit_rps": body.rate_limit_rps},
        ip=request.client.host if request.client else None,
    )
    # Richer than the generic middleware row (carries the actual rps diff).
    mark_explicitly_recorded(request)

    logger.info(
        "Partner rate_limit_rps updated: id=%s name='%s' %d → %d",
        partner.id, partner.name, old, body.rate_limit_rps,
    )
    return _partner_response(partner)


# ── A2A session revocation (Slice 2 of security hardening) ───────────────────


@router.post("/{partner_id}/revoke-sessions")
def revoke_partner_sessions(partner_id: str, db: DbDep, _: AdminUser):
    """Mark every active A2ASession row for this partner as revoked.

    The next inbound JWT validated by `SdkAuthMiddleware` will hit a
    revoked_at-IS-NOT-NULL row and be refused with 401 session_revoked.
    Use this when:
      * a partner reports its api_key / refresh token as compromised
      * the admin disables a partner mid-traffic (status flip alone
        isn't enough — old JWTs in flight remain valid until expiry)

    Idempotent: revoking already-revoked sessions is a no-op.
    """
    from datetime import datetime, timezone
    from app.models.phase_c import A2ASession

    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    now = datetime.now(timezone.utc)
    revoked = (
        db.query(A2ASession)
          .filter(
              A2ASession.partner_id == partner_id,
              A2ASession.revoked_at.is_(None),
          )
          .update({"revoked_at": now}, synchronize_session=False)
    )
    db.commit()

    logger.info(
        "Partner sessions revoked: id=%s name='%s' count=%d",
        partner.id, partner.name, revoked,
    )
    return {"partner_id": partner_id, "revoked_count": revoked, "revoked_at": now.isoformat()}


@router.get("/stats")
def partner_stats(db: DbDep, _: AdminUser):
    """Get partner statistics."""
    total = db.scalar(select(func.count(PartnerAgent.id))) or 0
    active = db.scalar(select(func.count(PartnerAgent.id)).where(PartnerAgent.status == PartnerStatus.ACTIVE)) or 0

    # Count partners that include each type in their array
    all_partners = db.scalars(select(PartnerAgent)).all()
    by_type = {pt.value: 0 for pt in PartnerType}
    for p in all_partners:
        types = _normalize_types(p.partner_type)
        for t in types:
            if t in by_type:
                by_type[t] += 1

    return {
        "total": total,
        "active": active,
        "inactive": total - active,
        "by_type": by_type,
    }
