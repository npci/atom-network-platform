# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Document provenance — Generate-or-Upload.

Adds provenance columns to every Phase-A artifact table so a user-uploaded
document can substitute the generated one for all downstream contextual use:

    source            documentsource NOT NULL DEFAULT 'generated'
    original_filename VARCHAR(500)   NULL
    uploaded_by       VARCHAR(36)    NULL   FK -> users.id (ON DELETE SET NULL), like approved_by
    uploaded_at       TIMESTAMPTZ    NULL

Tables: brds, tech_specs, xsds, product_canvases, product_kit_documents.
Also backfills `file_path` on product_canvases (it lacked one) so the
original uploaded file can be persisted uniformly with the other artifacts.

Idempotent (inspector-gated) so re-runs against an already-migrated DB are
safe. Postgres gets a native enum; SQLite (test harness) gets plain String.

Revision ID: 0061
Revises: 0060
Create Date: 2026-06-03

Renumbered from 0056 to 0061 on retrofit: the branch was cut from old main
and 0056 collided with 0056_npci_policy. This migration is position-independent
(inspector-gated, column-disjoint from 0056-0060), so it simply chains after
the current head 0060_kit_publications.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

_TABLES = ("brds", "tech_specs", "xsds", "product_canvases", "product_kit_documents")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='documentsource') THEN "
            "CREATE TYPE documentsource AS ENUM ('generated','uploaded'); END IF; END $$;"
        )
        source_type = postgresql.ENUM("generated", "uploaded", name="documentsource", create_type=False)
    else:
        source_type = sa.String(20)

    # product_canvases historically lacked a file_path; add it so uploaded
    # originals can be persisted the same way as the other artifacts.
    pc_cols = {c["name"] for c in insp.get_columns("product_canvases")}
    if "file_path" not in pc_cols:
        op.add_column("product_canvases", sa.Column("file_path", sa.String(1000), nullable=True))

    for table in _TABLES:
        cols = {c["name"] for c in insp.get_columns(table)}
        if "source" not in cols:
            op.add_column(
                table,
                sa.Column("source", source_type, nullable=False, server_default="generated"),
            )
        if "original_filename" not in cols:
            op.add_column(table, sa.Column("original_filename", sa.String(500), nullable=True))
        if "uploaded_by" not in cols:
            op.add_column(table, sa.Column("uploaded_by", sa.String(36), nullable=True))
        if "uploaded_at" not in cols:
            op.add_column(table, sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True))

        # uploaded_by → users.id FK (matches the ORM and the sibling
        # `approved_by` columns). Postgres only; idempotent via inspector.
        if is_pg:
            fk_name = f"{table}_uploaded_by_fkey"
            existing_fks = {fk.get("name") for fk in insp.get_foreign_keys(table)}
            if fk_name not in existing_fks:
                op.create_foreign_key(
                    fk_name, table, "users", ["uploaded_by"], ["id"], ondelete="SET NULL",
                )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    is_pg = bind.dialect.name == "postgresql"

    for table in _TABLES:
        if is_pg:
            fk_name = f"{table}_uploaded_by_fkey"
            existing_fks = {fk.get("name") for fk in insp.get_foreign_keys(table)}
            if fk_name in existing_fks:
                op.drop_constraint(fk_name, table, type_="foreignkey")
        cols = {c["name"] for c in insp.get_columns(table)}
        for col in ("uploaded_at", "uploaded_by", "original_filename", "source"):
            if col in cols:
                op.drop_column(table, col)

    pc_cols = {c["name"] for c in insp.get_columns("product_canvases")}
    if "file_path" in pc_cols:
        op.drop_column("product_canvases", "file_path")

    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS documentsource;")
