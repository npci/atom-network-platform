# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Starlette middleware that authenticates inbound A2A JSON-RPC calls.

Wraps the SDK sub-app returned by `build_a2a_components`. Validates
`Authorization: Bearer <jwt>`, ties the JWT hash to a non-revoked
`A2ASession` row, and stashes the resolved `PartnerAgent` on a
`contextvars.ContextVar` that the executor reads.

Slice 2 of the A2A security hardening. Future slices extend this same
middleware with HMAC envelope verification (Slice 5), mTLS fingerprint
pinning (Slice 6), CIDR allowlist enforcement (Slice 7), and audit
metadata population (Slice 8). Each adds one block; the order below
is fixed to fail fast on the cheapest checks first.

Public surface:
    AUTH_CONTEXT          — contextvars.ContextVar holding (partner, audit_meta)
    SdkAuthMiddleware     — Starlette BaseHTTPMiddleware subclass
    get_authenticated_partner() -> PartnerAgent
        Helper for executors: pulls the partner out of the contextvar.
        Raises RuntimeError if called outside a request scope (test
        harness, or a request that bypassed the middleware somehow).
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.core.config import settings

logger = logging.getLogger(__name__)


# ── Context plumbing ─────────────────────────────────────────────────────────


@dataclass
class AuthContext:
    """Per-request bundle the middleware passes to the executor.

    `partner` is the SQLAlchemy row (eager-loaded by the middleware so
    the executor doesn't repeat the DB hit). `audit_meta` carries
    everything Slice 8 wants to persist on the `A2AMessage` audit row —
    populated incrementally as later slices add fields (caller_ip in
    this slice; jwt_sub/jwt_iat/jwt_exp also here; client_cert_fingerprint
    arrives in Slice 6).
    """

    partner: Any  # PartnerAgent — typed Any to avoid circular import at module load
    audit_meta: dict = field(default_factory=dict)


AUTH_CONTEXT: contextvars.ContextVar[Optional[AuthContext]] = contextvars.ContextVar(
    "a2a_auth_context", default=None,
)


def get_authenticated_partner() -> Any:
    """Helper for executors. Returns the `PartnerAgent` row attached to
    the current request by `SdkAuthMiddleware`. Raises if called
    outside a middleware-wrapped request scope."""
    ctx = AUTH_CONTEXT.get()
    if ctx is None or ctx.partner is None:
        raise RuntimeError(
            "No authenticated partner in context. SdkAuthMiddleware must wrap "
            "the SDK sub-app, and the executor must run inside a request flow."
        )
    return ctx.partner


def get_audit_meta() -> dict:
    """Helper for executors / handlers — read the audit metadata
    populated by the middleware. Returns an empty dict outside a
    request scope (so write paths don't have to special-case)."""
    ctx = AUTH_CONTEXT.get()
    return dict(ctx.audit_meta) if ctx else {}


# ── Middleware ───────────────────────────────────────────────────────────────


class SdkAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer JWT + active A2ASession on every JSON-RPC call.

    Allowlist `paths_skip_auth` is a tuple of URL prefixes that bypass
    the check entirely (e.g. `/.well-known/agent-card.json` so SDK
    clients can discover the card without a token). Defaults to
    well-known paths only.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths_skip_auth: tuple[str, ...] = ("/.well-known/",),
    ) -> None:
        super().__init__(app)
        self._skip_paths = paths_skip_auth

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self._skip_paths):
            # No auth on agent-card / well-known. Future slices may also
            # exempt /healthz here; today the SDK sub-app doesn't expose one.
            return await call_next(request)

        auth_header = request.headers.get("authorization") or ""
        if not auth_header.lower().startswith("bearer "):
            return _err(401, "missing_bearer_token", "Authorization header required.")

        token = auth_header.split(None, 1)[1].strip()
        if not token:
            return _err(401, "missing_bearer_token", "Empty Bearer token.")

        # Imports inside the dispatch path — Starlette mounts this at app
        # startup, before SQLAlchemy / models are guaranteed wired. Lazy
        # imports break the circular `app.core.config -> Settings ->
        # app.a2a_common -> models` graph that the unit-test harness hits.
        from app.core.database import SessionLocal
        from app.core.security import decode_partner_token
        from app.models.phase_c import A2ASession, PartnerAgent, PartnerStatus

        partner_id, token_type = decode_partner_token(token)
        if not partner_id or token_type != "a2a":
            return _err(401, "invalid_token", "JWT signature invalid or wrong type.")

        token_hash = hashlib.sha256(token.encode()).hexdigest()

        db = SessionLocal()
        try:
            # Session check — written by /api/a2a/auth, validated here.
            # Slice 9 adds .refresh_token_hash + .refreshed_at; Slice 2
            # only cares about the access-token row.
            session = (
                db.query(A2ASession)
                  .filter(A2ASession.jwt_token_hash == token_hash)
                  .first()
            )
            if session is None:
                return _err(401, "session_unknown", "JWT not registered with server.")
            if getattr(session, "revoked_at", None) is not None:
                return _err(401, "session_revoked", "Session has been revoked.")
            # Expiry — JWT itself carries `exp` and decode_partner_token
            # already enforces it. We rely on that, but also defend
            # against clock drift between issuer + validator by checking
            # the row's `expires_at`.
            from datetime import datetime, timezone
            expires_at = session.expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                # SQLite (test harness) returns naive datetimes; the column
                # is written UTC-aware, so assume UTC rather than raising
                # on the naive/aware comparison.
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at and expires_at < datetime.now(timezone.utc):
                return _err(401, "session_expired", "Session row expired.")

            # Partner status — mirrors core.deps.get_current_partner.
            partner = db.get(PartnerAgent, partner_id)
            if partner is None:
                return _err(401, "partner_unknown", "Partner not registered.")
            if partner.status != PartnerStatus.ACTIVE:
                return _err(401, "partner_inactive", f"Partner status is {partner.status.value}.")

            # ── Slice 6 — mTLS enforcement for bank-tier partners ────
            # Banks present a client cert at nginx :8443; nginx forwards
            # the SHA-256 fingerprint via X-Client-Cert-Fingerprint.
            # Both presence and exact-match against `client_cert_fingerprint`
            # are required. JWT-tier partners skip this check.
            if getattr(partner, "tls_tier", "jwt") == "mtls":
                presented = request.headers.get("x-client-cert-fingerprint")
                if not presented:
                    return _err(
                        401, "mtls_required",
                        "Bank-tier partner must call via the mTLS ingress (:8443).",
                    )
                expected = (partner.client_cert_fingerprint or "").lower()
                if not expected:
                    return _err(
                        401, "mtls_not_provisioned",
                        "Partner is tier=mtls but no client_cert_fingerprint is registered. "
                        "Operator must PATCH /admin/partners/{id}/cert-fingerprint.",
                    )
                if presented.lower() != expected:
                    return _err(
                        401, "mtls_fingerprint_mismatch",
                        "Client cert fingerprint does not match the registered value.",
                    )

            # ── Slice 7 — CIDR allowlist enforcement ──────────────────
            # `partner.allowed_cidrs` is a JSON list of CIDR strings.
            # NULL or empty = no enforcement (current behaviour for
            # partners not yet onboarded onto the allowlist). Caller
            # IP comes from X-Real-IP (set by nginx), with a fall-back
            # to X-Forwarded-For's first hop and finally to the raw
            # ASGI `client.host` so the test harness can inject a
            # synthetic IP.
            allowed = getattr(partner, "allowed_cidrs", None) or []
            if allowed:
                caller_ip = (
                    request.headers.get("x-real-ip")
                    or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                    or (request.client.host if request.client else None)
                )
                if not _ip_in_cidrs(caller_ip, allowed):
                    logger.warning(
                        "CIDR reject: caller_ip=%s partner_id=%s allowed=%s",
                        caller_ip, partner_id, allowed,
                    )
                    return _err(
                        401, "ip_not_allowed",
                        "Caller IP is not in this partner's allowed CIDRs.",
                    )

            # ── A9 — per-partner rate limit enforcement ───────────────
            # `partner.rate_limit_rps` has existed since Slice 7 as the
            # "override hook for Slice 9" (see the field's own comment in
            # models/phase_c.py) but was never read anywhere. nginx applies
            # one flat GLOBAL zone (rate=100r/s for every partner combined),
            # so a single partner could burst the platform's entire budget.
            # This closes it with a Redis sliding-window counter keyed per
            # partner. Fails OPEN (allows the request) if Redis is
            # unavailable — matching the rest of the A2A stack's degrade
            # policy (e.g. HMAC nonce store) — with a warning log, since a
            # hard-fail-closed here would turn a Redis blip into a total
            # partner-traffic outage, which is a worse operational risk than
            # a temporary loss of rate limiting.
            if getattr(settings, "a2a_rate_limit_enabled", True):
                limited, retry_after = _check_rate_limit(partner_id, partner.rate_limit_rps)
                if limited:
                    logger.warning(
                        "SECURITY_EVENT event=rate_limit_violation severity=medium "
                        "partner_id=%s limit_rps=%s decision=throttled",
                        partner_id, partner.rate_limit_rps,
                    )
                    return _err(
                        429, "rate_limit_exceeded",
                        f"Partner rate limit ({partner.rate_limit_rps} req/s) exceeded.",
                        retry_after=retry_after,
                    )

            # ── audit metadata for Slice 8 ──────────────────────────
            # Stored as native types where the executor wants to write
            # them straight onto the A2AMessage row (datetimes, str).
            # `request_started_ns` is the perf_counter_ns() snapshot the
            # executor diff's against to compute latency_ms.
            audit = {
                "caller_ip":          request.headers.get("x-real-ip")
                                       or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                                       or (request.client.host if request.client else None),
                "jwt_sub":            partner_id,
                # No `iat` in the JWT today (Slice 9 adds it). Use the
                # session row's created_at as the issuance timestamp
                # — that's exactly when /a2a/auth minted the token.
                "jwt_iat":            session.created_at,
                "jwt_exp":            session.expires_at,
                "request_protocol":   request.headers.get("x-forwarded-proto") or request.url.scheme,
                "request_started_ns": time.perf_counter_ns(),
                # Slice 6 will populate this from nginx's
                # X-Client-Cert-Fingerprint header for mTLS partners.
                "client_cert_fingerprint": request.headers.get("x-client-cert-fingerprint"),
            }

            # Detach partner from the session so the executor can use it
            # without triggering lazy-loads on a closed session.
            db.expunge(partner)
        finally:
            db.close()

        # Set the contextvar; reset on return so unrelated work (e.g.
        # SSE drain after the response) doesn't see stale auth.
        token_ctx = AUTH_CONTEXT.set(AuthContext(partner=partner, audit_meta=audit))
        try:
            return await call_next(request)
        finally:
            AUTH_CONTEXT.reset(token_ctx)


