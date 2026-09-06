# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2ASession revocation flag.

Slice 2 of the A2A security hardening. Adds `revoked_at TIMESTAMPTZ NULL`
to `a2a_sessions` so the new `SdkAuthMiddleware` (Slice 2) can refuse
JWTs whose session has been administratively revoked, and so the
admin endpoint `POST /admin/partners/{id}/revoke-sessions` (this slice)
can mark every session for a partner revoked in one shot.

Idempotent (inspector-gated) so re-runs against an already-migrated DB
are safe.

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa


revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("a2a_sessions")}
    if "revoked_at" not in cols:
        op.add_column(
            "a2a_sessions",
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("a2a_sessions")}
    if "revoked_at" in cols:
        op.drop_column("a2a_sessions", "revoked_at")
