# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add app_configs table for platform settings.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_configs",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text, nullable=False, server_default=""),
        sa.Column("category", sa.String(50), nullable=False, server_default="general"),
        sa.Column("is_secret", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("app_configs")
