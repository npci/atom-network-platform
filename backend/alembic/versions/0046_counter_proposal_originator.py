# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Counter-proposal multi-round negotiation — Tier 1.5.

Adds two things to counter_proposals to support PM counter-back:

  originator   varchar(20) NOT NULL DEFAULT 'partner'
                Which side originated this proposal — 'partner' (the
                existing case) or 'npci' (PM countered back). Each
                negotiation round alternates.

  status enum gains a COUNTERED_BACK value
                Means: the receiver responded with their own counter,
                so this row is no longer the "live" one — the new
                opposite-originator row with status='open' is.

The state machine becomes:
  partner sends counter → row(orig=partner, status=open)
  PM accepts          → row.status = accepted, negotiation closes
  PM rejects          → row.status = rejected, negotiation closes
                        (partner can re-counter up to MAX_NEGOTIATION_ROUNDS)
  PM counters back    → row.status = countered_back
                        new row(orig=npci, status=open) created
                        partner can: Accept (PROPOSAL_ACCEPTANCE),
                                     Counter again (new partner row),
                                     or do nothing (terms expire at valid_until)

`is_final_offer` deliberately not modeled (per user instruction).

Revision ID: 0046
Revises: 0045
Create Date: 2026-05-10
"""
from alembic import op
import sqlalchemy as sa


revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # 1) Add the originator column if missing.
    cols = {c["name"] for c in insp.get_columns("counter_proposals")}
    if "originator" not in cols:
        op.add_column(
            "counter_proposals",
            sa.Column("originator", sa.String(20), nullable=False, server_default="partner"),
        )

    # 2) Add COUNTERED_BACK to the enum. Postgres requires this outside
    # the transactional DDL block; alembic provides autocommit_block().
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE counterproposalstatus ADD VALUE IF NOT EXISTS 'countered_back'"
            )


def downgrade() -> None:
    # Postgres doesn't support removing enum values cleanly. We drop
    # the column but leave the enum value in place — harmless if no
    # rows reference it.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("counter_proposals")}
    if "originator" in cols:
        op.drop_column("counter_proposals", "originator")
