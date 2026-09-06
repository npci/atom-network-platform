# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""a2a_messages integrity + non-repudiation columns; partner_agents key version

Revision ID: 0127
Revises: 0126
Create Date: 2026-08-24

Closes THREAT_MODEL.md T1 ("No at-rest integrity check on
A2AMessage.payload") and T2 ("HMAC signature/key-version not persisted
for non-repudiation across rotations").

- `a2a_messages.payload_sha256` — sha256 of the raw inbound body at
  receipt time, so at-rest tampering (vs. in-transit, which the HMAC
  envelope already covers) can be detected by recomputing the hash
  against the current `payload` column on read.
- `a2a_messages.hmac_signature` / `hmac_key_version` — the verified
  signature and a snapshot of which key version produced it, so a
  future dispute can be adjudicated against the exact secret in effect
  at receipt time, independent of subsequent rotations.
- `partner_agents.signing_secret_version` — monotonic counter,
  incremented on every HMAC secret rotation, never reused.

Additive only; all new columns are nullable (a2a_messages) or default
to 1 (partner_agents), so every existing row remains valid with no
backfill required. Idempotent + inspector-gated (repo convention — see
0087/0123/0124/0125/0126).
"""
from alembic import op
import sqlalchemy as sa

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    a2a_cols = {c["name"] for c in insp.get_columns("a2a_messages")}
    if "payload_sha256" not in a2a_cols:
        op.add_column("a2a_messages", sa.Column("payload_sha256", sa.String(64), nullable=True))
    if "hmac_signature" not in a2a_cols:
        op.add_column("a2a_messages", sa.Column("hmac_signature", sa.String(64), nullable=True))
    if "hmac_key_version" not in a2a_cols:
        op.add_column("a2a_messages", sa.Column("hmac_key_version", sa.Integer(), nullable=True))

    partner_cols = {c["name"] for c in insp.get_columns("partner_agents")}
    if "signing_secret_version" not in partner_cols:
        op.add_column(
            "partner_agents",
            sa.Column("signing_secret_version", sa.Integer(), nullable=False,
                     server_default="1"),
        )


def downgrade():
    with op.batch_alter_table("a2a_messages") as batch_op:
        batch_op.drop_column("payload_sha256")
        batch_op.drop_column("hmac_signature")
        batch_op.drop_column("hmac_key_version")
    with op.batch_alter_table("partner_agents") as batch_op:
        batch_op.drop_column("signing_secret_version")
