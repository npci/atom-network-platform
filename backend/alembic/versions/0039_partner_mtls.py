# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""mTLS columns on partner_agents — `tls_tier` + `client_cert_fingerprint`.

Slice 6 of the A2A security hardening — the bank-tier transport
upgrade. Adds two columns:

  partner_agents.tls_tier                 VARCHAR(20)  DEFAULT 'jwt'
  partner_agents.client_cert_fingerprint  VARCHAR(64)  NULL

`tls_tier` values:
  'jwt'  — Bearer JWT only on :443 (existing behaviour, default)
  'mtls' — Pinned client cert at :8443 PLUS Bearer JWT. Banks only.

`client_cert_fingerprint` is the SHA-256 hex of the client cert that
nginx presents (`$ssl_client_fingerprint`). The auth middleware reads
nginx's `X-Client-Cert-Fingerprint` header and compares against this
column when `tls_tier == 'mtls'`.

Mtls is layered on TOP of JWT — both must pass. mTLS is the network /
transport identity; JWT is the application-layer identity. The bank
holds two independent secrets (the cert's private key + the JWT
signing key); a leak of either alone doesn't grant impersonation.

Idempotent (inspector-gated).

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa


revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "tls_tier" not in cols:
        op.add_column(
            "partner_agents",
            sa.Column("tls_tier", sa.String(20), nullable=False, server_default="jwt"),
        )
    if "client_cert_fingerprint" not in cols:
        op.add_column(
            "partner_agents",
            sa.Column("client_cert_fingerprint", sa.String(64), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "client_cert_fingerprint" in cols:
        op.drop_column("partner_agents", "client_cert_fingerprint")
    if "tls_tier" in cols:
        op.drop_column("partner_agents", "tls_tier")
