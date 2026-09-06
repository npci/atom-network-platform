# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Durable changes-artifact — change_manifests.diffs.

Stores the full per-repo unified git diff captured at freeze time, so the UI can
show the change-set as an inspectable artifact during AND long after the run
(survives workspace GC + the post-push commit that empties `git diff HEAD`).
Idempotent + inspector-gated .

Revision ID: 0081
Revises: 0080
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa


revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "change_manifests" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("change_manifests")}
    if "diffs" not in cols:
        op.add_column("change_manifests", sa.Column("diffs", sa.JSON(), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "change_manifests" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("change_manifests")}
    if "diffs" in cols:
        op.drop_column("change_manifests", "diffs")
