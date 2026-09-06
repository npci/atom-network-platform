# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add tree-sitter-derived symbol metadata columns to document_chunks.

Slice 3 of platform-enhancement-backlog.md. Columns are nullable so existing
rows (and non-code chunks) are unaffected. Code chunks emitted by the
tree-sitter chunker (behind the USE_TREE_SITTER_CHUNKER flag) will populate
these; the regex chunker continues to leave them NULL.

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("symbol_kind", sa.String(50), nullable=True))
    op.add_column("document_chunks", sa.Column("symbol_name", sa.String(500), nullable=True))
    op.add_column("document_chunks", sa.Column("signature", sa.Text(), nullable=True))
    op.add_column("document_chunks", sa.Column("line_start", sa.Integer(), nullable=True))
    op.add_column("document_chunks", sa.Column("line_end", sa.Integer(), nullable=True))
    op.add_column("document_chunks", sa.Column("language", sa.String(30), nullable=True))

    # Useful lookup for per-language filtering later (Slice 17 LSP work).
    op.create_index(
        "ix_document_chunks_language",
        "document_chunks",
        ["language"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_language", table_name="document_chunks")
    op.drop_column("document_chunks", "language")
    op.drop_column("document_chunks", "line_end")
    op.drop_column("document_chunks", "line_start")
    op.drop_column("document_chunks", "signature")
    op.drop_column("document_chunks", "symbol_name")
    op.drop_column("document_chunks", "symbol_kind")
