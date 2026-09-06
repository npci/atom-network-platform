# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin API rate limiting (net-new gap from the S1 hostility-tier exercise —
not in either review PDF, but recorded in ARCHITECTURE_REVIEW_REMEDIATION.md
§14 as a tracked follow-up: "/admin/* has no rate limiting despite being a
real attack surface — a compromised admin credential, or a CSRF-style abuse
from an authenticated browser session, could still hammer the API.")

Reuses the SAME Redis fixed-window counter pattern already proven for the
A2A partner boundary (``a2a_common/sdk_auth_middleware.py::_check_rate_limit``)
— one counter key per identity per window, INCR + EXPIRE, fail-OPEN if Redis
is unavailable (an admin-tooling outage from a rate-limiter Redis blip would
be a worse operational risk than a temporary loss of throttling, matching
the A2A boundary's own stated trade-off).

Applied as ASGI middleware (not a per-route dependency) because ``/admin/*``
routes are spread across a dozen router files under different ``AdminUser``/
``CurrentUser`` dependencies — a middleware closes the gap for all of them
at once without touching every route file. Keyed by the resolved user id
(decoded straight from the session JWT, no DB round-trip) rather than by
IP, since an admin's IP is often a shared corporate NAT/VPN egress and a
compromised CREDENTIAL, not a compromised IP, is the threat this closes.
An unauthenticated request (no valid token) is NOT rate-limited here — it
falls through to the normal 401 from the route's own auth dependency,
which is a cheap, fast rejection this limiter would add no value to.
"""
from __future__ import annotations

import logging
import time

from app.core.config import settings

logger = logging.getLogger("app")

_ADMIN_PATH_PREFIX = "/api/admin"


def _get_redis_client():
    """Lazy redis client — same production-tested connection helper the A2A
    boundary's rate limiter and HMAC nonce store already use."""
    try:
        from app.services.job_registry import _get_redis
        return _get_redis()
    except Exception:  # noqa: BLE001
        return None


def _check_admin_rate_limit(user_id: str, limit_rps: int, window_s: int) -> tuple[bool, int | None]:
    """Returns ``(limited, retry_after_seconds)``. Fixed-window counter,
    identical shape to the A2A partner limiter. Fails OPEN (returns
    ``(False, None)``) on a disabled/misconfigured limit or a Redis error."""
    if not limit_rps or limit_rps <= 0:
        return False, None
    redis_client = _get_redis_client()
    if redis_client is None:
        return False, None
    window_s = max(1, int(window_s or 1))
    bucket = int(time.time() // window_s)
    key = f"admin:ratelimit:{user_id}:{bucket}"
    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, window_s + 1)
        if count > limit_rps:
            return True, window_s
        return False, None
    except Exception as e:  # noqa: BLE001 — a Redis hiccup must fail OPEN, not become an outage
        logger.warning("admin rate limiter: redis error (%s) — failing open for user_id=%s", e, user_id)
        return False, None


class AdminRateLimitMiddleware:
    """ASGI middleware: throttle authenticated requests to ``/api/admin/*`` by
    the caller's resolved user id. No-ops entirely (disabled by default via
    ``admin_rate_limit_enabled``) until an operator turns it on — this is
    additive, not a behavior change to existing admin tooling by default."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not getattr(settings, "admin_rate_limit_enabled", False):
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not path.startswith(_ADMIN_PATH_PREFIX):
            await self.app(scope, receive, send)
            return
        user_id = self._resolve_user_id(scope)
        if not user_id:
            # No valid session — let the route's own auth dependency reject with
            # 401 at normal speed; rate-limiting an already-rejected request adds
            # nothing but Redis load.
            await self.app(scope, receive, send)
            return
        limited, retry_after = _check_admin_rate_limit(
            user_id,
            int(getattr(settings, "admin_rate_limit_rps", 20) or 0),
            int(getattr(settings, "admin_rate_limit_window_s", 1) or 1),
        )
        if limited:
            logger.warning(
                "SECURITY_EVENT event=admin_rate_limit_violation severity=medium "
                "user_id=%s path=%s decision=throttled", user_id, path,
            )
            await self._send_429(send, retry_after)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _resolve_user_id(scope) -> str | None:
        """Decode the session JWT straight from the ASGI scope's cookies/headers —
        no DB session, so this middleware costs one JWT verify, not a query.
        Mirrors ``session_cookie.extract_token``'s cookie-then-bearer order
        without needing a full ``Request`` object."""
        try:
            from app.core.session_cookie import COOKIE_NAME
            from app.core.security import decode_access_token
            headers = dict(scope.get("headers") or [])
            cookie_header = (headers.get(b"cookie") or b"").decode("latin-1")
            token = None
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith(f"{COOKIE_NAME}="):
                    token = part.split("=", 1)[1].strip()
                    break
            if not token:
                auth = (headers.get(b"authorization") or b"").decode("latin-1")
                if auth.lower().startswith("bearer "):
                    token = auth[7:].strip()
            if not token:
                return None
            return decode_access_token(token)
        except Exception:  # noqa: BLE001 — identity resolution must never break the request path
            return None

    @staticmethod
    async def _send_429(send, retry_after: int | None) -> None:
        headers = [(b"content-type", b"application/json")]
        if retry_after:
            headers.append((b"retry-after", str(retry_after).encode()))
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body",
                    "body": b'{"detail":"Admin API rate limit exceeded"}'})
