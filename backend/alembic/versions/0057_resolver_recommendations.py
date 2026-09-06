# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""resolver_recommendations — stores the feasibility resolver's output
per inbound partner message (query or counter-proposal).

Inspector-gated + idempotent .

Revision ID: 0057
Revises:    0056
Create Date: 2026-05-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "resolver_recommendations" not in insp.get_table_names():
        op.create_table(
            "resolver_recommendations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False, index=True),
            sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False),
            sa.Column("a2a_message_id", sa.String(36), sa.ForeignKey("a2a_messages.id"), nullable=False, index=True),
            sa.Column("message_type", sa.String(20), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("model_used", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("resolver_recommendations")
