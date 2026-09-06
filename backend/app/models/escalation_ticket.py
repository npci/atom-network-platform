# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Escalation tickets — route a partner query to Risk / InfoSec / Tech.

Created by the feasibility resolver when its recommended_action is
"escalate". One ticket per (a2a_message, team). The team responds from its
inbox; the response is folded back into the resolver draft the PM reviews
before replying to the partner.

`team` is intentionally constrained to exactly the three teams the
negotiation protocol allows — Risk, InfoSec, Tech. Stored as VARCHAR (not a
Postgres enum) to keep the migration simple and the sqlite test harness happy,
matching the convention used by phase_c / eval_verdicts.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


# Stored as plain strings; these constants are the allowed values.
ESCALATION_TEAMS = ("risk", "infosec", "tech")

ESC_STATUS_OPEN = "open"
ESC_STATUS_RESPONDED = "responded"
ESC_STATUS_CLOSED = "closed"


class EscalationTicket(Base):
    __tablename__ = "escalation_tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True,
    )
    partner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("partner_agents.id"), nullable=False,
    )
    # The inbound query/counter that triggered the escalation.
    a2a_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("a2a_messages.id"), nullable=True, index=True,
    )
    # Optional cross-partner cluster link (reserved for cluster-level escalation).
    cluster_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    team: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ESC_STATUS_OPEN, index=True,
    )

    # The partner's question, captured at escalation time so the team inbox
    # is self-contained (doesn't have to re-derive it from the A2A payload).
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Why the resolver escalated (its action_summary).
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The AI's full team assessment (detailed reasoning), shown to the reviewer
    # as context.
    ai_suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The concise, submittable review-comment draft that pre-fills the
    # reviewer's response box (they validate/edit/replace it).
    ai_comment_draft: Mapped[str | None] = mapped_column(Text, nullable=True)

    team_response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
