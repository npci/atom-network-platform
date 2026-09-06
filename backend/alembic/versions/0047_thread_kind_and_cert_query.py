# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cert messaging channel — partition NegotiationThread by kind.

Adds a `kind` column to negotiation_threads to separate the existing
Phase C "general" Q&A channel from a new "cert" certification-
clarification channel. Both channels travel over the same Google A2A
SDK wire; the kind column is the single partition key — APIs filter by
it, UI tabs filter by it, no row is ever shared between channels.

  negotiation_threads.kind   varchar(20) NOT NULL DEFAULT 'general'
                             Values: 'general' | 'cert'. Existing rows
                             are backfilled to 'general' via the column
                             default. New cert-tab threads are created
                             with kind='cert'.

A new A2ATaskType.CERT_QUERY enum value is added on the Python side.
The DB column `a2a_messages.task_type` is plain varchar(50), so no
schema change is required there — adding the Python enum value is
sufficient for both reads and writes.

Revision ID: 0047
Revises: 0046
Create Date: 2026-05-10
"""
from alembic import op
import sqlalchemy as sa


revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    cols = {c["name"] for c in insp.get_columns("negotiation_threads")}
    if "kind" not in cols:
        op.add_column(
            "negotiation_threads",
            sa.Column("kind", sa.String(20), nullable=False, server_default="general"),
        )
        op.create_index(
            "ix_negotiation_threads_kind",
            "negotiation_threads",
            ["kind"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("negotiation_threads")}
    if "kind" in cols:
        op.drop_index("ix_negotiation_threads_kind", table_name="negotiation_threads")
        op.drop_column("negotiation_threads", "kind")
