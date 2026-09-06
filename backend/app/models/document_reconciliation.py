# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Reconcile an uploaded document against the ratified Change-Analysis plan.

When a user uploads a BRD instead of using the generated one, it can contradict
the code-grounded plan, or silently drop something the plan requires. This row is
the per-upload record of that check: the detected conflicts (in the clarification
question shape), the user's resolutions, and a status the async gate blocks
downstream generation on while `pending`.

One row per upload event that produced conflicts. ``doc_kind`` is "brd" today; the
whole reconciliation flow is doc-kind-parameterized so TSD reconciliation is a
later switch-on.

Conventions : generic JSON (not JSONB); String status
column (no native PG enum → new states need no ALTER TYPE).
"""
from datetime import datetime

from sqlalchemy import String, Integer, JSON, ForeignKey, Index
from sqlalchemy.orm import mapped_column, Mapped

from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class DocumentReconciliation(Base, TimestampMixin):
    __tablename__ = "document_reconciliations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True
    )
    # "brd" now; "tech_spec" later — the whole reconciliation flow keys off this.
    doc_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # The uploaded artifact row (e.g. brds.id) + its version — for the
    # "conflicts for BRD v2" display and the staleness binding.
    doc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    doc_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The ChangeAnalysis.version the upload was reconciled against.
    plan_version_before: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Lifecycle (the gate treats pending + applying as "open — doc not final"):
    #   pending   — conflicts awaiting the user's resolution
    #   applying  — resolved; the corrected/regenerated doc is being produced (async)
    #   resolved  — corrections done; the doc is final and can go to approval
    #   applied   — brd_wins deltas folded into a new plan version (at BRD approval)
    #   superseded — replaced by a re-upload / revert / dismiss
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    # Detected conflicts in the clarification question shape:
    #   [{id, text, jurisdiction, kind, severity, evidence, options:[{id,label}]}]
    conflicts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # {conflict_id: {chosen_option_id, custom_answer}} — filled at resolve time.
    resolutions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # delta_grounding output — {status, grounded_at, deltas:[...]} — computed in the
    # 'applying' phase; merged into the plan at the fold, surfaced on the panel (S2/S3).
    grounding: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# Gate lookups filter on (change_request_id, doc_kind, status="pending").
Index(
    "ix_document_reconciliations_change_kind_status",
    DocumentReconciliation.change_request_id,
    DocumentReconciliation.doc_kind,
    DocumentReconciliation.status,
)
