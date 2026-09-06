# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add negotiation freeze + emergency_issues (Slice 5).

- change_requests.negotiation_frozen_at: set when the final kit (v3) ships;
  the executor then rejects inbound queries / counter-proposals.
- emergency_issues: the only partner→NPCI channel after a change is frozen.

Revision ID: 0070
Revises: 0069
Create Date: 2026-06-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    cols = {c["name"] for c in insp.get_columns("change_requests")}
    if "negotiation_frozen_at" not in cols:
        op.add_column(
            "change_requests",
            sa.Column("negotiation_frozen_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "emergency_issues" not in insp.get_table_names():
        op.create_table(
            "emergency_issues",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "change_request_id", sa.String(36),
                sa.ForeignKey("change_requests.id"), nullable=False, index=True,
            ),
            sa.Column(
                "partner_id", sa.String(36),
                sa.ForeignKey("partner_agents.id"), nullable=False, index=True,
            ),
            sa.Column("issue_id", sa.String(64), nullable=True),
            sa.Column("severity", sa.String(16), nullable=False, server_default="critical"),
            sa.Column("status", sa.String(16), nullable=False, server_default="open", index=True),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("description", sa.Text, nullable=False),
            sa.Column("npci_resolution_text", sa.Text, nullable=True),
            sa.Column("resolved_by", sa.String(36), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.text("now()"),
            ),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "emergency_issues" in insp.get_table_names():
        op.drop_table("emergency_issues")
    cols = {c["name"] for c in insp.get_columns("change_requests")}
    if "negotiation_frozen_at" in cols:
        op.drop_column("change_requests", "negotiation_frozen_at")
