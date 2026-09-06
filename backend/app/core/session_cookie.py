# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Session-cookie transport for the operator JWT.

WHY THIS EXISTS
---------------
The operator session JWT used to travel only as an ``Authorization: Bearer``
header, which meant the SPA had to hold it in JavaScript-reachable storage
(``localStorage``). Any script running in the app's origin could read it, so a
single XSS yielded a live, privileged credential — and operator tokens are 8h
and slide forward on every authenticated request, so the stolen credential
renews itself for as long as it is used.

Moving the token into an ``httpOnly`` cookie removes it from JavaScript's reach
entirely. This module is the single source of truth for that cookie's name and
flags so the login, MFA, refresh, logout and WebSocket paths cannot drift apart.

It mirrors the same migration already completed on the partner platform
(``app/api/auth.py`` in its own repository), deliberately: same cookie
mechanics, same dual-scheme fallback, same env-conditional ``Secure`` flag.

THE TRADE, STATED PLAINLY
-------------------------
A Bearer header is immune to CSRF — an attacker's page cannot set it. A cookie
is attached by the browser automatically, including on cross-site requests, so
cookie auth REINTRODUCES CSRF unless it is defended. Two controls do that here:

  1. ``SameSite=Strict`` on the cookie itself (below). This console is a
     same-origin admin app with no inbound cross-site navigation flow that must
     carry a session, so Strict is viable — it is a stronger choice than the
     partner platform's ``Lax`` and blocks cross-site sends outright.
  2. An Origin/Referer check on state-changing methods, in
     ``app.core.csrf``. Defence in depth for clients or browsers where the
     SameSite attribute is not honoured.

Removing either one re-opens CSRF on a platform whose endpoints record
certification verdicts and sign-offs. They belong together.

COOKIE NAME AND PATH
--------------------
``atom_session`` is deliberately distinct from the partner platform's
``pp_session``. Both apps are served from ONE origin behind the same nginx
under different path prefixes (``/a2a/`` and ``/a2a-partner/``), and
``pp_session`` is set with ``path="/"`` — so it is already sent to this backend
on every request. A shared name would make the two apps fight over one cookie
slot and silently log operators out of whichever logged in first.

The path stays ``/`` rather than the context prefix on purpose: the SPA's base
path is injected by nginx at serve time (``frontend/src/utils/basePath.js``),
so it is not knowable here at cookie-set time, and a mismatch would produce a
cookie the browser never sends back. A distinct NAME already gives the
isolation that matters.
"""
from __future__ import annotations

from fastapi import Request, Response

from app.core.config import settings

# Operator session cookie. Distinct from the partner platform's `pp_session`
# (see module docstring — both apps share one origin).
COOKIE_NAME = "atom_session"


def _is_production() -> bool:
    return (getattr(settings, "app_env", "") or "").strip().lower() == "production"


def set_session_cookie(response: Response, token: str) -> None:
    """Attach the session JWT as an httpOnly cookie.

    ``max_age`` tracks the JWT's own lifetime so the cookie and the credential
    inside it expire together — a cookie outliving its token would leave the
    browser sending a value that can only ever produce a 401.
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,          # the whole point: unreadable from JavaScript
        # Secure ONLY in production. The dev stack serves plain HTTP, and a
        # Secure cookie is never sent back over HTTP — setting it
        # unconditionally would break every local login with no visible cause.
        secure=_is_production(),
        samesite="strict",      # primary CSRF control; see module docstring
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove the session cookie.

    The attributes must match those used when setting it or the browser treats
    this as a different cookie and leaves the original in place.
    """
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        samesite="strict",
        secure=_is_production(),
        httponly=True,
    )


def extract_token(request: Request) -> str | None:
    """Return the session JWT from the cookie, else the Bearer header.

    DUAL-SCHEME ON PURPOSE, in this order:

    * The cookie wins so browsers use the credential JavaScript cannot read.
    * The Bearer fallback is NOT legacy cruft to delete later. Non-browser
      callers have no cookie jar, and several first-class flows depend on it:
      the A2A partner tokens verified by ``get_current_partner``, the MFA
      enrolment bridge token (which is presented before any session cookie
      exists), and operator scripts / curl.

    It also makes the frontend migration incremental: the backend serves both
    schemes at once, so the SPA can move endpoint by endpoint with no flag day.
    """
    tok = request.cookies.get(COOKIE_NAME)
    if tok:
        return tok
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def extract_token_ws(websocket) -> str | None:
    """Session JWT for a WebSocket, taken from the handshake cookies.

    The WebSocket handshake is an ordinary same-origin HTTP upgrade, so the
    browser attaches cookies to it automatically with no client-side code.
    That is why the SPA's per-socket ``{"token": ...}`` auth frame can simply
    be dropped rather than replaced with some other mechanism.

    Returns None when absent, leaving the caller to fall back to the auth frame
    (still supported for non-browser subscribers).
    """
    try:
        return websocket.cookies.get(COOKIE_NAME) or None
    except Exception:  # noqa: BLE001 — a scope without cookies must not 500
        return None
