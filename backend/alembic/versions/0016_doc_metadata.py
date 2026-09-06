# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add doc metadata + hierarchical-chunking fields to document_chunks.

Slice 7 of platform-enhancement-backlog.md. All columns nullable — existing
rows, non-markdown rows, and code rows keep them NULL.

Columns added:
  - title_breadcrumb   "Payments > Retry Logic > Rate Limits" (heading path)
  - last_modified      file mtime or explicit metadata
  - author             document author (when available)
  - product_area       broader taxonomy (cross-cuts doc_category)
  - freshness_score    decay function of last_modified (future slice)
  - deprecated         explicit flag — filtered out of retrieval by default
  - parent_chunk_id    link from child paragraph → parent section chunk
                       (NULL on parent + pre-Slice-7 rows)

Revision ID: 0016
Revises: 0015
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("title_breadcrumb", sa.String(1000), nullable=True))
    op.add_column("document_chunks", sa.Column("last_modified",   sa.DateTime(timezone=True), nullable=True))
    op.add_column("document_chunks", sa.Column("author",           sa.String(200), nullable=True))
    op.add_column("document_chunks", sa.Column("product_area",     sa.String(100), nullable=True))
    op.add_column("document_chunks", sa.Column("freshness_score",  sa.Float(), nullable=True))
    op.add_column("document_chunks", sa.Column("deprecated",       sa.Boolean(), nullable=True))
    op.add_column("document_chunks", sa.Column("parent_chunk_id",  sa.String(36), nullable=True))

    op.create_index(
        "ix_document_chunks_parent_chunk_id",
        "document_chunks",
        ["parent_chunk_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_parent_chunk_id", table_name="document_chunks")
    op.drop_column("document_chunks", "parent_chunk_id")
    op.drop_column("document_chunks", "deprecated")
    op.drop_column("document_chunks", "freshness_score")
    op.drop_column("document_chunks", "product_area")
    op.drop_column("document_chunks", "author")
    op.drop_column("document_chunks", "last_modified")
    op.drop_column("document_chunks", "title_breadcrumb")
