# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Change partner_type from enum to JSON array for multi-type support.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Convert existing single enum values to JSON arrays
    # e.g. 'bank' → '["bank"]'
    op.execute("""
        ALTER TABLE partner_agents
        ALTER COLUMN partner_type TYPE jsonb
        USING jsonb_build_array(partner_type::text)
    """)


def downgrade() -> None:
    # Convert back: take first element of array
    op.execute("""
        ALTER TABLE partner_agents
        ALTER COLUMN partner_type TYPE varchar(20)
        USING (partner_type->>0)::varchar(20)
    """)
