# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add kit_revision_plans — editable plan for the next Product Kit version.

Built from the round's resolved outcomes; the PM edits it, then regenerates
v(N+1) from it. One row per (change_request_id, target_version).

Revision ID: 0073
Revises: 0072
Create Date: 2026-06-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "kit_revision_plans" in insp.get_table_names():
        return

    op.create_table(
        "kit_revision_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "change_request_id", sa.String(36),
            sa.ForeignKey("change_requests.id"), nullable=False, index=True,
        ),
        sa.Column("target_version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("items", sa.JSON, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("updated_by", sa.String(36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Unique: one plan row per (change, target_version). Enforces the model's
    # "one row per (change_request_id, target_version)" invariant so the Celery
    # sweep and the manual draft endpoint can't both insert a duplicate plan
    # for the same target version (their check-then-insert is racy).
    op.create_index(
        "ix_kit_revision_plans_change_version",
        "kit_revision_plans",
        ["change_request_id", "target_version"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "kit_revision_plans" in insp.get_table_names():
        op.drop_index("ix_kit_revision_plans_change_version", table_name="kit_revision_plans")
        op.drop_table("kit_revision_plans")
