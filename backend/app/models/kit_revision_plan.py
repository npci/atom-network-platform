# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Kit revision plan — the editable plan for the next Product Kit version.

After a negotiation round closes, the resolved outcomes (decided clusters +
doc-impact decisions) are turned into a per-document plan: for each kit
document that needs updating, a concrete change instruction + rationale. The PM
reviews/edits this plan, then triggers regeneration of v(N+1) from it.

One row per (change_request_id, target_version). `items` is an editable JSON
list — generic JSON (portable to sqlite) so the test harness doesn't choke.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


# Lifecycle. draft → (PM edits) edited → generating → generated → shipped.
RP_STATUS_DRAFT = "draft"
RP_STATUS_EDITED = "edited"
RP_STATUS_GENERATING = "generating"
RP_STATUS_GENERATED = "generated"
RP_STATUS_SHIPPED = "shipped"
# Set when the planner LLM was unavailable and the items were built by the
# deterministic fallback from the round outcomes (see revision_planner). The
# flagged docs are real (from doc-impact), but the change instructions are
# coarse — the PM should review and may re-draft once the LLM is back.
RP_STATUS_NEEDS_RETRY = "needs_retry"


class KitRevisionPlan(Base):
    __tablename__ = "kit_revision_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True,
    )
    # The kit version this plan will produce (current negotiation_version + 1).
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=RP_STATUS_DRAFT)

    # Editable per-document plan. Each item:
    #   {"doc_type": "faq", "change_instruction": "...", "rationale": "...",
    #    "include": true}
    items: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # One-paragraph overview of what changes v(N)→v(N+1) (seeds the summary doc).
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=True,
    )
