# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""partner_agents — per-partner outbound TLS verification (ssl_verify + ca_cert_pem).

Lets an operator control, per partner, how the backend verifies that partner's
HTTPS SERVER cert when it calls `endpoint_url` (the Test-connectivity probe and
the real A2A card fetch): toggle verification on/off, and upload a CA/cert PEM to
trust. NULL `ssl_verify` inherits the global `settings.partner_tls_verify`.

Idempotent + inspector-gated  . Boolean/Text work on Postgres and
the SQLite test backend — no `.with_variant` needed.

Revision ID: 0094
Revises: 0093
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

_TABLE = "partner_agents"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "ssl_verify" not in cols:
        op.add_column(_TABLE, sa.Column("ssl_verify", sa.Boolean(), nullable=True))
    if "ca_cert_pem" not in cols:
        op.add_column(_TABLE, sa.Column("ca_cert_pem", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "ca_cert_pem" in cols:
        op.drop_column(_TABLE, "ca_cert_pem")
    if "ssl_verify" in cols:
        op.drop_column(_TABLE, "ssl_verify")
