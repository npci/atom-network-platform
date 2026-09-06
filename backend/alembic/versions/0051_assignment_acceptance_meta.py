# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-assignment acceptance metadata (rollout-contract structured fields).

Adds one column:

  change_partner_assignments.acceptance_meta   JSONB   NULL
                                               written by:
                                                 - process_proposal_acknowledged
                                                   (kit_files_received[],
                                                    checksum verification,
                                                    received_at)
                                                 - process_change_acknowledgement
                                                   (decision, accepted_by,
                                                    internal_change_advisory_ref,
                                                    estimated_phase_timeline)
                                                 NULL = no ack/acceptance recorded yet.

Why a single JSON column rather than named columns: the partner-driven
fields are the rollout-doc contract, which partners may extend with
fields we don't model. Persisting the whole structured payload keeps
the audit trail intact; named-column projections can be added later if
the dashboard needs them.

Idempotent inspector-gated. JSONB on Postgres / JSON variant on SQLite
test backends.

Revision ID: 0051
Revises: 0050
Create Date: 2026-05-10

Renumbered from 0043 → 0051 during the a2acert merge: a sibling 0043
(`product_kit_pptx_path`) landed on main first, and the cert-lifecycle
arc on the branch had already filled 0044-0050. This migration was
re-slotted at the chain tail so the linear history stays unambiguous.
The acceptance_meta column has no FK dependencies, so position is
purely organisational.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("change_partner_assignments")}
    if "acceptance_meta" not in existing:
        op.add_column(
            "change_partner_assignments",
            sa.Column(
                "acceptance_meta",
                postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("change_partner_assignments")}
    if "acceptance_meta" in existing:
        op.drop_column("change_partner_assignments", "acceptance_meta")
