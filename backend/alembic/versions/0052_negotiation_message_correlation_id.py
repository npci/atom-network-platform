# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Correlation IDs on negotiation_messages — bind a partner query to its
CLARIFICATION_RESPONSE so the response always lands on the originating
OutgoingQuery row, not "the most recent" one.

Adds a nullable `correlation_id varchar(36)` column to
`negotiation_messages`. Populated for `role='partner'` rows when a partner
sends a QUERY / CERT_QUERY whose A2A payload carries `correlation_id`.
The matching `approve_and_respond` route echoes that ID back on the
outbound CLARIFICATION_RESPONSE so the partner-side handler can attach
the response to the exact OutgoingQuery that originated the query.

Nullable on purpose: existing rows pre-date the protocol change, and
inbound messages from older partner builds may legitimately omit it.

Revision ID: 0052
Revises:    0051
Create Date: 2026-05-14
"""
from alembic import op
import sqlalchemy as sa


revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("negotiation_messages")}
    if "correlation_id" not in cols:
        op.add_column(
            "negotiation_messages",
            sa.Column("correlation_id", sa.String(36), nullable=True),
        )
        op.create_index(
            "ix_negotiation_messages_correlation_id",
            "negotiation_messages",
            ["correlation_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("negotiation_messages")}
    if "correlation_id" in cols:
        op.drop_index(
            "ix_negotiation_messages_correlation_id",
            table_name="negotiation_messages",
        )
        op.drop_column("negotiation_messages", "correlation_id")
