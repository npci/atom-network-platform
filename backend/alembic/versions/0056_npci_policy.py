# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""npci_policy — singleton table holding the NPCI_POLICY.md content
loaded by the feasibility resolver.

Inspector-gated + idempotent . Safe tore-run against an already-migrated DB.

Revision ID: 0056
Revises:    0055
Create Date: 2026-05-27
"""
import sqlalchemy as sa
from alembic import op


revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "npci_policy" not in insp.get_table_names():
        op.create_table(
            "npci_policy",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("updated_by", sa.String(36), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint("id = 1", name="ck_npci_policy_singleton"),
        )


def downgrade() -> None:
    op.drop_table("npci_policy")
