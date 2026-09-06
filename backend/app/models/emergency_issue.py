# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Emergency issues — the only partner→the Authority channel after a change freezes.

Once the final Product Kit (v3) ships, negotiation is frozen: the executor
rejects inbound queries and counter-proposals. A partner that hits a critical,
work-stopping problem raises an EmergencyIssue instead (the "emergency blocker
button" on the partner side). The Authority triages and resolves it.

Deliberately separate from Blocker: a Blocker is a mid-implementation obstacle
during normal negotiation; an EmergencyIssue is a post-freeze break-glass
signal that something is stopping the partner from proceeding at all.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


# Stored as plain strings; these are the allowed values.
EMERGENCY_SEVERITIES = ("critical", "high", "medium", "low")

EI_STATUS_OPEN = "open"
EI_STATUS_ACKED = "acknowledged"
EI_STATUS_RESOLVED = "resolved"


class EmergencyIssue(Base):
    __tablename__ = "emergency_issues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True,
    )
    partner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("partner_agents.id"), nullable=False, index=True,
    )
    # Partner-supplied wire id (e.g. "EMG-001"), distinct from the DB pk.
    issue_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="critical")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EI_STATUS_OPEN, index=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Column name predates the rename and is retained for schema stability —
    # renaming a live column is a data migration, not a refactor.
    authority_resolution_text: Mapped[str | None] = mapped_column(
        "npci_resolution_text", Text, nullable=True,
    )
    resolved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
