# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add code_plans table for Slice 12 structured code-change plans.

Plan §7.3 — structured plan as an explicit artifact, reviewed at HITL gate #3
before the editor agent runs. Table is additive — does not touch the existing
`code_iterations` / `phase_b_runs` tables.

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_plans",
        sa.Column("id",                sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("phase_b_run_id",    sa.String(36), sa.ForeignKey("phase_b_runs.id"),    nullable=True),
        sa.Column("status",            sa.String(20), nullable=False, server_default="draft"),
        sa.Column("plan_data",         sa.JSON(),     nullable=False),
        sa.Column("reviewer_comments", sa.Text(),     nullable=True),
        sa.Column("created_at",        sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at",        sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_code_plans_change_request_id",
        "code_plans",
        ["change_request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_code_plans_change_request_id", table_name="code_plans")
    op.drop_table("code_plans")
