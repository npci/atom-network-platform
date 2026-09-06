# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add escalation_tickets.ai_comment_draft — concise submittable review comment.

The reviewer gets both the full AI assessment (ai_suggestion) and a short
review-comment draft (ai_comment_draft) that pre-fills their response box.

Revision ID: 0072
Revises: 0071
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("escalation_tickets")}
    if "ai_comment_draft" not in cols:
        op.add_column("escalation_tickets", sa.Column("ai_comment_draft", sa.Text, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("escalation_tickets")}
    if "ai_comment_draft" in cols:
        op.drop_column("escalation_tickets", "ai_comment_draft")
