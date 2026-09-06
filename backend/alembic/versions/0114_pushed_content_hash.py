# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase B push idempotency — bind push state to the pushed CONTENT.

``phase_b_run_repos.pushed_content_hash`` (sha256 of the per-repo files payload)
is the Phase-B analogue of the agentic ``pushed_manifest_hash`` (0109): "an MR
exists" is not "the current content is pushed". A re-approved fix round now
re-pushes; an unchanged payload stays idempotent. NULL = legacy row (unknown) —
treated as pushed, never surprise-re-pushed.

Idempotent + inspector-gated (safe to re-run).

Revision ID: 0114
Revises: 0113
"""
from alembic import op
import sqlalchemy as sa

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "phase_b_run_repos" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("phase_b_run_repos")}
    if "pushed_content_hash" not in cols:
        op.add_column("phase_b_run_repos",
                      sa.Column("pushed_content_hash", sa.String(64), nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "phase_b_run_repos" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("phase_b_run_repos")}
    if "pushed_content_hash" in cols:
        op.drop_column("phase_b_run_repos", "pushed_content_hash")
