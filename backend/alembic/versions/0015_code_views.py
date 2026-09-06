# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add view_kind + parent_symbol_id to document_chunks for 3-view code embeddings.

Slice 4 of platform-enhancement-backlog.md. Plan §4.2.3: three embedding views
per code symbol (signature / body / nl_summary). Views share a parent_symbol_id
so the retriever can dedup when multiple views of the same symbol rank in top-k.

Both columns are nullable — non-code rows and pre-flag rows keep them NULL.

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("view_kind", sa.String(20), nullable=True))
    op.add_column("document_chunks", sa.Column("parent_symbol_id", sa.String(36), nullable=True))
    op.create_index(
        "ix_document_chunks_parent_symbol_id",
        "document_chunks",
        ["parent_symbol_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_parent_symbol_id", table_name="document_chunks")
    op.drop_column("document_chunks", "parent_symbol_id")
    op.drop_column("document_chunks", "view_kind")
