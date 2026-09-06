# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""API Registry — enforce a single production baseline row at the DB level.

Partial unique index on ``code_repos(is_registry_baseline) WHERE is_registry_baseline``
so at most one row can be the registry's production source. Backs the advisory-lock
serialization in ``set_production_source`` — a hard guarantee that also holds for
direct DB writes and the SQLite test harness (which has no advisory lock).

Idempotent + inspector-gated (safe to re-run). Collapses any pre-existing multi-row
state to a single winner first, so the index can be created.

Revision ID: 0113
Revises: 0112
"""
from alembic import op
import sqlalchemy as sa

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None

_INDEX = "uq_code_repos_single_registry_baseline"


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "code_repos" not in set(insp.get_table_names()):
        return
    if _INDEX in {ix["name"] for ix in insp.get_indexes("code_repos")}:
        return

    # Defensive: if a prior race left >1 baseline, keep the first, clear the rest,
    # so the unique index can be built.
    op.execute(sa.text(
        "UPDATE code_repos SET is_registry_baseline = FALSE "
        "WHERE id NOT IN (SELECT id FROM code_repos WHERE is_registry_baseline "
        "ORDER BY id LIMIT 1) AND is_registry_baseline"
    ))

    dialect = bind.dialect.name
    kwargs = {"unique": True}
    if dialect == "postgresql":
        kwargs["postgresql_where"] = sa.text("is_registry_baseline")
    else:  # sqlite (test harness) also supports partial indexes
        kwargs["sqlite_where"] = sa.text("is_registry_baseline")
    op.create_index(_INDEX, "code_repos", ["is_registry_baseline"], **kwargs)


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "code_repos" not in set(insp.get_table_names()):
        return
    if _INDEX in {ix["name"] for ix in insp.get_indexes("code_repos")}:
        op.drop_index(_INDEX, table_name="code_repos")
