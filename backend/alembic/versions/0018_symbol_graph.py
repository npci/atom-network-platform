# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add symbol-graph edge columns to document_chunks for Slice 17.

Columns (all nullable — populated only by the Java symbol-graph extractor
when USE_SYMBOL_GRAPH_EXTRACTOR is on; left NULL for docs, non-code rows,
and non-Java code):

  - imports       JSON list of imported fully-qualified names
  - inherits      String(500) — single superclass name (Java 'extends')
  - implements    JSON list of interface names
  - calls         JSON list of method names invoked (method chunks only)
  - called_by     JSON list of method names within the same file that
                  invoke this method (cross-file called_by requires a
                  global symbol index — follow-up)

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("imports",    sa.JSON(),      nullable=True))
    op.add_column("document_chunks", sa.Column("inherits",   sa.String(500), nullable=True))
    op.add_column("document_chunks", sa.Column("implements", sa.JSON(),      nullable=True))
    op.add_column("document_chunks", sa.Column("calls",      sa.JSON(),      nullable=True))
    op.add_column("document_chunks", sa.Column("called_by",  sa.JSON(),      nullable=True))


def downgrade() -> None:
    op.drop_column("document_chunks", "called_by")
    op.drop_column("document_chunks", "calls")
    op.drop_column("document_chunks", "implements")
    op.drop_column("document_chunks", "inherits")
    op.drop_column("document_chunks", "imports")
