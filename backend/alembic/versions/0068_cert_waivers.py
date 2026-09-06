# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Protocol v1 — cert_waivers table.

Backs the cert waiver flow (PDF §7.8–7.9): a partner requests a waiver for a
cert case (cert_waiver_request), NPCI's Risk+Product gate decides
(cert_waiver_decision). Idempotent + inspector-gated (house pattern).

Revision ID: 0068
Revises: 0067
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "cert_waivers" in insp.get_table_names():
        return
    op.create_table(
        "cert_waivers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False),
        sa.Column("cflow_id", sa.String(64), nullable=True),
        sa.Column("case_id", sa.String(100), nullable=False),
        sa.Column("category", sa.String(40), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="requested"),
        sa.Column("conditions", sa.Text(), nullable=True),
        sa.Column("valid_until", sa.String(40), nullable=True),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "cert_waivers" in insp.get_table_names():
        op.drop_table("cert_waivers")
