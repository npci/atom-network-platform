# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""agentic_run_repos.pushed_manifest_hash — bind push state to the approved content

A repo's push_state="pushed" used to mean "pushed once, forever done": a later
re-freeze (fix rounds after an early approve+push) changed the manifest and
invalidated the approval, but the push skip-guard still no-op'd the re-consented
push and reported success — git silently kept the older approved state. Recording
WHICH manifest_hash was pushed lets _push_all distinguish a crash-recovery
re-dispatch (same hash → skip) from a stale branch (hash changed → re-push).

NULL on pre-existing rows = unknown; treated as current (no surprise re-pushes
of historical runs).

Idempotent + inspector-gated (safe to re-run).

Revision ID: 0109
Revises: 0108
"""
from alembic import op
import sqlalchemy as sa

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("agentic_run_repos")}
    if "pushed_manifest_hash" not in cols:
        op.add_column("agentic_run_repos",
                      sa.Column("pushed_manifest_hash", sa.String(64), nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("agentic_run_repos")}
    if "pushed_manifest_hash" in cols:
        op.drop_column("agentic_run_repos", "pushed_manifest_hash")
