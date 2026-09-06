# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""users — TOTP MFA columns (InfoSec phase 2).

Adds:
  mfa_enabled       BOOLEAN  not null default false
  mfa_secret        VARCHAR  Fernet-encrypted TOTP seed (never stored in clear)
  mfa_backup_codes  JSON     list of bcrypt-hashed single-use recovery codes

Idempotent + inspector-gated   Boolean/JSON work on Postgres and
the SQLite test backend.

NOTE (migration chain): per the project owner, versioning continues from 0100 and
the 0095/0096 collision (my `0095_partner_max_inline_attachment` vs the teammate's
`0095_upi_baseline_phase15`, and the two `0096_*`) is to be RETROFITTED separately.
`down_revision` is set to the intended predecessor "0099"; adjust it during that
retrofit if the real head revision id differs.

Revision ID: 0100
Revises: 0095
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0100"
down_revision = "0095"
branch_labels = None
depends_on = None

_TABLE = "users"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "mfa_enabled" not in cols:
        op.add_column(_TABLE, sa.Column("mfa_enabled", sa.Boolean(), nullable=False,
                                        server_default=sa.false()))
    if "mfa_secret" not in cols:
        op.add_column(_TABLE, sa.Column("mfa_secret", sa.String(length=255), nullable=True))
    if "mfa_backup_codes" not in cols:
        op.add_column(_TABLE, sa.Column("mfa_backup_codes", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "mfa_backup_codes" in cols:
        op.drop_column(_TABLE, "mfa_backup_codes")
    if "mfa_secret" in cols:
        op.drop_column(_TABLE, "mfa_secret")
    if "mfa_enabled" in cols:
        op.drop_column(_TABLE, "mfa_enabled")
