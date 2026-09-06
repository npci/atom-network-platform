# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic prod-hardening columns: created_by (per-run authz), error_code
(structured failure triage), last_heartbeat_at (stuck-run observability).

Idempotent + inspector-gated  . All nullable so existing rows
need no backfill. Generic types to match the ORM.

Revision ID: 0082
Revises: 0081
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "agentic_runs" not in set(insp.get_table_names()):
        return  # branch-skewed / partially-restored DB — nothing to alter
    cols = {c["name"] for c in insp.get_columns("agentic_runs")}
    if "created_by" not in cols:
        op.add_column("agentic_runs", sa.Column("created_by", sa.String(36), nullable=True))
    if "error_code" not in cols:
        op.add_column("agentic_runs", sa.Column("error_code", sa.String(64), nullable=True))
    if "last_heartbeat_at" not in cols:
        op.add_column("agentic_runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("agentic_runs")}
    for c in ("last_heartbeat_at", "error_code", "created_by"):
        if c in cols:
            op.drop_column("agentic_runs", c)
