# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cached per-change-request context: taxonomy, retrieved chunks, and structured proposals.

One row per change request. Populated at the end of Research (or lazily on
first BRD/Tech Spec generation) and then reused by every downstream Phase A
agent. See services/context_cache.py for the orchestration logic.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String, Text, Float, Integer, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin

# JSONB on Postgres, plain JSON on the SQLite test harness (house pattern —
# see kit_publication.py; raw JSONB breaks create_all under SQLite).
_JSON = JSONB().with_variant(JSON(), "sqlite")


class ChangeRequestContext(Base, TimestampMixin):
    __tablename__ = "change_request_contexts"

    change_request_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("change_requests.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Taxonomy
    taxonomy_primary:    Mapped[str | None] = mapped_column(String(64), nullable=True)
    taxonomy_labels:     Mapped[list | None] = mapped_column(_JSON, nullable=True)
    taxonomy_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    taxonomy_rationale:  Mapped[str | None] = mapped_column(Text, nullable=True)

    # Retrieved chunks — stored as list[dict] with id/source/content/score
    retrieved_chunks: Mapped[list | None] = mapped_column(_JSON, nullable=True)

    # Structured proposals
    proposals:            Mapped[dict | None] = mapped_column(_JSON, nullable=True)
    proposals_confidence: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Inferred the network parties in-scope (v3 — replaces the 4-yes/no clarification
    # fan-out). Shape: PartyInferenceResult.model_dump() —
    # {parties_in_scope: list[str], rationale: str, confidence: str, source: str}.
    # Consumed by question_generator.build_scope_signal_questions to
    # pre-check the parties multi-select clarification question.
    parties_inference:   Mapped[dict | None] = mapped_column(_JSON, nullable=True)

    # Metadata
    last_refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_version:    Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationship back to change request
    change_request = relationship("ChangeRequest")
