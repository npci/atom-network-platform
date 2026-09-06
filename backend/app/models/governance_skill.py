# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance skill files (EA / InfoSec review rulebooks) — append-only versions.

Each row is one immutable uploaded version of a skill document; the ACTIVE
version of a type is simply the highest ``version`` for that ``skill_type``.
Uploads INSERT max+1 — there are no UPDATE or DELETE paths, so the table is
its own audit trail (which rulebook was in force when a review ran is pinned
by {type, version, checksum} on the governance run itself).

Follows the Authority-policy pattern (admin-uploaded .md stored verbatim as TEXT,
injected whole into agent prompts — never chunked or retrieved), extended
with versioning because governance reviews must be reproducible.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Integer, LargeBinary, String, Text, JSON, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


class GovernanceSkill(Base):
    __tablename__ = "governance_skills"
    __table_args__ = (
        UniqueConstraint("skill_type", "version", name="uq_governance_skill_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    skill_type: Mapped[str] = mapped_column(String(16), nullable=False)  # "ea" | "infosec"
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # ── Skill SLOT (0118): a type may hold SEVERAL skills side by side (the org's
    # InfoSec repo ships four). ``name`` — derived from SKILL.md frontmatter at
    # upload — identifies the slot; a slot's active row is its highest version and
    # every ENABLED slot executes in the stage. Versions stay GLOBAL per type so
    # the (skill_type, version) pin on runs is still unambiguous. Pre-slot rows
    # backfill to 'default'. ``enabled`` retires a slot without deleting audit rows.
    name: Mapped[str] = mapped_column(String(120), nullable=False, server_default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    content: Mapped[str] = mapped_column(Text, nullable=False)  # verbatim SKILL.md
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # ── Bundle columns (0117) — the Agent-Skill shape. NULL bundle_bytes = a
    # legacy/markdown-only skill (degenerate bundle: SKILL.md only, zero scripts).
    # bundle_bytes is DEFERRED: it holds the multi-MB archive and is needed only at
    # materialization/smoke (rare), NOT on the hot status-poll path (active_skills /
    # list_skills). A plain load would pull every historical zip per 5s poll.
    bundle_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    bundle_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bundle_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manifest_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    exec_manifest_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    safety_warnings_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    provenance_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # prove-it-runs gate: pending|green|failed (NULL = no scripts, nothing to prove).
    # A scripted bundle whose smoke is not green can NEVER gate a change.
    smoke_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    smoke_detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Parsed [{"id": ..., "title": ...}] snapshot taken at upload (audit aid only —
    # the agents re-parse `content` at run time so parser fixes apply retroactively).
    rules_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
