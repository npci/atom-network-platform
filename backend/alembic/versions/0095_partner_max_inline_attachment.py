# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""partner_agents — per-partner inline-attachment size cap.

Adds `max_inline_attachment_bytes`: the per-partner cap (bytes) on the
base64-encoded size of a single product-kit attachment shipped inline in the
A2A envelope. Oversize attachments are omitted (metadata kept) so a large inline
video/docx can't blow a partner's ingress body-size limit (ngrok / envoy) — the
kit-send-resets-mid-upload failure. NULL inherits the global
`settings.partner_max_inline_attachment_bytes`.

Idempotent + inspector-gated  . Integer works on Postgres and the
SQLite test backend — no `.with_variant` needed.

Revision ID: 0095
Revises: 0094
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

_TABLE = "partner_agents"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "max_inline_attachment_bytes" not in cols:
        op.add_column(_TABLE, sa.Column("max_inline_attachment_bytes", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "max_inline_attachment_bytes" in cols:
        op.drop_column(_TABLE, "max_inline_attachment_bytes")
