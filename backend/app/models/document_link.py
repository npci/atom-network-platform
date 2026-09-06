# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SQLAlchemy model for the doc_code_links table (Slice 18)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Float, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import mapped_column, Mapped

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


class DocCodeLink(Base):
    """Edge from a doc chunk to a code symbol chunk with LLM-scored confidence.

    Populated post-ingest by `doc_code_linker.link_chunks`. Unique per
    `(doc_chunk_id, symbol_chunk_id)` so the linker is idempotent: re-runs
    UPDATE confidence + last_checked, don't duplicate.

    Both FKs point at `document_chunks.id` — the "doc" side is a row whose
    `doc_category` is a narrative category (RBI_GUIDELINE, NETWORK_PRODUCT_DOC,
    PAST_BRD, etc.) and the "symbol" side is typically a row whose
    `doc_category == JAVA_SOURCE` and `symbol_kind` is set (Slice 3+).
    """
    __tablename__ = "doc_code_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    doc_chunk_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False,
    )
    symbol_chunk_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    last_checked: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("doc_chunk_id", "symbol_chunk_id", name="uq_doc_code_links_pair"),
    )
