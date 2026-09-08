# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

import logging
from typing import Annotated, Generator
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.security import decode_access_token, decode_access_token_amr
from app.core.session_cookie import extract_token
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

# auto_error=False so a missing Authorization header does NOT 401 on its own —
# the session may legitimately be carried by the httpOnly cookie instead. The
# "no credential at all" case is raised explicitly in get_current_user below,
# which keeps one consistent error shape for both schemes.
bearer_scheme = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbDep = Annotated[Session, Depends(get_db)]


def get_current_user(
    request: Request,
    db: DbDep,
    # Declared but unused: it keeps the Bearer scheme in the OpenAPI document
    # so /api/docs still renders the "Authorize" button and marks these routes
    # as secured. auto_error=False means it never rejects on its own — the
    # actual credential is resolved from the cookie OR the header below.
    _bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """Resolve the operator session from the httpOnly cookie or a Bearer header.

    The cookie is preferred so browsers never need the token in
    JavaScript-reachable storage; the Bearer fallback keeps non-browser callers
    and the MFA enrolment bridge working. See app.core.session_cookie.
    """
    token = extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    user_id = decode_access_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    # Check Redis denylist (lazy import to avoid circular dep at module load)
    from app.api.auth import is_token_revoked
    if is_token_revoked(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
        )
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def _audit_denial(user: User, request: Request | None, required: str) -> None:
    """Record an authorization denial (item 28 — security event logging).

    A 403 from these guards used to be completely invisible: no audit row, no
    security event, and the admin rate limiter only fires on THROTTLE, so a
    caller walking the route table looking for one that would admit them left no
    trace. Denials are the signal that distinguishes a misconfigured client from
    someone probing for privilege escalation, so they need a durable record with
    actor, target and outcome.

    Best-effort and fully swallowed: auditing must never convert a clean 403
    into a 500. Uses its own short-lived session because the guard may run
    before — or instead of — a request-scoped one.
    """
    try:
        from app.core import auth_audit
        db = SessionLocal()
        try:
            auth_audit.record(
                db, auth_audit.AUTHZ_DENIED,
                username=getattr(user, "username", None),
                user_id=getattr(user, "id", None),
                ip=(request.client.host if request and request.client else None),
                detail=(f"required={required} active_role="
                        f"{getattr(getattr(user, 'role', None), 'value', '?')} "
                        f"path={getattr(getattr(request, 'url', None), 'path', '?')}"),
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — auditing must never break authorization
        logger.warning("authz denial audit failed", exc_info=True)


def require_role(*roles: UserRole):
    def checker(current_user: CurrentUser, request: Request) -> User:
        if current_user.role not in roles:
            _audit_denial(current_user, request,
                          ",".join(sorted(r.value for r in roles)))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {[r.value for r in roles]}",
            )
        return current_user
    return checker


def require_admin(request: Request, current_user: CurrentUser) -> User:
    if current_user.role != UserRole.ADMIN:
        _audit_denial(current_user, request, "admin")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    # Closes T7 — "Human administrative access MUST require MFA". Enforced
    # for the ADMIN role SPECIFICALLY, independent of `settings.
    # mfa_enforced` (the platform-wide, all-users switch, which may
    # legitimately stay off for non-admin roles). A session token's `amr`
    # claim must be "pwd+mfa" — i.e. the caller actually completed the
    # /auth/mfa/verify step for THIS session — not merely "the account has
    # MFA enabled" (a stale token issued before MFA was set up, or a token
    # from a password-only login path, does not satisfy this).
    #
    # `admin_mfa_required` defaults to True. An operator can set it False
    # ONLY as an explicit, logged, break-glass-style override — never a
    # silent default — for environments where MFA enrollment for admins is
    # still being rolled out; this mirrors the pattern
    # `a2a_require_hmac_for_active_partners` already establishes in
    # startup_validation.py (secure-by-default, explicit opt-out only).
    if getattr(settings, "admin_mfa_required", True):
        token = extract_token(request)
        amr = decode_access_token_amr(token) if token else None
        if amr != "pwd+mfa":
            # Audited too, and arguably the more interesting denial of the two:
            # the caller HAS the admin role but is presenting a password-only
            # session, which is what a stolen or downgraded session looks like.
            _audit_denial(current_user, request, "admin+mfa")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Admin access requires a session established via MFA "
                    "(password + OTP/backup code). Complete MFA enrollment "
                    "and re-authenticate via /auth/mfa/verify."
                ),
            )
    # T8 — stamp the resolved admin identity onto the
    # request so `AdminActionAuditMiddleware` can record the action
    # generically, for EVERY admin-gated route, without each endpoint
    # having to remember an explicit `admin_action_audit.record()` call.
    #
    # Why request.state and not a path prefix: admin-gated routes are NOT
    # all under /api/admin/* — `users.py` (/api/users), `rag.py` (/api/rag),
    # `change_requests.py` and `resolver.py` (/api/changes), `logs.py`
    # (/api/logs), `eval.py`, `agents.py` and others also depend on
    # `AdminUser`. A prefix-keyed middleware would silently miss them,
    # which would recreate T8's coverage gap in a new form. Keying on the
    # dependency that actually granted admin access is exact by
    # construction: if `require_admin` ran and passed, the request is an
    # authenticated admin action, whatever its URL.
    try:
        request.state.admin_audit_user_id = current_user.id
        request.state.admin_audit_username = getattr(current_user, "username", None)
    except Exception:  # noqa: BLE001 — never let audit bookkeeping break authorization
        pass
    return current_user


