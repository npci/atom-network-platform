# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add `event_kind` column on `negotiation_messages`.

When a NegotiationMessage acts as the spine for a structured event
(CounterProposal creation / resolution, Blocker creation / resolution),
this column tells the renderer which kind of bubble to draw. NULL on
free-text chat rows. Paired with the FK columns added in 0053.

Values used by the dual-write code:
  - 'proposal'            — NM linked to a newly-created CounterProposal
  - 'resolution'          — NM linked to a CounterProposal that was just resolved
  - 'blocker'             — NM linked to a newly-created Blocker
  - 'blocker_resolution'  — NM linked to a Blocker that was just resolved

Revision ID: 0054
Revises:    0053
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa


revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("negotiation_messages")}
    if "event_kind" not in cols:
        op.add_column(
            "negotiation_messages",
            sa.Column("event_kind", sa.String(20), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("negotiation_messages")}
    if "event_kind" in cols:
        op.drop_column("negotiation_messages", "event_kind")
