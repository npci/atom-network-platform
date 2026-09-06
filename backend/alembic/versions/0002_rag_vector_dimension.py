# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""RAG — resize embedding vector to 384 (all-MiniLM-L6-v2) and add HNSW index

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-12
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop existing ivfflat index (created in 0001)
    op.drop_index("ix_document_chunks_embedding", table_name="document_chunks")

    # Resize vector column: 1536 → 384 (sentence-transformers/all-MiniLM-L6-v2)
    op.execute(
        "ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(384) "
        "USING NULL::vector(384)"
    )

    # Re-create index as HNSW (better for small-to-medium datasets, no training needed)
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_embedding", table_name="document_chunks")
    op.execute(
        "ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(1536) "
        "USING NULL::vector(1536)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding "
        "ON document_chunks USING ivfflat (embedding vector_cosine_ops)"
    )
