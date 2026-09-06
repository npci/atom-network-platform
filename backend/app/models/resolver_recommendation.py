# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Resolver recommendations — one per inbound partner message.

Each row holds the feasibility resolver's structured recommendation for
a PM reviewing a partner query or counter-proposal. Keyed on the
A2AMessage that triggered it (the executor's audit row).
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


class ResolverRecommendation(Base):
    __tablename__ = "resolver_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True,
    )
    partner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("partner_agents.id"), nullable=False,
    )
    a2a_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("a2a_messages.id"), nullable=False, index=True,
    )
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
