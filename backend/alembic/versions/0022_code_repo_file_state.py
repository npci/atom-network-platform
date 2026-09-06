# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add `code_repo_file_state` table for incremental ingest (Slice 26).

Stores the SHA256 of each ingested file at last successful run, keyed by
(repo_id, source_file). Polyglot-incremental ingest reads this on every
invocation, diffs against the live file set, and only re-processes
files whose hash changed.

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_repo_file_state",
        sa.Column("id",              sa.String(36), primary_key=True),
        sa.Column("repo_id",         sa.String(36), nullable=False, index=True),
        sa.Column("source_file",     sa.String(1000), nullable=False),
        sa.Column("content_hash",    sa.String(64), nullable=False),
        sa.Column("language",        sa.String(30), nullable=True),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_at",      sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("repo_id", "source_file",
                            name="uq_code_repo_file_state_pair"),
    )
    # Note: `index=True` on the repo_id column already creates the index
    # `ix_code_repo_file_state_repo_id`; no explicit `create_index` needed.


def downgrade() -> None:
    op.drop_table("code_repo_file_state")
