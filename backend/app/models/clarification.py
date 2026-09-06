# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Clarification — PM-facing pre-generation gap questions.

One row per (change_request_id, version). Only the latest version is
consumed when generating downstream documents.
"""
from datetime import datetime

from sqlalchemy import JSON, String, Integer, ForeignKey, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid

# JSONB on Postgres, plain JSON on the SQLite test harness (house pattern —
# see kit_publication.py; raw JSONB breaks create_all under SQLite).
_JSON = JSONB().with_variant(JSON(), "sqlite")


class Clarification(Base, TimestampMixin):
    __tablename__ = "clarifications"
    __table_args__ = (
        UniqueConstraint("change_request_id", "version", name="uq_clarifications_change_version"),
        Index("ix_clarifications_change_id", "change_request_id"),
    )

    id:                Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id", ondelete="CASCADE"), nullable=False,
    )
    version:           Mapped[int] = mapped_column(Integer, nullable=False)

    blocking_gap_keys: Mapped[list | None]  = mapped_column(_JSON, nullable=True)
    assumed_gaps:      Mapped[list | None]  = mapped_column(_JSON, nullable=True)

    questions:         Mapped[list | None]  = mapped_column(_JSON, nullable=True)
    answers:           Mapped[dict | None]  = mapped_column(_JSON, nullable=True)

    status:            Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    change_request = relationship("ChangeRequest")
