# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add docx_path column to product_canvases so the on-demand download endpoint
can cache the assembled DOCX path for the Canvas, same as BRD / Tech Spec / XSD.

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_canvases", sa.Column("docx_path", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("product_canvases", "docx_path")
