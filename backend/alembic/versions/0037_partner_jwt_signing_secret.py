# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-partner JWT signing secret for outbound A2A.

Slice 3 of the A2A security hardening. NPCI today only mints Bearer
JWTs for the cert_engine partner (signed with the platform-wide
`settings.secret_key`). Other partners receive no Authorization header
on outbound A2A calls.

This migration adds:

  partner_agents.jwt_signing_secret VARCHAR(128) NULL

A 32-byte hex secret per partner. NPCI signs JWTs to partner X using
partner X's secret; partner X verifies with the same secret stored as
`partner_settings.npci_jwt_secret` on its side. Rotation = update the
column on NPCI + the partner-side row in lockstep, with a 24h grace
window (Slice 9 wires the grace).

Why per-partner instead of a single global secret:
  - Containment: a leak from one partner's vault doesn't impersonate
    NPCI to every other partner.
  - Rotation independence: each partner can rotate on its own schedule
    aligned with that partner's compliance cycle.

cert_engine partner is a special case — it shares NPCI's platform
secret_key (same process boundary) and continues via the existing
`fetch_bearer_jwt` handshake. `jwt_signing_secret` stays NULL on the
cert_engine row.

Idempotent (inspector-gated) so re-runs against an already-migrated DB
are safe.

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa


revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "jwt_signing_secret" not in cols:
        op.add_column(
            "partner_agents",
            sa.Column("jwt_signing_secret", sa.String(128), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "jwt_signing_secret" in cols:
        op.drop_column("partner_agents", "jwt_signing_secret")
