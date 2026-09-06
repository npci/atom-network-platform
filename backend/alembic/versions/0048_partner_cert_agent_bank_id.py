# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Map partner_agents → cert-agent bank_id.

Cert-agent (separate compose stack) registers banks at /api/bank-configs
with its own short codes (HDFC, ICICI001, etc). The NPCI partner_agents
table uses a UUID primary key, so the cert orchestrator needs a column
that says "this NPCI partner is THIS bank in cert-agent". Without it,
auto-running certification on partner readiness can't resolve the
correct simulator endpoint.

Nullable on purpose — partners that aren't wired to cert-agent yet stay
NULL. Orchestrator surfaces a clear error when it tries to act on a
partner with no mapping; admin UI offers an inline edit field.

Revision ID: 0048
Revises: 0047
Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "cert_agent_bank_id" not in cols:
        op.add_column(
            "partner_agents",
            sa.Column("cert_agent_bank_id", sa.String(50), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "cert_agent_bank_id" in cols:
        op.drop_column("partner_agents", "cert_agent_bank_id")
