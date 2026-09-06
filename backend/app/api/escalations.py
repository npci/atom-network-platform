# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Escalation inbox API — Risk / InfoSec / Tech review teams.

The feasibility resolver opens an EscalationTicket when a partner query needs
a review team's sign-off. Team members see their team's open tickets here,
respond, and the response loops back into the PM's partner-reply draft.

Endpoints:
  GET  /escalations?status=open                  — tickets visible to the caller's team
  POST /escalations/{ticket_id}/respond          — team submits its response
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from app.core.deps import CurrentUser, DbDep
from app.models.change_request import ChangeRequest
from app.models.escalation_ticket import (
    ESC_STATUS_RESPONDED,
    EscalationTicket,
)
from app.models.phase_c import PartnerAgent
from app.models.user import UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/escalations", tags=["escalations"])


# Which escalation teams a role may see/act on. PM/PO/admin oversee all three.
_ROLE_TEAMS: dict[UserRole, set[str]] = {
    UserRole.RISK_REVIEWER: {"risk"},
    UserRole.INFOSEC_REVIEWER: {"infosec"},
    UserRole.TECH_LEAD: {"tech"},
    UserRole.PRODUCT_MANAGER: {"risk", "infosec", "tech"},
    UserRole.PRODUCT_OWNER: {"risk", "infosec", "tech"},
    UserRole.ADMIN: {"risk", "infosec", "tech"},
}


def _teams_for(user) -> set[str]:
    return _ROLE_TEAMS.get(user.role, set())


class RespondBody(BaseModel):
    response_text: str


def _serialize(t: EscalationTicket, partner_name: str, change_title: str = "") -> dict:
    return {
        "id": t.id,
        "change_request_id": t.change_request_id,
        "change_title": change_title,
        "partner_id": t.partner_id,
        "partner_name": partner_name,
        "a2a_message_id": t.a2a_message_id,
        "team": t.team,
        "status": t.status,
        "question_text": t.question_text,
        "escalation_reason": t.escalation_reason,
        "ai_suggestion": t.ai_suggestion,
        "ai_comment_draft": t.ai_comment_draft,
        "team_response_text": t.team_response_text,
        "responded_by": t.responded_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "responded_at": t.responded_at.isoformat() if t.responded_at else None,
    }


@router.get("")
def list_escalations(
    db: DbDep,
    current_user: CurrentUser,
    status: str | None = Query(None),
    change_id: str | None = Query(None),
):
    """List escalation tickets visible to the caller's team(s)."""
    teams = _teams_for(current_user)
    if not teams:
        raise HTTPException(status_code=403, detail="No escalation team for this role")

    q = db.query(EscalationTicket).filter(EscalationTicket.team.in_(teams))
    if status:
        q = q.filter(EscalationTicket.status == status)
    if change_id:
        q = q.filter(EscalationTicket.change_request_id == change_id)
    tickets = q.order_by(EscalationTicket.created_at.desc()).all()

    partner_ids = {t.partner_id for t in tickets}
    names = {
        p.id: p.name
        for p in db.query(PartnerAgent).filter(PartnerAgent.id.in_(partner_ids)).all()
    } if partner_ids else {}

    change_ids = {t.change_request_id for t in tickets}
    titles = {
        c.id: c.title
        for c in db.query(ChangeRequest).filter(ChangeRequest.id.in_(change_ids)).all()
    } if change_ids else {}

    return [
        _serialize(t, names.get(t.partner_id, "Unknown"), titles.get(t.change_request_id, ""))
        for t in tickets
    ]


@router.post("/{ticket_id}/respond")
async def respond_escalation(
    ticket_id: str,
    body: RespondBody,
    db: DbDep,
    current_user: CurrentUser,
    background: BackgroundTasks,
):
    """Team submits its response. Marks the ticket RESPONDED and re-runs the
    resolver with the team input folded in, so the PM's partner-reply draft
    reflects the team's position."""
    ticket = db.get(EscalationTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="escalation ticket not found")
    if ticket.team not in _teams_for(current_user):
        raise HTTPException(status_code=403, detail="not your team's escalation")
    if not body.response_text.strip():
        raise HTTPException(status_code=400, detail="response_text required")

    ticket.team_response_text = body.response_text.strip()
    ticket.responded_by = current_user.id
    ticket.status = ESC_STATUS_RESPONDED
    ticket.responded_at = datetime.now(timezone.utc)
    db.commit()

    # Loop back into the resolver so the PM draft incorporates the team input.
    if ticket.a2a_message_id:
        from app.agents.feasibility_resolver import auto_resolve_background
        team_input = f"[{ticket.team.upper()} TEAM]: {ticket.team_response_text}"
        background.add_task(
            auto_resolve_background,
            ticket.a2a_message_id,
            ticket.change_request_id,
            team_input,
        )

    logger.info("escalation %s responded by %s (team=%s)", ticket_id, current_user.id, ticket.team)
    return {"status": "ok", "id": ticket.id, "ticket_status": ticket.status}
