# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic durable progress ledger — add agentic_runs.progress_ledger_json.

A nullable JSON column with two uses on `agentic_runs`:
  - stall-escalation history (`blocker_keys`) — written by DEFAULT whenever
    `agentic_max_stall_rounds > 0` (default 2), so a resumed run keeps its window;
  - files-already-read + verify-failure history at the CODE_CHANGE boundary — gated
    by `agentic_progress_ledger` (default OFF).
So the column IS used by default (via the stall ledger); it is not purely opt-in.

Idempotent + inspector-gated . Generic JSON (not JSONB) so the SQLite
test harness works without a variant.

Revision ID: 0090
Revises: 0089
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "agentic_runs" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("agentic_runs")}
    if "progress_ledger_json" not in cols:
        op.add_column("agentic_runs", sa.Column("progress_ledger_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "agentic_runs" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("agentic_runs")}
    if "progress_ledger_json" in cols:
        op.drop_column("agentic_runs", "progress_ledger_json")