# ── helpers ──────────────────────────────────────────────────────────────────


def _err(status_code: int, error_code: str, detail: str, retry_after: int | None = None) -> JSONResponse:
    """Structured 401/403/429 response. The error_code field is what
    operators / partner SDK clients should match on; detail is for
    humans. `retry_after` (A9) sets the standard `Retry-After` header on
    429s so a well-behaved partner client backs off instead of hot-looping
    retries into the same rate limit."""
    logger.warning("a2a_auth_reject code=%s detail=%s", error_code, detail)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        status_code=status_code,
        content={"error": error_code, "detail": detail},
        headers=headers,
    )


# ── A9 — Redis sliding-window rate limiter ───────────────────────────────────
#
# One counter key per partner per window, using INCR + EXPIRE (the standard
# "fixed window counter" pattern — not a true sliding log, but O(1) per
# request and accurate enough for a per-partner RPS cap; a burst straddling
# two windows can momentarily allow up to ~2x the configured rate, which is
# an acceptable trade-off against the O(N) cost of a true sliding-window log
# at this request volume). Shares the same Redis connection helper as the
# HMAC nonce store (`app.services.job_registry._get_redis`) so there is one
# production-tested Redis wiring path for the whole A2A boundary.

def _get_redis_client():
    """Lazy redis client, shared with sdk_hmac_middleware.py's nonce store."""
    try:
        from app.services.job_registry import _get_redis
        return _get_redis()
    except Exception:  # noqa: BLE001
        return None


def _check_rate_limit(partner_id: str, limit_rps: int) -> tuple[bool, int | None]:
    """Returns (limited, retry_after_seconds). `limited=True` means the
    partner has exceeded `limit_rps` requests in the current window and the
    caller should reject with 429. Fails OPEN (returns `(False, None)`) if
    Redis is unavailable or `limit_rps` is not a positive integer — an
    unconfigured/unreachable limiter must not itself become an outage."""
    if not limit_rps or limit_rps <= 0:
        return False, None
    redis_client = _get_redis_client()
    if redis_client is None:
        return False, None
    window_s = max(1, int(getattr(settings, "a2a_rate_limit_window_s", 1) or 1))
    # Bucket key changes every `window_s` seconds — a fixed window, not a
    # rolling one. `window_s=1` (the default) makes this behave like a
    # simple per-second token count, matching the "rate_limit_rps" name.
    bucket = int(time.time() // window_s)
    key = f"a2a:ratelimit:{partner_id}:{bucket}"
    try:
        count = redis_client.incr(key)
        if count == 1:
            # Only the first request in a fresh bucket sets the TTL —
            # avoids resetting the expiry on every increment, which could
            # let a sufficiently steady stream of requests keep the key
            # alive indefinitely.
            redis_client.expire(key, window_s + 1)
        if count > limit_rps:
            return True, window_s
        return False, None
    except Exception as e:  # noqa: BLE001 — a Redis hiccup must fail OPEN, not become an outage
        logger.warning("A9 rate limiter: redis error (%s) — failing open for partner_id=%s", e, partner_id)
        return False, None


def _ip_in_cidrs(ip: str | None, cidrs: list[str]) -> bool:
    """True iff `ip` falls inside any CIDR in `cidrs`. Used by Slice 7.

    Stdlib only — no `netaddr` dep. Bad CIDRs (operator typo) are
    skipped silently, with a warn log; the per-partner allowlist
    edit endpoint validates inputs at write time so this branch is
    defensive only.
    """
    if not ip:
        return False
    import ipaddress
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if ip_obj in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            logger.warning("Skipping unparseable CIDR in allowlist: %r", cidr)
            continue
    return False
