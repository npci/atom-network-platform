# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Enable Apache AGE extension and create the `npci_kg` named graph.

Slice 19 — depends on the custom Postgres image (docker/postgres/Dockerfile)
that has AGE compiled + installed. Running this migration against the
upstream pgvector image will fail with `extension "age" is not available`
— that's the intended signal to build the custom image first.

Idempotency:
  - `CREATE EXTENSION IF NOT EXISTS age;` skips if already installed.
  - `create_graph('npci_kg')` raises if the graph exists, so we guard with
    an existence check on `ag_catalog.ag_graph`.

No node/edge labels are created here — that's the runtime job of
`app.kg.schema.initialise_graph(db)`.

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-24
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


GRAPH_NAME = "npci_kg"


def upgrade() -> None:
    # 1) Enable the extension (idempotent).
    op.execute("CREATE EXTENSION IF NOT EXISTS age;")

    # 2) Load the AGE shared library into this session. `LOAD 'age'` is a
    #    per-session directive; the subsequent statement needs it to resolve
    #    AGE functions like `create_graph`.
    op.execute("LOAD 'age';")

    # 3) Put ag_catalog on the search path so unqualified AGE function names
    #    resolve without schema-qualification.
    op.execute('SET search_path = ag_catalog, "$user", public;')

    # 4) Create the named graph only if it doesn't already exist. Wrapped in
    #    a DO block so re-running is a no-op.
    op.execute(f"""
        DO $do$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM ag_catalog.ag_graph WHERE name = '{GRAPH_NAME}'
            ) THEN
                PERFORM create_graph('{GRAPH_NAME}');
            END IF;
        END
        $do$;
    """)

    # Keep later migrations from accidentally creating application tables in
    # ag_catalog after AGE function setup.
    op.execute('SET search_path = "$user", public;')


def downgrade() -> None:
    # Drop the graph first (data loss warning — users must accept this).
    op.execute("LOAD 'age';")
    op.execute('SET search_path = ag_catalog, "$user", public;')
    op.execute(f"""
        DO $do$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM ag_catalog.ag_graph WHERE name = '{GRAPH_NAME}'
            ) THEN
                PERFORM drop_graph('{GRAPH_NAME}', true);
            END IF;
        END
        $do$;
    """)
    op.execute('SET search_path = "$user", public;')
    # Drop the extension itself. Requires no other schemas to depend on it.
    op.execute("DROP EXTENSION IF EXISTS age;")
