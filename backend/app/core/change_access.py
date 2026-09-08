# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Object-level authorization for everything hanging off ``/changes/{change_id}``.

WHY THIS EXISTS
---------------
``api/change_requests.py`` has always enforced ownership: ``_is_creator_or_admin``
gates its detail, patch, artifact, reconciliation and download routes. But the
same ``/api/changes/{change_id}`` path space is served by five OTHER routers —
``phase_b``, ``phase_c``, ``negotiation_mgmt``, ``eval``, ``product_kit_video``,
``agents`` — and those only ever checked that the row EXISTS
(``_get_change_or_404``), never that the caller may see it. ``phase_c.py`` had no
ownership helper at all across 33 routes.

The practical effect: a user refused ``GET /api/changes/{X}`` with 403 could read
the same change's code, partner correspondence and negotiation state through
``/phase-b``, ``/phase-c/messages`` and ``/negotiate/status``, and could
``POST /phase-b/git/push`` to push its branch and open a merge request.

WHY A ROUTER-LEVEL DEPENDENCY, NOT A PER-ROUTE CHECK
-----------------------------------------------------
A per-route check is exactly what was already tried and is exactly what drifted:
one router remembered it, five did not, and nothing failed when a new route
forgot. Attached with ``APIRouter(dependencies=[...])`` this runs for EVERY route
on the router — including ones added later — so the default is deny and opting a
route out has to be deliberate and visible.

It takes ``HTTPConnection`` (the shared base of ``Request`` and ``WebSocket``)
rather than ``Request`` so the same dependency can be attached to routers that
also carry WebSocket routes. ``get_current_user`` cannot be used here for the
same reason: it is typed ``Request`` and FastAPI cannot inject that into a
WebSocket route.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from starlette.requests import HTTPConnection

from app.core.database import SessionLocal
from app.core.security import decode_access_token
from app.core.session_cookie import extract_token
from app.models.change_request import ChangeRequest
from app.models.user import User, UserRole


def can_read_all_changes(user: User) -> bool:
    """Roles with read-only visibility into ALL changes: admin + the review
    teams. Mirrors ``api/change_requests._can_read_all_changes`` — the review
    teams see the change list and the Product Kit, but not BRD/TSD/Phase-B/C.
    """
    return user.role in (
        UserRole.ADMIN,
        UserRole.RISK_REVIEWER,
        UserRole.INFOSEC_REVIEWER,
        UserRole.TECH_LEAD,
    )


def is_creator_or_admin(user: User, change: ChangeRequest) -> bool:
    """Full access — the change creator or an admin. Review teams are
    deliberately excluded; they get product-kit-only access via
    ``can_read_all_changes``. Mirrors ``api/change_requests._is_creator_or_admin``.
    """
    return user.role == UserRole.ADMIN or change.created_by == user.id


def _resolve_user(conn: HTTPConnection, db) -> User | None:
    """Best-effort session resolution shared by HTTP and WebSocket scopes.

    Deliberately returns None rather than raising: the caller decides the
    failure mode, and for a WebSocket the handshake must not raise an
    HTTPException.
    """
    token = extract_token(conn)
    if not token:
        return None
    user_id = decode_access_token(token)
    if not user_id:
        return None
    from app.api.auth import is_token_revoked          # lazy: circular at import
    if is_token_revoked(token):
        return None
    user = db.get(User, user_id)
    return user if user and user.is_active else None


def _check(conn: HTTPConnection, db, *, kit_read_ok: bool) -> None:
    change_id = conn.path_params.get("change_id")
    if not change_id:
        return                       # route is not change-scoped; nothing to gate

    # WebSocket routes authenticate themselves immediately after accept() via
    # `authenticate_ws`, which can close the socket with a proper code. Raising
    # an HTTPException during the handshake instead produces a far less useful
    # failure, so object-level enforcement for sockets stays with that helper.
    if conn.scope.get("type") != "http":
        return

    user = _resolve_user(conn, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated")

    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Change request not found")

    if is_creator_or_admin(user, change):
        return
    if kit_read_ok and conn.scope.get("method") == "GET" and can_read_all_changes(user):
        return

    # Same shape as change_requests.py's own 403 so the two layers are
    # indistinguishable to a caller probing for which router guards what.
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                        detail="Not authorised for this change request")


def _db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_change_access(conn: HTTPConnection, db=Depends(_db)) -> None:
    """Creator-or-admin on every ``{change_id}`` route of the router it guards."""
    _check(conn, db, kit_read_ok=False)


# Negotiation/certification outcomes are PM workflow data. Mirrors
# `negotiation_mgmt._NEGOTIATION_ROLES`.
WORKFLOW_ROLES = {UserRole.PRODUCT_MANAGER, UserRole.PRODUCT_OWNER, UserRole.ADMIN}


def require_workflow_role(conn: HTTPConnection, db=Depends(_db)) -> None:
    """PM / PO / admin only — for privileged workflow decisions.

    Distinct from `require_change_access`, and both are needed: that one asks
    "is this your change?", this one asks "is your ROLE allowed to make this
    kind of decision?". A tech_lead who created a change passes the first and
    must still fail the second.

    Applied to the certification and negotiation sign-offs — waiver decisions,
    counter-proposal acceptance, kit shipping, certification start. Those were
    gated on authentication alone, so an infosec_reviewer (a role
    change_requests.py deliberately restricts to product-kit reads) could grant
    a partner a certification waiver that is then transmitted over A2A and
    recorded as their decision.
    """
    if conn.scope.get("type") != "http":
        return
    user = _resolve_user(conn, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated")
    if user.role not in WORKFLOW_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires " + " or ".join(
                sorted(r.value for r in WORKFLOW_ROLES)))


def require_change_or_kit_access(conn: HTTPConnection, db=Depends(_db)) -> None:
    """As above, but additionally allows the review teams to GET.

    For routers serving Product Kit material, where ``change_requests.py``
    already grants the Risk/InfoSec/Tech reviewers read access to the kit (see
    its ``_kit_doc and _can_read_all_changes`` branch). Reads only — a reviewer
    still may not mutate someone else's change.
    """
    _check(conn, db, kit_read_ok=True)
