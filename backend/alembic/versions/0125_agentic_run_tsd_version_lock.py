# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""agentic_runs.tsd_version_locked — version-lock the TSD contract per run

Revision ID: 0125
Revises: 0124
Create Date: 2026-08-22

Closes SDLC review gap 4 ("TSD treated as prose document, not binding
contract") — part 2 of 3, per ADR-0005. Adds a single nullable column to
``agentic_runs``: the ``TechSpec.version`` that was APPROVED at the moment
this run entered CODE_CHANGE. ``read_doc(doc='tsd')`` resolves against
this locked version (falling back to "latest" only when the column is
NULL — a legacy/pre-migration run, or a run that never reached
CODE_CHANGE) so a TSD regenerated mid-run cannot change the contract
under the code agent's feet.

Idempotent + inspector-gated (repo convention — see 0087/0123/0124).
"""
from alembic import op
import sqlalchemy as sa

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("agentic_runs")}
    if "tsd_version_locked" not in cols:
        op.add_column(
            "agentic_runs",
            sa.Column("tsd_version_locked", sa.Integer(), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("agentic_runs")}
    if "tsd_version_locked" in cols:
        with op.batch_alter_table("agentic_runs") as batch_op:
            batch_op.drop_column("tsd_version_locked")
