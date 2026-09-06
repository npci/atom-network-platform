# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add key rotation grace-period columns to partner_agents.

Supports a 5-minute overlap window during secret rotation so in-flight
requests signed with the previous key are not rejected.

Idempotent + inspector-gated  .

Revision ID: 0084
Revises: 0083
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("partner_agents")}

    if "previous_jwt_signing_secret" not in cols:
        op.add_column(
            "partner_agents",
            sa.Column("previous_jwt_signing_secret", sa.String(128), nullable=True),
        )
    if "previous_signing_secret" not in cols:
        op.add_column(
            "partner_agents",
            sa.Column("previous_signing_secret", sa.String(128), nullable=True),
        )
    if "secret_rotated_at" not in cols:
        op.add_column(
            "partner_agents",
            sa.Column("secret_rotated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade():
    op.drop_column("partner_agents", "secret_rotated_at")
    op.drop_column("partner_agents", "previous_signing_secret")
    op.drop_column("partner_agents", "previous_jwt_signing_secret")
