# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add doc_code_links table for Slice 18 doc↔code linking.

Each row represents a confidence-scored edge from a doc chunk to a code
symbol chunk. Unique `(doc_chunk_id, symbol_chunk_id)` makes the linker
idempotent — re-runs UPDATE rather than duplicate.

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "doc_code_links",
        sa.Column("id",               sa.String(36), primary_key=True),
        sa.Column("doc_chunk_id",     sa.String(36), sa.ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol_chunk_id",  sa.String(36), sa.ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("confidence",       sa.Float(),    nullable=False),
        sa.Column("last_checked",     sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_at",       sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("doc_chunk_id", "symbol_chunk_id", name="uq_doc_code_links_pair"),
    )
    op.create_index("ix_doc_code_links_doc_chunk_id",    "doc_code_links", ["doc_chunk_id"],    unique=False)
    op.create_index("ix_doc_code_links_symbol_chunk_id", "doc_code_links", ["symbol_chunk_id"], unique=False)
    op.create_index("ix_doc_code_links_confidence",      "doc_code_links", ["confidence"],      unique=False)


def downgrade() -> None:
    op.drop_index("ix_doc_code_links_confidence",      table_name="doc_code_links")
    op.drop_index("ix_doc_code_links_symbol_chunk_id", table_name="doc_code_links")
    op.drop_index("ix_doc_code_links_doc_chunk_id",    table_name="doc_code_links")
    op.drop_table("doc_code_links")
