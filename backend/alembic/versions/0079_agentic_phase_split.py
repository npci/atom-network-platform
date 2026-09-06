# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic Phase A/B split — columns on agentic_runs.

THE BOOK v3.4. Splits the single agentic run (xsd→code chained) into two
human-gated stages: Phase A (kind='xsd', generate + approve the schema) and
Phase B (kind='code', adopt Phase A's workspace and finish the code → one
combined MR). Adds four nullable/defaulted columns to ``agentic_runs``:

    kind              "full" (legacy default) | "xsd" | "code"
    parent_run_id     the Phase-A run a Phase-B run continues
    workspace_run_id  the run whose on-disk clone holds the shared tree
    handoff_json      serialized XsdScope + changed-XSD contents (no context gap)

Idempotent + inspector-gated   so re-runs are no-ops. JSON uses
the generic ``JSON`` type to match the ORM (``app/models/agentic.py``).

Revision ID: 0079
Revises: 0078
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa


revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "agentic_runs" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("agentic_runs")}
    if "kind" not in cols:
        op.add_column("agentic_runs",
                      sa.Column("kind", sa.String(8), nullable=False, server_default="full"))
    if "parent_run_id" not in cols:
        op.add_column("agentic_runs", sa.Column("parent_run_id", sa.String(36), nullable=True))
    if "workspace_run_id" not in cols:
        op.add_column("agentic_runs", sa.Column("workspace_run_id", sa.String(36), nullable=True))
    if "handoff_json" not in cols:
        op.add_column("agentic_runs", sa.Column("handoff_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "agentic_runs" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("agentic_runs")}
    for c in ("handoff_json", "workspace_run_id", "parent_run_id", "kind"):
        if c in cols:
            op.drop_column("agentic_runs", c)
