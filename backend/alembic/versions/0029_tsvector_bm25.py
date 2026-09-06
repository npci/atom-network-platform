# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1.2 — `content_tsv` generated column + GIN index.

Replaces the per-process in-memory `rank_bm25.BM25Okapi` index with a
Postgres-native `tsvector` GIN index. Benefits:

  - Per-worker memory usage drops (BM25Okapi keeps the corpus in RAM).
  - Index updates are incremental — no full rebuild after each ingest.
  - Sub-100 ms latency at any corpus size.

Use `ts_rank_cd(content_tsv, plainto_tsquery('english', :q))` in queries.

Revision ID: 0029
Revises: 0028   (hnsw_embedding_index)
Create Date: 2026-05-06

Renumbered from 0027 → 0029 (chains after the renumbered HNSW migration).
See merge plan Step 12.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


COLUMN_NAME = "content_tsv"
INDEX_NAME = "idx_document_chunks_content_tsv"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Generated stored column — Postgres maintains it on every INSERT/UPDATE
    # so the BM25 plumbing can be deleted without losing index freshness.
    # Use IF NOT EXISTS so re-runs are idempotent.
    op.execute(sa.text(f"""
        ALTER TABLE document_chunks
            ADD COLUMN IF NOT EXISTS {COLUMN_NAME} tsvector
            GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED
    """))

    # GIN index on the new column. CONCURRENTLY so the migration doesn't
    # block writes on a live table. Same autocommit dance as 0026.
    conn = op.get_bind()
    conn.execute(sa.text("COMMIT"))
    try:
        conn.execute(sa.text(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            f"ON document_chunks USING gin({COLUMN_NAME})"
        ))
    finally:
        conn.execute(sa.text("BEGIN"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    conn = op.get_bind()
    conn.execute(sa.text("COMMIT"))
    try:
        conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))
    finally:
        conn.execute(sa.text("BEGIN"))

    op.execute(sa.text(f"ALTER TABLE document_chunks DROP COLUMN IF EXISTS {COLUMN_NAME}"))
