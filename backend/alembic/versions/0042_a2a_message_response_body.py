# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2A message response body capture.

Slice 25 — Admin A2A logs UI. The existing `a2a_messages.payload`
column stores the REQUEST body for both inbound and outbound rows.
Until now we recorded nothing about what came back: response artifacts
were drained and discarded. This migration adds:

  a2a_messages.response_body  JSONB NULL

so every row carries both halves of the round-trip:

  inbound  → payload = partner-sent body, response_body = NPCI's reply
  outbound → payload = NPCI-sent body,    response_body = partner's reply

JSONB on Postgres / JSON variant on SQLite test backends so the
Slice 7-style pattern (single column with `with_variant`) keeps both
test paths working.

Idempotent (inspector-gated) — safe to re-run.

Revision ID: 0042
Revises: 0041
Create Date: 2026-05-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("a2a_messages")}
    if "response_body" not in cols:
        op.add_column(
            "a2a_messages",
            sa.Column(
                "response_body",
                postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("a2a_messages")}
    if "response_body" in cols:
        op.drop_column("a2a_messages", "response_body")
