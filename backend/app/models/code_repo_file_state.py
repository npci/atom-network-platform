# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SQLAlchemy model for the code_repo_file_state table (Slice 26).

One row per (repo_id, source_file). Persists the SHA256 of the file's
content at last successful ingest. The polyglot-incremental path reads
this on each run, diffs the live file set against it, and only re-
processes files whose hash changed.

The unique `(repo_id, source_file)` constraint is what makes the
incremental algorithm idempotent — re-running with no file changes
yields zero chunk inserts/deletes.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


class CodeRepoFileState(Base):
    """Snapshot of one source file's hash at last successful ingest."""
    __tablename__ = "code_repo_file_state"

    id:           Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    repo_id:      Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_file:  Mapped[str] = mapped_column(String(1000), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    language:     Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    created_at:   Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("repo_id", "source_file", name="uq_code_repo_file_state_pair"),
    )
