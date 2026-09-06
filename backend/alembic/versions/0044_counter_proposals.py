# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Counter-proposal table — Tier 1 of the message-intent split.

Promotes COUNTER_PROPOSAL from a payload-flag-on-QUERY to a first-class
workflow with its own table + state machine. Open counters gate
assignment transitions: assignment.status can't progress past ACCEPTED
while any counter is OPEN.

Schema:
  counter_proposals:
    id                   uuid pk
    change_request_id    uuid fk
    partner_id           uuid fk
    assignment_id        uuid fk
    counter_proposal_id  varchar(64)        partner-supplied id
    status               enum               open / accepted / rejected / withdrawn
    negotiation_round    int default 1
    justification        text
    valid_until          timestamptz nullable
    payload              jsonb nullable     full inbound payload for audit
    created_at           timestamptz
    resolved_at          timestamptz nullable
    resolved_by          uuid fk users.id nullable
    resolution_text      text nullable

Indexed on (change_request_id, partner_id) for the dashboard's
"open counters per partner" query.

Inspector-gated. JSONB on Postgres / JSON on SQLite.

Revision ID: 0044
Revises: 0043
Create Date: 2026-05-10
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


_STATUS_ENUM = sa.Enum(
    "open", "accepted", "rejected", "withdrawn",
    name="counterproposalstatus",
    create_type=True,
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "counter_proposals" in insp.get_table_names():
        return

    op.create_table(
        "counter_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False, index=True),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False, index=True),
        sa.Column("assignment_id", sa.String(36), sa.ForeignKey("change_partner_assignments.id"), nullable=False, index=True),
        sa.Column("counter_proposal_id", sa.String(64), nullable=False),
        sa.Column("status", _STATUS_ENUM, nullable=False, server_default="open"),
        sa.Column("negotiation_round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolution_text", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "counter_proposals" in insp.get_table_names():
        op.drop_table("counter_proposals")
    # Drop the enum type only if nothing else references it
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS counterproposalstatus")
