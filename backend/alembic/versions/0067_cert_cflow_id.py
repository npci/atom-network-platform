# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Protocol v1 — `cflow_id` on cert_runs.

Adds the certification master identifier (PDF §5) that threads the whole cert
lifecycle for one (change, partner) effort. `cert_attempt` in the wire protocol
maps to the existing `cert_runs.run_number`, so only `cflow_id` is new.

Idempotent + inspector-gated (house pattern since 0035) so re-runs against an
already-migrated DB are no-ops. Plain String — no Postgres-native type, so no
SQLite variant needed.

Revision ID: 0067
Revises: 0066
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("cert_runs")}
    if "cflow_id" not in cols:
        op.add_column("cert_runs", sa.Column("cflow_id", sa.String(64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("cert_runs")}
    if "cflow_id" in cols:
        op.drop_column("cert_runs", "cflow_id")
