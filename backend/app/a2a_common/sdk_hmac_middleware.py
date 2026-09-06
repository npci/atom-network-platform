# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ASGI middleware that enforces the HMAC envelope on inbound A2A calls.

Slice 5 of the A2A security hardening. Runs as an ASGI raw middleware
(NOT BaseHTTPMiddleware) because it has to read the full request body
to compute the HMAC and then replay that body to the inner app — a
pattern BaseHTTPMiddleware can't express cleanly without breaking
downstream `request.body()` reads.

Wraps the SDK sub-app OUTSIDE of `SdkAuthMiddleware`:

    middleware=[
        Middleware(SdkHmacMiddleware),     # outer — verifies envelope
        Middleware(SdkAuthMiddleware),     # inner — verifies JWT + sets contextvar
    ]

Outer-first because the HMAC check is partner-scoped, and we need the
JWT's `sub` claim to look up `partner.signing_secret`. We DON'T verify
the JWT signature here (that's auth's job) — we only parse the claim
to pick a key. This is the same pattern JWS uses with `kid`.

Back-compat: a partner row with `signing_secret IS NULL` skips the
envelope check (warn-once log) so partners not yet upgraded keep
working.

Public surface:
    SdkHmacMiddleware(app, *, paths_skip=("/.well-known/",))
"""
from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass
from typing import Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .hmac_signer import (
    DEFAULT_MAX_SKEW_S, DEFAULT_NONCE_TTL_S,
    HEADER_NONCE, HEADER_SIGNATURE, HEADER_TIMESTAMP,
    verify as hmac_verify,
)

logger = logging.getLogger(__name__)


@dataclass
class HmacAuditMeta:
    """Closes THREAT_MODEL.md T2 (non-repudiation). Populated by
    SdkHmacMiddleware for EVERY inbound body-bearing request that carries
    a valid signature and read by `authority_executor.py` when it
    persists the `A2AMessage` audit row — contextvars propagate down the
    same async call chain this middleware invokes (`await
    self.app(...)`), so no explicit parameter threading through the SDK's
    own dispatch internals is needed.

    Deliberately does NOT carry a payload hash: this middleware only
    ever sees the RAW WIRE BODY (the full JSON-RPC envelope, pre-protobuf
    parsing), which is a different byte structure than the `data` dict
    `authority_executor.py` extracts and persists as `A2AMessage.payload`
    — hashing the wire body here and comparing it against the persisted
    `payload` column later would never match, by construction, not just
    due to incidental re-serialization drift. T1's integrity hash is
    therefore computed in `authority_executor.py` directly from `data`
    (the exact object that becomes the `payload` column) at the moment
    of persistence — see that module's `A2AMessage(...)` construction."""

    hmac_signature: str | None = None       # T2 — non-repudiation evidence (None if unsigned/back-compat)
    hmac_key_version: int | None = None     # T2 — which signing_secret_version verified it


HMAC_AUDIT_CONTEXT: contextvars.ContextVar[Optional[HmacAuditMeta]] = contextvars.ContextVar(
    "a2a_hmac_audit_context", default=None,
)


def get_hmac_audit_meta() -> HmacAuditMeta:
    """Helper for authority_executor.py — returns the current request's
    HMAC audit metadata, or an all-None instance outside a
    SdkHmacMiddleware-wrapped request scope (so callers never need to
    special-case a missing contextvar)."""
    ctx = HMAC_AUDIT_CONTEXT.get()
    return ctx if ctx is not None else HmacAuditMeta()

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001 — settings must be importable in prod; degrade
    # gracefully only for isolated unit tests that stub this module out.
    _settings = None


class _BodyTooLarge(Exception):
    """Internal signal — the inbound body exceeded a2a_max_request_body_bytes
    while still being read off the wire (A8)."""


