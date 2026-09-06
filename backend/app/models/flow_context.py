# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Index-time API FLOW context (THE BOOK v3.4, reuse-first §).

One row per repo: a precomputed map of how the system's APIs compose into
transaction flows — crucially, WHICH existing API carries the actual financial
leg (the debit/credit) versus the metadata/initiation/status APIs around it, and
how multi-leg flows are sequenced. Generated at index time (parallel to
``module_context``) and surfaced to the reuse-first approach gate so the agent
reasons over a ready flow map instead of rediscovering it each run.

LOW-AUTHORITY orientation, like ``module_context``: stamped with
``base_commit_sha`` for staleness; the agent still confirms against the clone.
"""
from datetime import datetime

from sqlalchemy import String, Text, JSON, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


class FlowContext(Base):
    """One flow-map row per repo (unique on ``repo_id``)."""
    __tablename__ = "flow_context"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    repo_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("code_repos.id"), nullable=False, index=True, unique=True
    )
    # Plain-language narrative of how transactions flow through the system.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{api, why}] — APIs that perform the actual money movement (debit/credit leg).
    transaction_apis: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # [{api, why}] — initiation / status / metadata APIs around the transaction.
    meta_apis: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # [{name, steps:[...]}] — multi-leg flow sequences.
    flows: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Raw discovered entry points the map was built from (advisory).
    entry_points: Mapped[list | None] = mapped_column(JSON, nullable=True)

    base_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
