# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add `cross_file_calls` JSON column to document_chunks (Slice 23).

Each row entry takes shape:
    [
      {
        "callee_symbol": "do_thing",
        "callee_path":   "src/utils/helpers.py",
        "line":          42,
        "language":      "python"
      },
      ...
    ]

Populated only when `settings.use_python_lsp` is True (or, in a follow-up
Slice 24, by the TypeScript LSP). Nullable for every existing row +
every chunk where LSP resolution finds no cross-file targets.

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column("cross_file_calls", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_chunks", "cross_file_calls")
