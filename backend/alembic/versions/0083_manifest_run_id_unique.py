# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add unique constraint on change_manifests.run_id.

The ORM declares ``uselist=False`` on ``AgenticRun.manifest``, expecting at
most one manifest per run. This migration enforces that at the DB level.

Idempotent + inspector-gated  .

Revision ID: 0083
Revises: 0082
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "change_manifests" not in tables:
        return
    existing = {idx["name"] for idx in insp.get_indexes("change_manifests")}
    if "uq_change_manifests_run_id" not in existing:
        op.create_index("uq_change_manifests_run_id", "change_manifests", ["run_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_change_manifests_run_id", table_name="change_manifests")
