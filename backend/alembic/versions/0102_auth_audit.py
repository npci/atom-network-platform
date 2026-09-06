# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""auth_audit table — authentication audit trail (InfoSec phase 4).

Append-only log of auth events (login success/fail, CAPTCHA/lockout, MFA
enrol/verify/disable/reset, LDAP provisioning) for InfoSec review.

Idempotent + inspector-gated  .

Revision ID: 0102
Revises: 0101
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None

_TABLE = "auth_audit"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event", sa.String(length=48), nullable=False),
        sa.Column("username", sa.String(length=150), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_auth_audit_event", _TABLE, ["event"])
    op.create_index("ix_auth_audit_username", _TABLE, ["username"])
    op.create_index("ix_auth_audit_created_at", _TABLE, ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        op.drop_table(_TABLE)
