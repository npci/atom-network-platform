# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add escalation_tickets table — route partner queries to Risk/InfoSec/Tech.

One row per (a2a_message, team). Created by the feasibility resolver when it
recommends "escalate"; the team responds from its inbox and the response is
folded back into the PM's partner-reply draft.

Revision ID: 0069
Revises: 0068
Create Date: 2026-06-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "escalation_tickets" in insp.get_table_names():
        return

    op.create_table(
        "escalation_tickets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "change_request_id", sa.String(36),
            sa.ForeignKey("change_requests.id"), nullable=False, index=True,
        ),
        sa.Column(
            "partner_id", sa.String(36),
            sa.ForeignKey("partner_agents.id"), nullable=False,
        ),
        sa.Column(
            "a2a_message_id", sa.String(36),
            sa.ForeignKey("a2a_messages.id"), nullable=True, index=True,
        ),
        sa.Column("cluster_id", sa.String(36), nullable=True, index=True),
        sa.Column("team", sa.String(16), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open", index=True),
        sa.Column("question_text", sa.Text, nullable=False),
        sa.Column("escalation_reason", sa.Text, nullable=True),
        sa.Column("team_response_text", sa.Text, nullable=True),
        sa.Column("responded_by", sa.String(36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "escalation_tickets" in insp.get_table_names():
        op.drop_table("escalation_tickets")
