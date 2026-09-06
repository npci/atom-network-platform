# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 4.1 — One-row-per-repo state for git-diff-based incremental ingest.

Distinct from `CodeRepoFileState` (which records per-file content hashes)
because the SHA is a top-level repo property, not a per-file one. Keeping
them in separate tables avoids the schema gymnastics of "store the SHA on
one arbitrary row" and lets the Phase 4.2 git-diff path be added without
touching the per-file state at all.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CodeRepoState(Base):
    __tablename__ = "code_repo_state"

    repo_id:              Mapped[str] = mapped_column(String(36), primary_key=True)
    last_ingested_sha:    Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_ingested_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ingested_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
