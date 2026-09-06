# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Make change_request_contexts.updated_at nullable.

Migration 0009 declared updated_at as NOT NULL with a server_default. But
SQLAlchemy's TimestampMixin explicitly sends updated_at=None on INSERT
(it relies on Python-side `onupdate` rather than a Postgres DEFAULT).
Postgres treats an explicit NULL as a violation even when a server DEFAULT
exists, so BRD context cache writes were failing with NotNullViolation.

Fix: drop the NOT NULL constraint and server default on updated_at — it
now behaves like every other table that uses TimestampMixin.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("change_request_contexts") as batch_op:
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
            server_default=None,
        )


def downgrade() -> None:
    # Restore the old (buggy) NOT NULL so the downgrade path matches 0009's
    # declared schema. Writes will fail as before.
    with op.batch_alter_table("change_request_contexts") as batch_op:
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        )