AdminUser = Annotated[User, Depends(require_admin)]


def require_admin_or_tech_lead(current_user: CurrentUser) -> User:
    """Agentic codegen is a privileged capability — admin + tech-lead only."""
    if current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agentic codegen is restricted to admin and tech-lead users.",
        )
    return current_user


# Admin + tech-lead gate for the agentic codegen subsystem.
AgenticUser = Annotated[User, Depends(require_admin_or_tech_lead)]


def get_current_user_ws(token: str, db: Session) -> User | None:
    """Authenticate a WebSocket connection using a raw JWT token string."""
    if not token:
        return None
    user_id = decode_access_token(token)
    if not user_id:
        return None
    from app.api.auth import is_token_revoked
    if is_token_revoked(token):
        return None
    user = db.get(User, user_id)
    if not user or not user.is_active:
        return None
    return user


def authenticate_ws(websocket, db: Session, frame_token: str = "") -> User | None:
    """Resolve the operator for a WebSocket, preferring the handshake cookie.

    The WebSocket handshake is a same-origin HTTP upgrade, so the browser
    attaches the session cookie automatically — no client-side code, nothing a
    new call site can forget to send. That is why the SPA no longer pushes a
    ``{"token": ...}`` auth frame.

    ``frame_token`` remains supported as a fallback for non-browser
    subscribers (and any client not yet migrated), so this is additive: every
    caller that used to work still works.
    """
    from app.core.session_cookie import extract_token_ws

    return (get_current_user_ws(extract_token_ws(websocket) or "", db)
            or get_current_user_ws(frame_token or "", db))


# ── A2A Partner Authentication ────────────────────────────────────────────────

def get_current_partner(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DbDep,
):
    """Decode A2A partner JWT and return the active PartnerAgent.

    Partners are machine callers with no cookie jar, so this path stays
    Bearer-only — it deliberately does NOT accept the operator session cookie.
    """
    from app.core.security import decode_partner_token
    from app.models.phase_c import PartnerAgent, PartnerStatus

    # bearer_scheme is auto_error=False (the operator path may authenticate by
    # cookie instead), so a missing header arrives here as None. Raise the 401
    # explicitly rather than letting attribute access blow up as a 500.
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    partner_id, token_type = decode_partner_token(credentials.credentials)
    if not partner_id or token_type != "a2a":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired A2A token",
        )
    partner = db.get(PartnerAgent, partner_id)
    if not partner or partner.status != PartnerStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Partner not found or inactive",
        )
    return partner


CurrentPartner = Annotated["PartnerAgent", Depends(get_current_partner)]
