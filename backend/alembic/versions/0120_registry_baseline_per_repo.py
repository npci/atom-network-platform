# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""API Registry — one production baseline per repo (was: one overall).

The registry's sources are role-segregated: XSDs live in the core repo while
validation code spans core and app repos, so operators need a production branch
per repo (core AND 2.0), not a single global pick. Replaces 0113's single-row
partial unique index with uniqueness per ``gitlab_repo``.

Idempotent + inspector-gated (safe to re-run).

Revision ID: 0120
Revises: 0119
"""
from alembic import op
import sqlalchemy as sa

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None

_OLD = "uq_code_repos_single_registry_baseline"
_NEW = "uq_code_repos_registry_baseline_per_repo"


def _partial_unique_kwargs(dialect: str) -> dict:
    kwargs = {"unique": True}
    if dialect == "postgresql":
        kwargs["postgresql_where"] = sa.text("is_registry_baseline")
    else:  # sqlite (test harness) also supports partial indexes
        kwargs["sqlite_where"] = sa.text("is_registry_baseline")
    return kwargs


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "code_repos" not in set(insp.get_table_names()):
        return
    names = {ix["name"] for ix in insp.get_indexes("code_repos")}
    if _OLD in names:
        op.drop_index(_OLD, table_name="code_repos")
    if _NEW in names:
        return

    # Defensive: collapse any multi-baseline-per-repo state (possible only on DBs
    # that never ran 0113) so the per-repo unique index can be built.
    op.execute(sa.text(
        "UPDATE code_repos SET is_registry_baseline = FALSE "
        "WHERE is_registry_baseline AND id NOT IN "
        "(SELECT MIN(id) FROM code_repos WHERE is_registry_baseline GROUP BY gitlab_repo)"
    ))
    op.create_index(_NEW, "code_repos", ["gitlab_repo"],
                    **_partial_unique_kwargs(bind.dialect.name))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "code_repos" not in set(insp.get_table_names()):
        return
    names = {ix["name"] for ix in insp.get_indexes("code_repos")}
    if _NEW in names:
        op.drop_index(_NEW, table_name="code_repos")
    if _OLD not in names:
        # Back to a single global baseline: keep one winner, clear the rest.
        op.execute(sa.text(
            "UPDATE code_repos SET is_registry_baseline = FALSE "
            "WHERE id NOT IN (SELECT id FROM code_repos WHERE is_registry_baseline "
            "ORDER BY id LIMIT 1) AND is_registry_baseline"
        ))
        op.create_index(_OLD, "code_repos", ["is_registry_baseline"],
                        **_partial_unique_kwargs(bind.dialect.name))
