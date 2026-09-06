# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin-only manual transitions on ChangePartnerAssignment.status.

Endpoints (all under /api):
  POST /changes/{change_id}/partners/{partner_id}/approve-for-production
  POST /changes/{change_id}/partners/{partner_id}/mark-live
  POST /changes/{change_id}/partners/{partner_id}/block            (body: {reason})
  POST /changes/{change_id}/partners/{partner_id}/unblock
  POST /changes/{change_id}/partners/{partner_id}/withdraw         (body: {reason})
  GET  /changes/{change_id}/partners/{partner_id}/status-history

Each transition writes a row in `assignment_status_history` capturing
actor + reason. `block` and `unblock` toggle `blocked_at` + `blocked_reason`
on the assignment row but do NOT mutate `status` (block is a concurrent
flag, not a state).
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.deps import DbDep, AdminUser
from app.models.base import generate_uuid, utcnow
from app.models.phase_c import (
    AssignmentStatus,
    AssignmentStatusHistory,
    ChangePartnerAssignment,
    PartnerAgent,
)
from app.services.assignment_status import set_status

logger = logging.getLogger(__name__)
router = APIRouter(tags=["assignment-actions"])


# ── Schemas ─────────────────────────────────────────────────────────────

class ReasonBody(BaseModel):
    reason: str | None = None


# ── Helpers ─────────────────────────────────────────────────────────────

def _load(db, change_id: str, partner_id: str) -> ChangePartnerAssignment:
    a = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()
    if not a:
        raise HTTPException(status_code=404, detail="Partner not assigned to this change")
    return a


def _current(a: ChangePartnerAssignment) -> str:
    s = a.status
    return s.value if hasattr(s, "value") else s


def _serialize(a: ChangePartnerAssignment) -> dict:
    return {
        "assignment_id":  a.id,
        "change_request_id": a.change_request_id,
        "partner_id":     a.partner_id,
        "status":         _current(a),
        "blocked_at":     a.blocked_at.isoformat() if a.blocked_at else None,
        "blocked_reason": a.blocked_reason,
        "assigned_at":    a.assigned_at.isoformat() if a.assigned_at else None,
    }


# ── Endpoints ───────────────────────────────────────────────────────────

@router.post("/changes/{change_id}/partners/{partner_id}/approve-for-production")
def approve_for_production(
    change_id: str, partner_id: str, body: ReasonBody, db: DbDep, user: AdminUser,
):
    """Move a CERTIFIED partner to READY_FOR_PRODUCTION.

    Manual the Authority sign-off — never automatic on cert-pass. Admin-only.
    """
    a = _load(db, change_id, partner_id)
    cur = _current(a)
    if cur != "certified":
        raise HTTPException(
            status_code=409,
            detail=f"Partner status is '{cur}'; approve-for-production requires 'certified'.",
        )
    set_status(
        a, AssignmentStatus.READY_FOR_PRODUCTION, db,
        actor_user_id=user.id,
        reason=body.reason or "Approved for production",
    )
    db.commit()
    logger.info("approve_for_production: change=%s partner=%s by=%s", change_id, partner_id, user.username)
    return _serialize(a)


@router.post("/changes/{change_id}/partners/{partner_id}/mark-live")
def mark_live(
    change_id: str, partner_id: str, body: ReasonBody, db: DbDep, user: AdminUser,
):
    """Move a READY_FOR_PRODUCTION partner to IN_PRODUCTION.

    the Authority ops clicks this once the partner is actually processing real
    traffic. Terminal forward state. Admin-only.
    """
    a = _load(db, change_id, partner_id)
    cur = _current(a)
    if cur != "ready_for_production":
        raise HTTPException(
            status_code=409,
            detail=f"Partner status is '{cur}'; mark-live requires 'ready_for_production'.",
        )
    set_status(
        a, AssignmentStatus.IN_PRODUCTION, db,
        actor_user_id=user.id,
        reason=body.reason or "Marked live",
    )
    db.commit()
    logger.info("mark_live: change=%s partner=%s by=%s", change_id, partner_id, user.username)
    return _serialize(a)


