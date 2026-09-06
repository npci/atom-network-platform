# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Allow placeholder approvals without a concrete approver assignment.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-03
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "approvals",
        "approver_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "approvals",
        "approver_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
