# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""users — auth_source column for hybrid LDAP/AD auth (InfoSec phase 3).

Adds `auth_source` ('local' | 'ldap'): 'local' users authenticate with the
bcrypt password_hash (today's path, incl. the break-glass admin); 'ldap' users
authenticate by BIND against the directory and are JIT-provisioned on first
successful login. Existing rows default to 'local'.

Idempotent + inspector-gated  . Plain String works on Postgres and
the SQLite test backend — a DB enum would need a separate type migration.

Revision ID: 0101
Revises: 0100
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

_TABLE = "users"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "auth_source" not in cols:
        op.add_column(_TABLE, sa.Column("auth_source", sa.String(length=16),
                                        nullable=False, server_default="local"))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "auth_source" in cols:
        op.drop_column(_TABLE, "auth_source")
