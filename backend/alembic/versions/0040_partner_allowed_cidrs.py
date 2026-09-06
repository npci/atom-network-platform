# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-partner CIDR allowlist + rate-limit override.

Slice 7 of the A2A security hardening. Adds two columns:

  partner_agents.allowed_cidrs    JSONB    NULL
                                  list of CIDR strings, e.g.
                                  ["10.0.0.0/8", "192.168.1.0/24"]
                                  NULL = no IP enforcement (default)

  partner_agents.rate_limit_rps   INTEGER  DEFAULT 100
                                  per-partner rate cap. nginx today
                                  applies a flat 100r/s zone; this
                                  column is read by the auth middleware
                                  and (later, Slice 9) by an njs-driven
                                  dynamic override on the nginx side.

Enforcement model:
  * nginx applies a flat baseline limit_req zone keyed on
    $http_authorization. Burst quota covers spikes.
  * The middleware enforces `allowed_cidrs` post-JWT (we need the
    partner row to read the list). Caller IP comes from `X-Real-IP`,
    already set by nginx for every location block.
  * `rate_limit_rps` is stored but not yet enforced per-partner; Slice
    9 wires the override.

Idempotent inspector-gated. JSONB on Postgres / JSON variant on SQLite
test backends.

Revision ID: 0040
Revises: 0039
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


_NEW_COLUMNS: list[tuple[str, sa.Column]] = [
    (
        "allowed_cidrs",
        sa.Column(
            "allowed_cidrs",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    ),
    (
        "rate_limit_rps",
        sa.Column(
            "rate_limit_rps",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("partner_agents")}
    for name, col in _NEW_COLUMNS:
        if name not in existing:
            op.add_column("partner_agents", col)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("partner_agents")}
    for name, _ in reversed(_NEW_COLUMNS):
        if name in existing:
            op.drop_column("partner_agents", name)
