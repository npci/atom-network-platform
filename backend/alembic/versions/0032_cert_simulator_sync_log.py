# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cert simulator sync log — audit table for Phase A → cert-agent test case
pushes.

Stores one row per `diff_view` and `apply` action, with summary JSON capturing
counts (added/changed/removed/applied/failed). Read by the cert-status timeline
(operation='apply' rows surface as `test_suite_registered` events) and by the
"Last synced" status line on the Phase A cert_test_cases page.

Renumbered from teammate's 0029 → 0032 to chain after the rebuilt
0031_partner_lifecycle migration.

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-06
"""
from alembic import op
import sqlalchemy as sa


revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cert_simulator_sync_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "change_request_id",
            sa.String(length=36),
            sa.ForeignKey("change_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cert_engine_partner_id",
            sa.String(length=36),
            sa.ForeignKey("partner_agents.id"),
            nullable=True,
        ),
        sa.Column(
            "actor_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_cert_simulator_sync_log_change_id",
        "cert_simulator_sync_log",
        ["change_request_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_cert_simulator_sync_log_created_at",
        "cert_simulator_sync_log",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cert_simulator_sync_log_created_at", table_name="cert_simulator_sync_log")
    op.drop_index("ix_cert_simulator_sync_log_change_id", table_name="cert_simulator_sync_log")
    op.drop_table("cert_simulator_sync_log")
