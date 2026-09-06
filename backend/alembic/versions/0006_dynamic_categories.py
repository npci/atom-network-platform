# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Change doc_category from enum to varchar for user-defined categories.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Convert enum column to varchar — preserves existing data
    op.execute(
        "ALTER TABLE document_chunks ALTER COLUMN doc_category TYPE varchar(100) "
        "USING doc_category::text"
    )
    # Drop the old enum type
    op.execute("DROP TYPE IF EXISTS doccategory")


def downgrade() -> None:
    # Recreate enum and convert back
    op.execute(
        "CREATE TYPE doccategory AS ENUM "
        "('rbi_guideline','upi_product_doc','past_brd','api_spec','xsd','java_source')"
    )
    op.execute(
        "ALTER TABLE document_chunks ALTER COLUMN doc_category TYPE doccategory "
        "USING doc_category::doccategory"
    )
