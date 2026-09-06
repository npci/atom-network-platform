# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 8.2 — `simple_code` text-search configuration.

The existing `content_tsv` column (Phase 1.2 / migration 0029) uses the
`'english'` config which stems aggressively (`'tokens'`, `'tokenize'`,
`'tokenized'` → `'token'`) and drops common English stopwords that are
also valid programming-language tokens (`'is'`, `'as'`, `'in'`, `'do'`).

For prose-heavy doc chunks this is the right behaviour. For code chunks
it costs recall — `'tokenize'` matches `'tokens'`, and a query containing
`'in'` matches against the column where `'in'` was dropped at index
time.

This migration installs a `simple_code` text-search configuration that
is a copy of `pg_catalog.simple` — words are lowercased and split on
non-alphanumerics, but NOT stemmed and NOT filtered against an English
stoplist. The dense `_dense_search` path can be switched to this config
via `settings.bm25_text_search_config = "simple_code"` (a hot-switch with
no rebuild — Phase 1.2's GIN index serves any tsvector regardless of
the per-query config).

Pure DDL — no data migration, no column rewrite.

Revision ID: 0066
Revises: 0065

Renumbered to 0066 during the retrofit onto current main (chains after
0065_code_repo_state).

Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # IF NOT EXISTS isn't a CREATE TEXT SEARCH CONFIGURATION clause in
    # Postgres. Use a DO block so re-runs on already-applied environments
    # don't fail.
    op.execute(sa.text(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_ts_config c
                JOIN pg_namespace n ON n.oid = c.cfgnamespace
                WHERE c.cfgname = 'simple_code'
                  AND n.nspname = 'public'
            ) THEN
                CREATE TEXT SEARCH CONFIGURATION public.simple_code
                    (COPY = pg_catalog.simple);
            END IF;
        END
        $$;
        """
    ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("DROP TEXT SEARCH CONFIGURATION IF EXISTS public.simple_code"))
