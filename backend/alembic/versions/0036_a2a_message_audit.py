# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2A message audit-trail enrichment.

Slice 8 of the A2A security hardening. Today `a2a_messages` records
direction + task_type + payload — but not WHO called, FROM WHERE, or
HOW LONG it took. RBI audit evidence wants more.

Adds seven columns:
  - caller_ip                  INET      ── X-Real-IP from nginx
  - jwt_sub                    VARCHAR   ── partner_id claim from the JWT
  - jwt_iat                    TIMESTAMPTZ ── token issuance time
  - jwt_exp                    TIMESTAMPTZ ── token expiry
  - latency_ms                 INTEGER   ── wall-clock ms from receive→commit
  - error_code                 VARCHAR   ── structured rejection code (auth fails, validation fails)
  - client_cert_fingerprint    VARCHAR   ── SHA-256 hex of mTLS client cert (Slice 6)

All nullable; legacy rows pre-Slice-8 keep their existing shape.

INET is Postgres-native and indexable for IP-range scans; on SQLite
test backends Alembic reflects it as TEXT. We use sa.dialects.postgresql.INET
with `astext_type` fallback to keep tests in-process happy without
extension shimming.

Idempotent (inspector-gated) so re-runs against an already-migrated DB
are safe.

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


_NEW_COLUMNS: list[tuple[str, sa.Column]] = [
    (
        "caller_ip",
        sa.Column(
            "caller_ip",
            postgresql.INET().with_variant(sa.String(45), "sqlite"),
            nullable=True,
        ),
    ),
    ("jwt_sub", sa.Column("jwt_sub", sa.String(64), nullable=True)),
    ("jwt_iat", sa.Column("jwt_iat", sa.DateTime(timezone=True), nullable=True)),
    ("jwt_exp", sa.Column("jwt_exp", sa.DateTime(timezone=True), nullable=True)),
    ("latency_ms", sa.Column("latency_ms", sa.Integer(), nullable=True)),
    ("error_code", sa.Column("error_code", sa.String(40), nullable=True)),
    (
        "client_cert_fingerprint",
        sa.Column("client_cert_fingerprint", sa.String(64), nullable=True),
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("a2a_messages")}
    for name, col in _NEW_COLUMNS:
        if name not in existing:
            op.add_column("a2a_messages", col)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("a2a_messages")}
    for name, _ in reversed(_NEW_COLUMNS):
        if name in existing:
            op.drop_column("a2a_messages", name)
