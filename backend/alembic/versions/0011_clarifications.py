# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Clarification sessions — questions asked of the PM before document generation.

One row per (change_request, version). New clarification runs bump the
version so history is preserved. Only the latest version is consumed by
the BRD/TSD/Product Kit agents.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add CLARIFICATION value to the existing changestatus enum.
    # Postgres allows ADD VALUE but only outside a transaction block; alembic
    # handles that with the `.autocommit_block()` context.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE changestatus ADD VALUE IF NOT EXISTS 'clarification'")

    op.create_table(
        "clarifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36),
                  sa.ForeignKey("change_requests.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("version", sa.Integer, nullable=False),

        # Detector output
        sa.Column("blocking_gap_keys", postgresql.JSONB, nullable=True),   # ["mandate_type", ...]
        sa.Column("assumed_gaps",      postgresql.JSONB, nullable=True),   # [{key, default, reason}]

        # Questions and answers
        sa.Column("questions", postgresql.JSONB, nullable=True),  # [{id, text, gap_key, required, category}]
        sa.Column("answers",   postgresql.JSONB, nullable=True),  # {question_id: "answer text"}

        # Lifecycle
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        # pending: generated, awaiting PM
        # answered: PM has submitted all required answers
        # skipped: no blocking gaps → user chose to proceed without answering
        # stale: research/canvas changed after clarification was produced

        # Timestamps — match TimestampMixin (updated_at nullable on insert)
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_clarifications_change_version",
        "clarifications",
        ["change_request_id", "version"],
    )
    op.create_index(
        "ix_clarifications_change_id",
        "clarifications",
        ["change_request_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_clarifications_change_id", table_name="clarifications")
    op.drop_constraint("uq_clarifications_change_version", "clarifications", type_="unique")
    op.drop_table("clarifications")
