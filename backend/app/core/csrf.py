# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CSRF defence for cookie-authenticated requests.

WHY THIS EXISTS
---------------
Moving the operator session from an ``Authorization`` header into a cookie
(``app.core.session_cookie``) closes an XSS credential-theft hole and opens a
CSRF one. The browser attaches cookies automatically, so without a control an
attacker's page could cause an authenticated state-changing request to this
API — on a platform where those requests record certification verdicts, grant
waivers and issue sign-offs.

``SameSite=Strict`` on the cookie is the primary control. This middleware is
the second layer, for the cases SameSite alone does not cover: older browsers
that ignore the attribute, and any future need to relax it.

WHAT IT CHECKS
--------------
For state-changing methods only (POST/PUT/PATCH/DELETE), when the request is
authenticated BY COOKIE, the ``Origin`` (or ``Referer``) must match an allowed
origin. Requests authenticated by ``Authorization: Bearer`` are exempt, because
a cross-site attacker cannot set that header — which is exactly the property
that made the old bearer-only design CSRF-proof, and it still holds for the
non-browser callers that continue to use it.

WHY NOT A DOUBLE-SUBMIT TOKEN
-----------------------------
A double-submit CSRF token needs a JS-readable cookie plus a header echoed by
every caller — including 8 WebSocket opens, an SSE stream and several manual
`fetch` calls in this SPA. An Origin check gets the same protection for
cookie-auth requests with no client-side contract at all, so there is nothing
for a future call site to forget to implement. If a deployment ever needs
cross-origin browser access, add the double-submit token THEN; today the app is
same-origin behind one nginx (prod) or the Vite proxy (dev).

FAIL-OPEN vs FAIL-CLOSED
------------------------
A browser always sends ``Origin`` on cross-origin state-changing requests, and
same-origin form/XHR posts send it too. A request with NO Origin and NO Referer
is therefore not a browser CSRF vector (it is curl, a test client, or a
server-to-server call) and is allowed through — being strict there would break
the API for every non-browser caller while blocking nothing an attacker can
actually do from a victim's browser.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.session_cookie import COOKIE_NAME

logger = logging.getLogger(__name__)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _origin_of(url: str) -> str | None:
    """Reduce a URL to scheme://host[:port], or None when unparseable."""
    try:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return None
        return f"{p.scheme}://{p.netloc}"
    except Exception:  # noqa: BLE001
        return None


def _allowed_origins(request: Request) -> set[str]:
    """Origins accepted for cookie-authenticated state-changing requests.

    The request's OWN origin (derived from the Host header) is included so the
    normal same-origin case works without configuration — in production both
    SPAs are served from the same nginx as the API, so `Origin` equals the API
    origin and there is nothing to configure. ``frontend_url`` covers the dev
    setup where the SPA is served by Vite on another port.
    """
    allowed: set[str] = set()

    fe = (getattr(settings, "frontend_url", "") or "").strip()
    if fe:
        o = _origin_of(fe)
        if o:
            allowed.add(o)

    # The origin the client actually addressed. Behind a proxy, honour the
    # forwarded headers nginx sets, else fall back to Host.
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        proto = (request.headers.get("x-forwarded-proto")
                 or request.url.scheme
                 or "http")
        allowed.add(f"{proto}://{host}")
        # A deployment may terminate TLS at the edge and speak http internally;
        # accept both schemes for the request's own host rather than rejecting
        # a legitimate same-origin call over a scheme mismatch.
        allowed.add(f"{'http' if proto == 'https' else 'https'}://{host}")

    return allowed


async def csrf_origin_middleware(request: Request, call_next):
    """Reject cookie-authenticated cross-origin state-changing requests."""
    if request.method not in UNSAFE_METHODS:
        return await call_next(request)

    # Only cookie-authenticated requests are CSRF-exposed. A Bearer header
    # cannot be set by a cross-site attacker, so those callers are exempt.
    if COOKIE_NAME not in request.cookies:
        return await call_next(request)

    origin = request.headers.get("origin")
    if not origin:
        ref = request.headers.get("referer")
        origin = _origin_of(ref) if ref else None

    # No Origin and no Referer → not a browser-driven cross-site request.
    # See "FAIL-OPEN vs FAIL-CLOSED" in the module docstring.
    if not origin:
        return await call_next(request)

    if origin in _allowed_origins(request):
        return await call_next(request)

    logger.warning(
        "CSRF: rejected %s %s from origin %r (cookie-authenticated, origin not allowed)",
        request.method, request.url.path, origin,
    )
    return JSONResponse(
        status_code=403,
        content={"detail": "Cross-origin request rejected"},
    )
