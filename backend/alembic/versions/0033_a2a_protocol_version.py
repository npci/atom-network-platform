# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-partner A2A protocol switch + Task lifecycle columns.

Slice 5 of the unified A2A SDK refactor. Adds:

  partner_agents.protocol_version  VARCHAR(20) NOT NULL DEFAULT 'legacy'
      Tells the outbound client (services/a2a_client.send_task_to_partner)
      which wire to use for this partner. 'legacy' = today's hand-rolled
      `POST /a2a-partner/api/a2a/tasks/send`. 'a2a_sdk' = SDK JSON-RPC at
      the partner's /a2a-rpc/rpc. Flipping the column flips the wire on
      next outbound call — no other config changes needed.

  a2a_messages.task_id_a2a   VARCHAR(64) NULL
      The SDK's Task ID (distinct from a2a_messages.id, the platform-side
      audit row id). Populated only on `protocol_ver='a2a_sdk'` rows.

  a2a_messages.task_state    VARCHAR(20) NULL
      The SDK's Task lifecycle state (SUBMITTED / WORKING / COMPLETED /
      FAILED / CANCELLED). Populated only on `protocol_ver='a2a_sdk'`
      rows; legacy delivery success/failure is captured by the existing
      `status` column.

  a2a_messages.protocol_ver  VARCHAR(20) NOT NULL DEFAULT 'legacy'
      Identifies which wire delivered/sent each row. Useful for analysis
      and debugging once both wires carry production traffic.

Idempotent against the prod state (each `op.add_column` is gated by an
inspector check) so re-running this migration is safe.

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-07
"""
from alembic import op
import sqlalchemy as sa


revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    pa_cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "protocol_version" not in pa_cols:
        op.add_column(
            "partner_agents",
            sa.Column(
                "protocol_version",
                sa.String(length=20),
                nullable=False,
                server_default="legacy",
            ),
        )

    am_cols = {c["name"] for c in insp.get_columns("a2a_messages")}
    if "task_id_a2a" not in am_cols:
        op.add_column(
            "a2a_messages",
            sa.Column("task_id_a2a", sa.String(length=64), nullable=True),
        )
    if "task_state" not in am_cols:
        op.add_column(
            "a2a_messages",
            sa.Column("task_state", sa.String(length=20), nullable=True),
        )
    if "protocol_ver" not in am_cols:
        op.add_column(
            "a2a_messages",
            sa.Column(
                "protocol_ver",
                sa.String(length=20),
                nullable=False,
                server_default="legacy",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    am_cols = {c["name"] for c in insp.get_columns("a2a_messages")}
    if "protocol_ver" in am_cols:
        op.drop_column("a2a_messages", "protocol_ver")
    if "task_state" in am_cols:
        op.drop_column("a2a_messages", "task_state")
    if "task_id_a2a" in am_cols:
        op.drop_column("a2a_messages", "task_id_a2a")

    pa_cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "protocol_version" in pa_cols:
        op.drop_column("partner_agents", "protocol_version")