@router.post("/changes/{change_id}/partners/{partner_id}/block")
def block_partner(
    change_id: str, partner_id: str, body: ReasonBody, db: DbDep, user: AdminUser,
):
    """Set the BLOCKED concurrent flag on the assignment.

    Does NOT change the underlying `status`. Forward actions on the partner
    should be gated on `blocked_at IS NULL` in the UI.
    """
    a = _load(db, change_id, partner_id)
    if a.blocked_at is not None:
        raise HTTPException(status_code=409, detail="Partner is already blocked")
    if not body.reason:
        raise HTTPException(status_code=400, detail="reason is required to block a partner")
    a.blocked_at = utcnow()
    a.blocked_reason = body.reason

    # Audit row — record the flag change with the same history table even
    # though `status` itself didn't move. Use to_status='blocked:flag-set'
    # so the viewer can distinguish.
    db.add(AssignmentStatusHistory(
        id=generate_uuid(),
        assignment_id=a.id,
        from_status=_current(a),
        to_status=f"{_current(a)} (blocked)",
        actor_user_id=user.id,
        reason=body.reason,
    ))
    db.commit()
    logger.info("block: change=%s partner=%s by=%s reason=%s", change_id, partner_id, user.username, body.reason[:80])
    return _serialize(a)


@router.post("/changes/{change_id}/partners/{partner_id}/unblock")
def unblock_partner(change_id: str, partner_id: str, db: DbDep, user: AdminUser):
    """Clear the BLOCKED concurrent flag."""
    a = _load(db, change_id, partner_id)
    if a.blocked_at is None:
        raise HTTPException(status_code=409, detail="Partner is not blocked")
    a.blocked_at = None
    a.blocked_reason = None

    db.add(AssignmentStatusHistory(
        id=generate_uuid(),
        assignment_id=a.id,
        from_status=f"{_current(a)} (blocked)",
        to_status=_current(a),
        actor_user_id=user.id,
        reason="Unblocked",
    ))
    db.commit()
    logger.info("unblock: change=%s partner=%s by=%s", change_id, partner_id, user.username)
    return _serialize(a)


@router.post("/changes/{change_id}/partners/{partner_id}/withdraw")
def withdraw_partner(
    change_id: str, partner_id: str, body: ReasonBody, db: DbDep, user: AdminUser,
):
    """Mark the partner as WITHDRAWN — terminal off-flow state.

    Settable from any state except IN_PRODUCTION (already live; can't
    withdraw without a separate retire flow) and WITHDRAWN itself.
    """
    a = _load(db, change_id, partner_id)
    cur = _current(a)
    if cur in ("in_production", "withdrawn"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot withdraw from '{cur}'.",
        )
    if not body.reason:
        raise HTTPException(status_code=400, detail="reason is required to withdraw a partner")
    set_status(
        a, AssignmentStatus.WITHDRAWN, db,
        actor_user_id=user.id,
        reason=body.reason,
    )
    db.commit()
    logger.info("withdraw: change=%s partner=%s by=%s reason=%s", change_id, partner_id, user.username, body.reason[:80])
    return _serialize(a)


# ── Status history viewer ──────────────────────────────────────────────

@router.get("/changes/{change_id}/partners/{partner_id}/status-history")
def status_history(change_id: str, partner_id: str, db: DbDep, _: AdminUser):
    """Return the audit trail of status transitions for one assignment."""
    a = _load(db, change_id, partner_id)
    rows = db.scalars(
        select(AssignmentStatusHistory)
        .where(AssignmentStatusHistory.assignment_id == a.id)
        .order_by(AssignmentStatusHistory.created_at.desc())
    ).all()

    # Resolve actor names. Lazy: fetch usernames + partner names per row;
    # the volume per assignment is small (handful of transitions).
    from app.models.user import User
    user_cache: dict[str, str] = {}
    partner_cache: dict[str, str] = {}

    def actor_label(r: AssignmentStatusHistory) -> str:
        if r.actor_user_id:
            if r.actor_user_id not in user_cache:
                u = db.get(User, r.actor_user_id)
                user_cache[r.actor_user_id] = u.username if u else r.actor_user_id
            return user_cache[r.actor_user_id]
        if r.actor_partner_id:
            if r.actor_partner_id not in partner_cache:
                p = db.get(PartnerAgent, r.actor_partner_id)
                partner_cache[r.actor_partner_id] = p.name if p else r.actor_partner_id
            return partner_cache[r.actor_partner_id]
        return "system"

    def actor_kind(r: AssignmentStatusHistory) -> str:
        if r.actor_user_id:    return "user"
        if r.actor_partner_id: return "partner"
        return "system"

    return {
        "assignment_id": a.id,
        "current_status": _current(a),
        "blocked": a.blocked_at is not None,
        "blocked_at": a.blocked_at.isoformat() if a.blocked_at else None,
        "blocked_reason": a.blocked_reason,
        "transitions": [
            {
                "id": r.id,
                "from_status": r.from_status,
                "to_status": r.to_status,
                "actor": actor_label(r),
                "actor_kind": actor_kind(r),
                "reason": r.reason,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
