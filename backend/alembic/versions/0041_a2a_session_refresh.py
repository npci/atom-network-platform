# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2A session refresh-token tracking.

Slice 9 of the A2A security hardening — short-TTL access tokens
backed by long-lived refresh tokens. Adds two columns to
`a2a_sessions`:

  refresh_token_hash  VARCHAR(200)  NULL    SHA-256 of the partner's
                                            current refresh token
  refreshed_at        TIMESTAMPTZ   NULL    last time /auth/refresh
                                            rotated this session's
                                            access token

Why short access + long refresh:
  * Reduces the blast radius of a leaked access token (Slice 9
    drops TTL 1h → 15m). A leaked token now grants <= 15 min of
    impersonation.
  * A long refresh token lets honest clients keep their session
    alive for 24h without re-doing the api_key handshake every
    15 min. The refresh token rotates on use (every refresh
    re-issues it) so a stolen refresh detects when the legit
    client uses theirs and the stolen one becomes invalid.

Idempotent inspector-gated.

Revision ID: 0041
Revises: 0040
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa


revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


_NEW_COLUMNS: list[tuple[str, sa.Column]] = [
    ("refresh_token_hash", sa.Column("refresh_token_hash", sa.String(200), nullable=True)),
    ("refreshed_at",       sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True)),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("a2a_sessions")}
    for name, col in _NEW_COLUMNS:
        if name not in existing:
            op.add_column("a2a_sessions", col)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("a2a_sessions")}
    for name, _ in reversed(_NEW_COLUMNS):
        if name in existing:
            op.drop_column("a2a_sessions", name)
