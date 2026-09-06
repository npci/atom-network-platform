# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-partner HMAC signing secret for the A2A envelope.

Slice 5 of the A2A security hardening. Adds a SECOND per-partner
secret — distinct from `jwt_signing_secret` (Slice 3):

  jwt_signing_secret  → signs the IDENTITY (Bearer JWT, who's calling)
  signing_secret      → signs the PAYLOAD (HMAC envelope, what was sent)

The envelope adds three headers to every A2A request:
  X-NPCI-Timestamp, X-NPCI-Nonce, X-NPCI-Signature

The signature is HMAC-SHA256(secret, f"{ts}.{nonce}.{sha256_hex(body)}")
in lowercase hex, recomputed by the receiver. Combined with a
short-window timestamp check and a redis-backed nonce uniqueness
table, this gives non-repudiation + replay resistance even if a JWT
leaks (the attacker still can't reuse a captured request, and can't
forge a new one without the HMAC secret).

Idempotent (inspector-gated) — safe to re-run.

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa


revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "signing_secret" not in cols:
        op.add_column(
            "partner_agents",
            sa.Column("signing_secret", sa.String(128), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "signing_secret" in cols:
        op.drop_column("partner_agents", "signing_secret")
