# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 3.2 — content-hash keyed embedding cache.

Caches embedding vectors keyed on (content_sha256, model, view_kind) so a
re-ingest of unchanged chunks reuses the previously-computed vector instead
of paying the Ollama HTTP round-trip again.

Why a separate table rather than reusing document_chunks:
  - cache survives `DELETE FROM document_chunks WHERE repo_id=…` (the
    indexed-content lifecycle is independent of the embedding lifecycle).
  - cross-repo deduplication: two repos with identical files share the
    cached vector.
  - cross-view-kind hits: the same content_sha256 can appear under
    different view_kinds (body, signature, nl_summary, window:N); the
    composite PK keeps them distinct without colliding.

Schema:
    content_sha256  CHAR(64)       hex digest of utf-8 content
    model           TEXT           e.g. 'nomic-embed-text'
    view_kind       TEXT           '' for default, else multiview/window suffix
    embedding       VECTOR(768)    same dim as document_chunks.embedding
    created_at      TIMESTAMPTZ    diagnostics; can be used for LRU eviction
                                   later if the cache grows unbounded.
    PRIMARY KEY (content_sha256, model, view_kind)

No HNSW index — this table is looked up by exact PK, never scanned.

Revision ID: 0064
Revises: 0063

Renumbered to 0064 during the retrofit onto current main: the uat branch had
this at 0052 (and renumbered the negotiation migrations to 0056-0059 to make
room), but current main occupies 0052-0063. The code-indexing chain now lands
after main's head 0063_eval_policy_audit; the negotiation renames were dropped
(main already has them at 0052-0055). Self-contained table, no cross-deps.

Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


TABLE_NAME = "embedding_cache"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # pgvector is Postgres-only; on other dialects this migration is a no-op
        # so dev SQLite environments still work.
        return

    # The pgvector extension is already enabled by an earlier migration; we just
    # reuse the Vector type via raw SQL so we don't have to import it here.
    op.execute(sa.text(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            content_sha256 CHAR(64)    NOT NULL,
            model          TEXT        NOT NULL,
            view_kind      TEXT        NOT NULL DEFAULT '',
            embedding      vector(768) NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (content_sha256, model, view_kind)
        )
        """
    ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(f"DROP TABLE IF EXISTS {TABLE_NAME}"))