class SdkHmacMiddleware:
    """ASGI middleware. Verifies X-NPCI-Signature on POST/PUT/PATCH;
    GET/HEAD/OPTIONS pass through (no body to sign)."""

    _BODY_METHODS = {"POST", "PUT", "PATCH"}

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths_skip: tuple[str, ...] = ("/.well-known/",),
        max_skew_s: int = DEFAULT_MAX_SKEW_S,
        nonce_ttl_s: int = DEFAULT_NONCE_TTL_S,
    ) -> None:
        self.app = app
        self._skip = paths_skip
        self._max_skew_s = max_skew_s
        self._nonce_ttl_s = nonce_ttl_s
        self._warned_no_secret: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if any(path.startswith(p) for p in self._skip) or method not in self._BODY_METHODS:
            await self.app(scope, receive, send)
            return

        # A8 (architecture review, see security_architecture_skills.md §16
        # "gateway-only security" is a prohibited anti-pattern; §11.1 requires
        # size limits on every inbound request/message, independent of any
        # edge proxy). Reject an oversized body BEFORE it is fully buffered
        # into memory, rather than relying solely on nginx's
        # `client_max_body_size`. A layer-only-at-the-edge control is exactly
        # what an operator bypasses when the backend's own port is reachable
        # (see A0 in docs/ARCHITECTURE_REVIEW_ACTIONS.md).
        max_body = int(getattr(_settings, "a2a_max_request_body_bytes", 0) or 0)
        try:
            body = await _read_body(receive, max_bytes=max_body if max_body > 0 else None)
        except _BodyTooLarge as e:
            logger.warning(
                "a2a_body_too_large path=%s limit_bytes=%d",
                path, max_body,
            )
            await _send_json(send, 413, {
                "error": "request_body_too_large",
                "detail": f"Request body exceeds the configured limit of {max_body} bytes.",
            })
            return

        # Extract partner_id from JWT (claim only — sig verification
        # happens in the inner SdkAuthMiddleware). If no/invalid token,
        # let auth handle the rejection — we just pass through here so
        # the structured error from auth ("missing_bearer_token") wins
        # over an HMAC-flavoured one.
        partner_id = _partner_id_from_auth_header(scope)
        if not partner_id:
            await _replay(self.app, scope, body, send)
            return

        # Load signing_secret. Lazy DB import — same reason as auth
        # middleware: avoid pulling SQLAlchemy at module load.
        from app.core.database import SessionLocal
        from app.models.phase_c import PartnerAgent

        db = SessionLocal()
        try:
            partner = db.get(PartnerAgent, partner_id)
            secret = partner.signing_secret if partner else None
        finally:
            db.close()

        if not secret:
            # Check if the request carries an HMAC signature header.
            # A signed request to a partner with no configured secret is
            # suspicious — reject it. An unsigned request is backwards-
            # compatible for partners not yet onboarded onto the envelope.
            raw_headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            has_sig = bool(raw_headers.get(HEADER_SIGNATURE.lower()))
            if has_sig:
                logger.warning(
                    "SdkHmacMiddleware: partner_id=%s sent HMAC signature but "
                    "has no signing_secret configured — rejecting.",
                    partner_id,
                )
                await _send_json(send, 401, {
                    "error": "hmac_secret_not_configured",
                    "detail": "Partner sent HMAC signature but has no signing secret configured.",
                })
                return
            # No signature and no secret — backwards-compatible pass-through.
            # A7: the startup validator (core/startup_validation.py) refuses to
            # boot when an ACTIVE partner is in this state, so reaching this
            # branch at runtime means either the operator explicitly opted out
            # (a2a_require_hmac_for_active_partners=False) or the partner is
            # not ACTIVE. Either way this is still a security-relevant event
            # per security_architecture_skills.md §13.2 ("configuration
            # validation failures" / "signature failures" MUST be structured
            # telemetry), not just a plain warning log.
            if partner_id not in self._warned_no_secret:
                logger.warning(
                    "SECURITY_EVENT event=hmac_envelope_bypassed severity=medium "
                    "partner_id=%s reason=no_signing_secret decision=allowed "
                    "detail=\"accepting request without envelope check — configure via "
                    "POST /admin/partners/{id}/rotate-hmac-secret\"",
                    partner_id,
                )
                self._warned_no_secret.add(partner_id)
            # Unsigned/back-compat request — no signature to record (T2
            # does not apply). Leave the contextvar at its default (all
            # None); T1's integrity hash is computed independently in
            # authority_executor.py regardless of whether this request
            # was signed.
            await _replay(self.app, scope, body, send)
            return

        # Pull headers as a case-insensitive dict.
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        # `verify` does its own case-insensitive lookup against the
        # canonical header names.
        envelope_headers = {
            HEADER_TIMESTAMP: headers.get(HEADER_TIMESTAMP.lower()),
            HEADER_NONCE:     headers.get(HEADER_NONCE.lower()),
            HEADER_SIGNATURE: headers.get(HEADER_SIGNATURE.lower()),
        }

        redis_client = _get_redis_client()
        ok, err = hmac_verify(
            envelope_headers, body, secret,
            redis_client=redis_client,
            max_skew_s=self._max_skew_s,
            nonce_ttl_s=self._nonce_ttl_s,
        )
        # T2: which key version verified this request — set as soon as we
        # know, so both the "verified on the first try" and "verified via
        # grace-period fallback" branches below agree with reality. Starts
        # as the CURRENT version; the grace-period branch corrects it to
        # "current - 1" only if the fallback secret is what actually
        # matched (never guessed — always tied to which `hmac_verify` call
        # returned ok=True).
        key_version = getattr(partner, "signing_secret_version", 1) or 1
        if not ok:
            # Grace-period fallback: if the current secret fails and the
            # partner has a previous_signing_secret that was rotated within
            # the last 5 minutes, try that before rejecting.
            retried = False
            if err == "signature_mismatch" and partner:
                prev_secret = getattr(partner, "previous_signing_secret", None)
                rotated_at = getattr(partner, "secret_rotated_at", None)
                if prev_secret and rotated_at:
                    from datetime import datetime, timezone, timedelta
                    if rotated_at.tzinfo is None:
                        rotated_at = rotated_at.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - rotated_at <= timedelta(minutes=5):
                        ok2, err2 = hmac_verify(
                            envelope_headers, body, prev_secret,
                            redis_client=redis_client,
                            max_skew_s=self._max_skew_s,
                            nonce_ttl_s=self._nonce_ttl_s,
                        )
                        if ok2:
                            ok, err = True, None
                            retried = True
                            # The PREVIOUS secret matched — this request was
                            # signed with the version before the current one.
                            key_version = max(key_version - 1, 1)
                            logger.info(
                                "a2a_hmac_grace partner_id=%s used previous_signing_secret",
                                partner_id,
                            )

            if not ok:
                await _send_json(send, 401, {"error": err or "envelope_invalid",
                                              "detail": "HMAC envelope check failed."})
                logger.warning("a2a_hmac_reject partner_id=%s code=%s", partner_id, err)
                return

        # T2: expose the verified signature and its key version to
        # authority_executor.py via the contextvar — this is the "signed
        # and verified" case, the strongest non-repudiation evidence this
        # platform can record.
        HMAC_AUDIT_CONTEXT.set(HmacAuditMeta(
            hmac_signature=envelope_headers.get(HEADER_SIGNATURE),
            hmac_key_version=key_version,
        ))
        await _replay(self.app, scope, body, send)


