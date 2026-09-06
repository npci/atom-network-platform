# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add escalation_tickets.ai_suggestion — show the AI draft to the reviewer.

Lets the escalated team member see the resolver's drafted response and either
adopt it or write their own input.

Revision ID: 0071
Revises: 0070
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("escalation_tickets")}
    if "ai_suggestion" not in cols:
        op.add_column("escalation_tickets", sa.Column("ai_suggestion", sa.Text, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("escalation_tickets")}
    if "ai_suggestion" in cols:
        op.drop_column("escalation_tickets", "ai_suggestion")
