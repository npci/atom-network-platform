# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Models for the Change Analysis stage + Decision Ledger (accuracy upgrade S1).

- ChangeAnalysis: the code-grounded analysis produced before the BRD. Carries a
  dual view (technical_analysis = full fidelity, never shown to the PM; and
  functional_plan = PM-facing), a machine-readable flow_spec that owns step IDs,
  the SHA the analysis was grounded on, the BRD-version binding for staleness,
  and the split ratification columns (PM ratifies functional, tech-lead the technical).
- DecisionLedgerEntry: append-only, per-question supersession. Every gate answer
  lands here; downstream agents receive a binding DECISIONS block built from the
  ACTIVE (non-superseded) entries.
- ChangeImpactedPath: queryable impacted XSD/module rows (not JSON-only) so
  cross-change collision detection is one intersection query.

Conventions : generic JSON (not JSONB); String
columns for enums (no native PG enum → new states need no ALTER TYPE).
"""
from datetime import datetime

from sqlalchemy import String, Text, Integer, JSON, ForeignKey, DateTime, Index
from sqlalchemy.orm import mapped_column, Mapped

from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class ChangeAnalysis(Base, TimestampMixin):
    __tablename__ = "change_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # draft | awaiting_clarifications | awaiting_ratification | ratified | superseded | skipped
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")

    # Full-fidelity, never shown to the PM. {impacted_repos, modules, flows,
    # schema_inventory:[{repo,path,namespace}], data_model_changes, reuse_findings,
    # constraints, risks}.
    technical_analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # PM-facing rendering; every statement must derive from a technical finding.
    functional_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # actors/steps/messages/states — OWNS step IDs that BRD/TSD render from.
    flow_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {repo_id: sha} the analysis was grounded on (= last_ingested_sha at run start).
    analysis_sha: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Staleness binding — NOT created_at (in-place version bumps don't move it).
    validated_against_brd_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    validated_against_brd_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validated_against_brd_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Split ratification: PM ratifies the functional plan; tech-lead the technical analysis.
    pm_ratified_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    pm_ratified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tech_ratified_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tech_ratified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The analysis-kind AgenticRun that produced this (nullable for degraded path).
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class DecisionLedgerEntry(Base, TimestampMixin):
    __tablename__ = "decision_ledger_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True
    )
    # Stable key; a new answer for an existing key appends with supersedes_id set.
    question_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # clarification | contract_direction | plan_ratification | assumption |
    # gate_decision | analysis_skipped | revalidation
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    chosen: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # System-rendered binding directive (NOT raw PM prose — trust boundary).
    directive: Mapped[str | None] = mapped_column(Text, nullable=True)

    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Artifact id+version this decision was made against (staleness).
    decided_against: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Append-only supersession: points at the entry this one replaces.
    supersedes_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ChangeImpactedPath(Base, TimestampMixin):
    __tablename__ = "change_impacted_paths"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True
    )
    repo_id: Mapped[str] = mapped_column(String(36), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    namespace: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # xsd | module | symbol
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)


# Cross-change collision lookups intersect on (repo_id, path).
Index("ix_change_impacted_paths_repo_path", ChangeImpactedPath.repo_id, ChangeImpactedPath.path)
