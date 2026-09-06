# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cold-storage manifest for compressed copies of aging artifacts.

Closes architecture review Finding #8 ("No Data Tiering for Large
Payloads") — see `app/services/artifact_tiering.py` for the compression
and archive-eligibility jobs that populate this table, and
`docs/ARCHITECTURE_REVIEW_REMEDIATION.md` §7 for the full design.

This table is a MANIFEST, not the compressed data's system of record —
the actual bytes live in a gzip file on disk (workspace-local cold
storage, `settings.artifact_coldstore_dir`), and this row just points at
it. The source row's own columns (`tech_specs.content`,
`brds.content`, `a2a_messages.payload`, ...) are NEVER modified or
deleted by this subsystem — compression is purely additive. Nothing in
this codebase deletes application data as part of tiering; the
`ready_for_archive` flag is a signal for a human/ops process to
consume, not an automated deletion trigger.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid


class ArtifactColdStorage(Base):
    """One row per (source_table, source_id) that has been compressed into
    workspace-local cold storage. See module docstring for the safety
    model — this is additive bookkeeping, never a replacement for or a
    trigger to delete the source row."""

    __tablename__ = "artifact_cold_storage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    # Which source table + row this describes. Not a foreign key — the
    # source table varies (tech_specs / brds / a2a_messages / ...) and this
    # manifest must survive even if the source row is later removed by an
    # unrelated process.
    source_table: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    change_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Path relative to settings.artifact_coldstore_dir — e.g.
    # "tech_specs/2026/08/<id>.json.gz".
    coldstore_path: Mapped[str] = mapped_column(String(500), nullable=False)
    original_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compressed_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compressed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # Set by the archive-eligibility sweep (compress_at + archive_after_days
    # elapsed). A FLAG for an operator/ops process to act on — nothing in
    # this codebase moves data to external archive storage or deletes
    # anything automatically.
    ready_for_archive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
