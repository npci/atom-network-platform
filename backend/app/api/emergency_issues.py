# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Emergency issue API (the Authority side) — view + resolve post-freeze issues.

After a change is frozen (final kit shipped), a partner's only inbound channel
is an EmergencyIssue. The PM triages and resolves them here.

Endpoints:
  GET  /changes/{change_id}/emergency-issues   — all issues for a change
  POST /emergency-issues/{issue_id}/resolve     — PM marks an issue resolved
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.deps import CurrentUser, DbDep
from app.models.emergency_issue import EI_STATUS_RESOLVED, EmergencyIssue
from app.models.phase_c import PartnerAgent
from app.models.user import UserRole

logger = logging.getLogger(__name__)
router = APIRouter(tags=["emergency-issues"])

_PM_ROLES = {UserRole.PRODUCT_MANAGER, UserRole.PRODUCT_OWNER, UserRole.ADMIN}


def _serialize(e: EmergencyIssue, partner_name: str) -> dict:
    return {
        "id": e.id,
        "change_request_id": e.change_request_id,
        "partner_id": e.partner_id,
        "partner_name": partner_name,
        "issue_id": e.issue_id,
        "severity": e.severity,
        "status": e.status,
        "title": e.title,
        "description": e.description,
        "authority_resolution_text": e.authority_resolution_text,
        "resolved_by": e.resolved_by,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
    }


@router.get("/changes/{change_id}/emergency-issues")
def list_emergency_issues(change_id: str, db: DbDep, current_user: CurrentUser):
    # Emergency issues are PM-owned operational data (triaged + resolved here).
    # Gate reads to the same roles as resolve so the product-kit-scoped review
    # teams can't enumerate cross-change partner issues (review finding SEC-1).
    if current_user.role not in _PM_ROLES:
        raise HTTPException(status_code=403, detail="Only PM/PO/admin can view emergency issues")
    issues = (
        db.query(EmergencyIssue)
        .filter(EmergencyIssue.change_request_id == change_id)
        .order_by(EmergencyIssue.created_at.desc())
        .all()
    )
    partner_ids = {e.partner_id for e in issues}
    names = {
        p.id: p.name
        for p in db.query(PartnerAgent).filter(PartnerAgent.id.in_(partner_ids)).all()
    } if partner_ids else {}
    return [_serialize(e, names.get(e.partner_id, "Unknown")) for e in issues]


class ResolveBody(BaseModel):
    resolution_text: str


@router.post("/emergency-issues/{issue_id}/resolve")
def resolve_emergency_issue(
    issue_id: str,
    body: ResolveBody,
    db: DbDep,
    current_user: CurrentUser,
):
    if current_user.role not in _PM_ROLES:
        raise HTTPException(status_code=403, detail="Only PM/PO/admin can resolve emergency issues")
    issue = db.get(EmergencyIssue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="emergency issue not found")
    if not body.resolution_text.strip():
        raise HTTPException(status_code=400, detail="resolution_text required")

    issue.authority_resolution_text = body.resolution_text.strip()
    issue.resolved_by = current_user.id
    issue.status = EI_STATUS_RESOLVED
    issue.resolved_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Emergency issue %s resolved by %s", issue_id, current_user.id)
    return {"status": "ok", "id": issue.id, "issue_status": issue.status}
