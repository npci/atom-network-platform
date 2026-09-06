# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 4.1 — Track the last successfully-ingested commit SHA per repo.

The existing `code_repo_file_state` table records hashes of file content,
which is the source of truth for "what is currently indexed". The new
`code_repo_state` table records the last GitLab commit SHA that produced
that state, so Phase 4.2 can ask GitLab Compare API "what changed since
this SHA?" instead of re-fetching every file.

Single row per (repo_id), PK gives idempotent upsert.

Revision ID: 0065
Revises: 0064

Renumbered to 0065 during the retrofit onto current main (chains after
0064_embedding_cache).

Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Inspector-gated so the migration is safe to re-run against an
    # already-migrated DB , matching the sibling 0064/0066.
    insp = sa.inspect(op.get_bind())
    if "code_repo_state" not in insp.get_table_names():
        op.create_table(
            "code_repo_state",
            sa.Column("repo_id",            sa.String(36), primary_key=True),
            sa.Column("last_ingested_sha",  sa.String(64), nullable=True),
            sa.Column("last_ingested_at",   sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_ingested_branch", sa.String(255), nullable=True),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "code_repo_state" in insp.get_table_names():
        op.drop_table("code_repo_state")
