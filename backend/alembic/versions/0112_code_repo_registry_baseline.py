# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""API Registry — designate an indexed repo/branch as the production baseline source.

Adds ``code_repos.is_registry_baseline``: the admin picks which already-indexed
(repo, branch) row is the production source the registry baseline syncs from —
instead of a free-text ref. Selection only; the sync itself is a separate step.

Idempotent + inspector-gated (safe to re-run).

Revision ID: 0112
Revises: 0111
"""
from alembic import op
import sqlalchemy as sa

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "code_repos" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("code_repos")}
    if "is_registry_baseline" not in cols:
        op.add_column(
            "code_repos",
            sa.Column("is_registry_baseline", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
        )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "code_repos" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("code_repos")}
    if "is_registry_baseline" in cols:
        op.drop_column("code_repos", "is_registry_baseline")
