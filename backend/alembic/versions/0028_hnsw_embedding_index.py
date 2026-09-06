# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1.1 — HNSW index on document_chunks.embedding.

The dense-search SQL in `app.rag.hybrid_search._dense_search` runs
`ORDER BY embedding <=> query_vector LIMIT N` against `document_chunks`.
Without an explicit pgvector index this is a sequential scan over the
entire table. At ≤ 10k chunks that's fine; at ≥ 100k it dominates
retrieval latency.

This migration creates an HNSW index using the cosine-distance operator
class — matching the `<=>` operator used in retrieval. HNSW is the
modern default for pgvector (≥ 0.5.0); approximation error is negligible
for normalised embeddings.

Tunables:
  - `m`               = 16  (default; raise for higher recall, slower build)
  - `ef_construction` = 64  (default; raise for better build quality)
  - `ef_search`       = set per-query in retrieval (default 80)

The build is wrapped in IF NOT EXISTS so re-runs are idempotent. We use
CONCURRENTLY so the migration can run on a live database without
blocking writes — but Alembic disables that within a transaction by
default, so we explicitly run autocommit.

Revision ID: 0028
Revises: 0027   (phase_b_run_repos)
Create Date: 2026-05-06

Renumbered from 0026 → 0028 because slot 0026 was already taken by
0026_approval_approver_nullable in this branch (and 0027 by phase_b_run_repos).
The DB on Ubuntu is currently stamped at "0027"; this migration chains
after phase_b_run_repos. See merge plan Step 12.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


# IF NOT EXISTS makes both the create and drop idempotent so the migration
# can be safely re-run on environments that may have already applied it
# manually (production hot-fix path).
INDEX_NAME = "idx_document_chunks_embedding_hnsw"


def upgrade() -> None:
    # The CONCURRENTLY clause requires running outside a transaction.
    # We don't want to block writes on a live DB during the build.
    bind = op.get_bind()
    # Detect dialect — HNSW is Postgres-only via pgvector.
    if bind.dialect.name != "postgresql":
        return

    # Re-establish autocommit only for this DDL (same trick used in 0020).
    raw = bind.execute(sa.text("SHOW transaction_isolation")).scalar()
    # CREATE INDEX CONCURRENTLY can't run inside a transaction. Alembic's
    # default migration_context wraps everything in one. The conventional
    # workaround is to commit and emit raw SQL via the connection in
    # autocommit mode.
    conn = op.get_bind()
    conn.execute(sa.text("COMMIT"))
    try:
        conn.execute(sa.text(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            f"ON document_chunks USING hnsw (embedding vector_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64)"
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
