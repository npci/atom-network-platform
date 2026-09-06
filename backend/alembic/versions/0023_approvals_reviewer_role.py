# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add `reviewer_role` column to approvals table.

The Approval model added this nullable column for placeholder approvals
(e.g. storing "tech_lead" so the UI knows which role is needed even
before a specific user is assigned) but the column was never added to
the initial schema via migration.

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("reviewer_role", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("approvals", "reviewer_role")
