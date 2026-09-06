# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""process_execution_audit — audit trail for ProcessExecutor invocations

Revision ID: 0126
Revises: 0125
Create Date: 2026-08-24

Closes S5 (`ARCHITECTURE_REVIEW_ACTIONS.md` — "Wrap direct OS process
execution... with... telemetry, audit and bounded execution time") per
ADR-0003 (docs/adr/ADR-0003-controlled-process-execution.md) migration
step 4: "every invocation writes one row to a new
`process_execution_audit` table... satisfying §12.2's 'audit trails'
requirement independent of application logs (which may rotate/
truncate)."

`ProcessExecutor` (backend/app/core/process_executor.py) already emits a
structured log line per invocation via its default audit sink; this
table is the durable persistence layer for that same event, for callers
that construct `ProcessExecutor(audit_sink=<db-writing sink>)` instead of
relying on the log-only default. Additive only — no existing table is
touched.

Idempotent + inspector-gated (repo convention — see 0087/0123/0124/0125).
"""
from alembic import op
import sqlalchemy as sa

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "process_execution_audit" not in insp.get_table_names():
        op.create_table(
            "process_execution_audit",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("command", sa.String(64), nullable=False),
            # Digest, NOT full args — args may reference paths that
            # incidentally include sensitive names; never the raw secret
            # VALUES, which this platform passes via env_overrides
            # (never logged/audited) rather than args, by convention.
            sa.Column("args_digest", sa.String(256), nullable=True),
            sa.Column("cwd", sa.String(500), nullable=True),
            sa.Column("run_id", sa.String(64), nullable=True, index=True),
            sa.Column("actor", sa.String(128), nullable=True),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column("duration_ms", sa.Float(), nullable=True),
            sa.Column("timed_out", sa.Boolean(), nullable=False,
                     server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.text("now()")),
        )
    existing_idx = {i["name"] for i in insp.get_indexes("process_execution_audit")} \
        if "process_execution_audit" in insp.get_table_names() else set()
    for col in ("command", "created_at"):
        name = f"ix_process_execution_audit_{col}"
        if name not in existing_idx:
            op.create_index(name, "process_execution_audit", [col])


def downgrade():
    op.drop_table("process_execution_audit")
