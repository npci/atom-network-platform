# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add docx_path columns so generated .docx files can be linked.

One nullable column per artefact table. Populated after DOCX assembly runs;
NULL means "no DOCX yet" (legacy rows or generation still in progress).

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_TABLES = ("brds", "tech_specs", "xsds", "product_kit_documents")


def upgrade() -> None:
    for t in _TABLES:
        op.add_column(t, sa.Column("docx_path", sa.String(500), nullable=True))


def downgrade() -> None:
    for t in _TABLES:
        op.drop_column(t, "docx_path")
