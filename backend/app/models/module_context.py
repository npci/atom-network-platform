# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Module-wise hierarchical context generated at index time (THE BOOK §19).

The "heart" of the agentic codegen design: a per-module knowledge file
(module → submodule → sub-submodule) produced when the code index is built and
injected as **labeled orientation only** during coding. Concrete details are
always re-derived on demand via grep/glob/read against the clone — these rows
orient, they never substitute for reading.
"""
from datetime import datetime

from sqlalchemy import String, Text, Integer, ForeignKey, JSON, DateTime
from sqlalchemy.orm import mapped_column, Mapped

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


class ModuleContext(Base):
    """One row per Maven module / nested submodule (any depth), per repo.

    Provenance-stamped with ``base_commit_sha`` (ties into §5 stale detection).
    An in-repo ``MODULE_NOTES.md`` overrides this generated row (hybrid rule,
    §14). Unique on ``(repo_id, module_path)`` (migration M-D).
    """
    __tablename__ = "module_context"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    repo_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_repos.id"), nullable=False, index=True)
    module_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    parent_module_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_types: Mapped[list | None] = mapped_column(JSON, nullable=True)
    entry_points: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Code-derived "how it actually works end to end" narrative (low-authority).
    functional_flow: Mapped[str | None] = mapped_column(Text, nullable=True)
    conventions: Mapped[str | None] = mapped_column(Text, nullable=True)
    gotchas: Mapped[str | None] = mapped_column(Text, nullable=True)
    why: Mapped[str | None] = mapped_column(Text, nullable=True)
    java_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    depends_on: Mapped[list | None] = mapped_column(JSON, nullable=True)

    base_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RepoPathContext(Base):
    """DB fallback for in-repo ``MODULE_NOTES.md`` context-as-asset (§14).

    In-repo notes win on conflict; this table holds the same content for repos
    where committing a notes file in the MR is not desired.
    """
    __tablename__ = "repo_path_context"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    repo_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_repos.id"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