# ── helpers ──────────────────────────────────────────────────────────────────


async def _read_body(receive: Receive, max_bytes: int | None = None) -> bytes:
    """Drain the ASGI receive stream and return concatenated body bytes.

    A8: when `max_bytes` is set, aborts as soon as the running total exceeds
    it — raising `_BodyTooLarge` — instead of buffering the full (possibly
    multi-GB) body before rejecting it. This bounds the memory a single
    request can force the process to allocate, independent of anything the
    edge proxy enforces."""
    chunks: list[bytes] = []
    total = 0
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            # http.disconnect or unexpected — treat as empty
            break
        chunk = msg.get("body", b"") or b""
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise _BodyTooLarge(f"{total} bytes > limit {max_bytes}")
        chunks.append(chunk)
        if not msg.get("more_body", False):
            break
    return b"".join(chunks)


async def _replay(app: ASGIApp, scope: Scope, body: bytes, send: Send) -> None:
    """Call the inner ASGI app with a receive that replays the buffered body."""
    sent = False

    async def replay_receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    await app(scope, replay_receive, send)


async def _send_json(send: Send, status: int, payload: dict) -> None:
    """Emit a JSON ASGI response without going through Starlette's Response."""
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _partner_id_from_auth_header(scope: Scope) -> Optional[str]:
    """Best-effort extraction of the JWT `sub` claim from the
    Authorization header. We deliberately DO NOT verify the signature
    here — that's the auth middleware's job further down the stack.
    A failure here returns None and lets the request proceed; the
    auth middleware will reject with `invalid_token`."""
    auth = None
    for k, v in scope.get("headers", []):
        if k.decode("latin-1").lower() == "authorization":
            auth = v.decode("latin-1")
            break
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth.split(None, 1)[1].strip()
    if not token:
        return None

    # Lazy import — keep startup light.
    try:
        import jwt as _jwt
        # Deliberately UNVERIFIED: this reads `sub` only to look up the
        # partner's signing_secret, and the explicit `verify_signature: False`
        # option is the guard rail. Turning signature verification off also
        # disables every other claim check (exp/aud/iss), which is exactly the
        # semantics of python-jose's `get_unverified_claims` that this replaced;
        # no key and no `algorithms` argument are needed in this mode.
        # The signature IS verified for the same request by the inner
        # SdkAuthMiddleware (mounted at main.py:214-215, outer=HMAC /
        # inner=auth), which calls
        # app.core.security.decode_partner_token -> jwt.decode(..., algorithms=[ALGORITHM]).
        # A bad signature therefore still fails the request, after this line.
        # If you ever use a claim from here for a TRUST decision, verify first.
        claims = _jwt.decode(token, options={"verify_signature": False})
        sub = claims.get("sub")
        return str(sub) if sub else None
    except Exception:  # noqa: BLE001
        return None


def _get_redis_client():
    """Lazy redis. Reuses the same connection helper as job_registry so
    nonce uniqueness shares the production-tested redis wiring."""
    try:
        from app.services.job_registry import _get_redis
        return _get_redis()
    except Exception:  # noqa: BLE001
        return None
