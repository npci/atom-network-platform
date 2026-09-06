# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CodeRepo model — tracks GitLab repositories registered for code indexing."""
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, Text, JSON, Boolean
from sqlalchemy.orm import mapped_column, Mapped
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid, utcnow


class CodeRepo(Base, TimestampMixin):
    __tablename__ = "code_repos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    gitlab_url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # override global setting
    gitlab_repo: Mapped[str] = mapped_column(String(500), nullable=False)       # e.g. "root/network-platform"
    gitlab_branch: Mapped[str] = mapped_column(String(200), nullable=False, default="main")
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    files_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunks_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Admin marks one indexed (repo, branch) row as the API Registry's production
    # baseline source — what the deterministic XSD re-ingest clones from.
    is_registry_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ── Agentic codegen: inter-repo build graph + smoke/module hints (§20/§5) ──
    # role: "core" | "app" | "legacy". depends_on: list of repo_ids this repo's
    # build depends on (network-2.0 -> network-core). locations: smoke/module hints
    # (e.g. {"smoke": {...}, "module_roots": [...]}).
    # Index-time commit provenance lives in code_repo_state.last_ingested_sha,
    # so no indexed_commit_sha column is added here (reuse, not duplicate).
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    depends_on: Mapped[list | None] = mapped_column(JSON, nullable=True)
    locations: Mapped[dict | None] = mapped_column(JSON, nullable=True)
