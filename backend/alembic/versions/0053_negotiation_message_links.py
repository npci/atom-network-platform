# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Schema groundwork for the unified-conversation model.

Adds two nullable FK columns on `negotiation_messages` so a single
chat row can optionally point back at the structured record it
represents — a `counter_proposals` row or a `blockers` row. Used by
Step 2 of the model proposal: collapse the "synthetic counter event"
rendering path so the timeline is just a stream of NegotiationMessage
rows with optional structured payloads attached.

Pure additive. No behavior change on its own — dual-write logic that
populates these columns lands in a follow-up commit, and the synthetic
event emission in `get_negotiation_thread` stays untouched until the
backfill is done.

Revision ID: 0053
Revises:    0052
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa


revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("negotiation_messages")}

    if "counter_proposal_id" not in cols:
        op.add_column(
            "negotiation_messages",
            sa.Column(
                "counter_proposal_id",
                sa.String(36),
                sa.ForeignKey("counter_proposals.id"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_negotiation_messages_counter_proposal_id",
            "negotiation_messages",
            ["counter_proposal_id"],
        )

    if "blocker_id" not in cols:
        op.add_column(
            "negotiation_messages",
            sa.Column(
                "blocker_id",
                sa.String(36),
                sa.ForeignKey("blockers.id"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_negotiation_messages_blocker_id",
            "negotiation_messages",
            ["blocker_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("negotiation_messages")}

    if "blocker_id" in cols:
        op.drop_index(
            "ix_negotiation_messages_blocker_id",
            table_name="negotiation_messages",
        )
        op.drop_column("negotiation_messages", "blocker_id")

    if "counter_proposal_id" in cols:
        op.drop_index(
            "ix_negotiation_messages_counter_proposal_id",
            table_name="negotiation_messages",
        )
        op.drop_column("negotiation_messages", "counter_proposal_id")
