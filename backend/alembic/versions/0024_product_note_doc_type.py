# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add 'product_note' value to productkitdoctype enum.

Adopted from teammate's `doc-generation-wiring` branch (their `0015`).
Renumbered to `0024` to chain after current main's `0023_approvals_reviewer_role`.

Required because the docgen merge introduces a new Product Kit document type
("Product Note") routed through the LangGraph docgen pipeline. The model
constant `ProductKitDocType.PRODUCT_NOTE = "product_note"` is added in the
same commit; this migration registers the value with the underlying Postgres
enum so inserts of `doc_type='product_note'` succeed.

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-30
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres requires ALTER TYPE ... ADD VALUE outside of a transaction block.
    # Alembic runs each migration in a transaction by default, so issue the
    # ALTER inside an explicit autocommit block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE productkitdoctype ADD VALUE IF NOT EXISTS 'product_note'")


def downgrade() -> None:
    # Postgres has no clean way to remove an enum value. Document a manual
    # path if rollback is ever needed.
    #
    #   1. Migrate or delete rows whose doc_type='product_note'
    #   2. CREATE TYPE productkitdoctype_old AS ENUM (... without 'product_note' ...);
    #   3. ALTER TABLE product_kit_documents ALTER COLUMN doc_type
    #      TYPE productkitdoctype_old USING doc_type::text::productkitdoctype_old;
    #   4. DROP TYPE productkitdoctype;
    #   5. ALTER TYPE productkitdoctype_old RENAME TO productkitdoctype;
    pass
