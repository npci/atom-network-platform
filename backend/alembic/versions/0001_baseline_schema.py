"""Baseline schema — the single migration this repo starts from.

Revision ID: 0001
Revises: None
Create Date: 2026-09-08

This collapses the 135 historical revisions that built the schema between the
initial commit and revision 0141. Those files are gone; this one reproduces
their end state in a single step.

WHAT THE SIDECAR CONTAINS. `0001_baseline_schema.sql` is the complete schema,
captured with pg_dump from a database built by applying all 135 migrations —
the real end state, not a re-derivation from `Base.metadata`. That distinction
is the whole point: the metadata is missing the HNSW index on
`document_chunks.embedding` (raw SQL, and it went ivfflat -> hnsw partway
through the history), the partial index on `a2a_messages.next_retry_at`, and
the `vector` extension itself. A model-derived baseline would start clean and
silently sequential-scan every similarity search. The dump excludes
`alembic_version` and the AGE graph schema; it holds 97 tables, 128 indexes,
33 types and 241 constraints.

VERIFIED. A database built from this baseline alone was dumped and diffed
against `baseline_ref` — a database built by applying all 135 migrations in
order. The two are identical apart from pg_dump's per-dump `\\restrict` nonce.

EXISTING DATABASES must be stamped, not upgraded. Any database that predates
the collapse already carries this schema under a revision id that no longer
exists in the tree, so alembic cannot place it in the chain:

    alembic stamp 0001

Running `alembic upgrade head` against one instead would try to recreate all
97 tables and fail.
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

from app.core.config import settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_DDL_PATH = Path(__file__).with_suffix(".sql")


def upgrade() -> None:
    # A graph name is interpolated into DDL, so constrain it to a bare
    # identifier — it cannot be a bind parameter.
    graph_name = settings.kg_graph_name
    if not graph_name.replace("_", "").isalnum():
        raise ValueError(
            f"KG_GRAPH_NAME must be a bare identifier, got {graph_name!r}"
        )

    op.execute(_DDL_PATH.read_text())

    # Apache AGE, mirroring the historical setup: enable the extension, LOAD it
    # into this session so `create_graph` resolves, create the graph only if it
    # is absent, then restore the search path so nothing lands in ag_catalog.
    #
    # The graph is NOT part of the captured DDL on purpose. It is a named schema
    # registered in `ag_catalog.ag_graph`; replaying a dumped copy of its tables
    # yields something that looks like a graph and is not a registered one.
    # Creating it from the setting also means each deployment gets ITS graph
    # name, not whichever one the dump happened to be taken under.
    op.execute("CREATE EXTENSION IF NOT EXISTS age;")
    op.execute("LOAD 'age';")
    op.execute('SET search_path = ag_catalog, "$user", public;')
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM ag_catalog.ag_graph WHERE name = '{graph_name}'
            ) THEN
                PERFORM create_graph('{graph_name}');
            END IF;
        END
        $do$;
        """
    )
    op.execute('SET search_path = "$user", public;')


def downgrade() -> None:
    # There is nothing below a baseline to downgrade to. An empty body here
    # would let `alembic downgrade base` report success while leaving all 97
    # tables in place, so refuse instead.
    raise NotImplementedError(
        "0001 is the baseline revision; it cannot be downgraded. "
        "Drop and recreate the database instead."
    )
