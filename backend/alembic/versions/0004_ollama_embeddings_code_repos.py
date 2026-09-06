# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Switch embeddings to Ollama nomic-embed-text (768-dim) and add code_repos table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Resize vector column: 384 → 768 (nomic-embed-text via Ollama)
    #    Drop existing chunks since dimension changed — they need re-embedding
    op.execute("DELETE FROM document_chunks")
    op.drop_index("ix_document_chunks_embedding", table_name="document_chunks")
    op.execute(
        "ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(768) "
        "USING NULL::vector(768)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    # 2. Create code_repos table
    op.create_table(
        "code_repos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("gitlab_url", sa.String(500), nullable=True),
        sa.Column("gitlab_repo", sa.String(500), nullable=False),
        sa.Column("gitlab_branch", sa.String(200), nullable=False, server_default="main"),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("files_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("chunks_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("code_repos")

    op.execute("DELETE FROM document_chunks")
    op.drop_index("ix_document_chunks_embedding", table_name="document_chunks")
    op.execute(
        "ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(384) "
        "USING NULL::vector(384)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )
