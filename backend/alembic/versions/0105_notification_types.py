# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""notifications: add operational alert types

`NotificationType` gained DELIVERY_FAILED and MANDATORY_REJECTION on the Python side, but
`type` is a native Postgres ENUM — extending the Python Enum does NOT extend the DB type.
Without this, every operational alert fails on insert with:

    invalid input value for enum notificationtype: "delivery_failed"

which the best-effort `notify()` swallows, so alerts would silently never be written —
exactly the "failure with no signal" problem these notifications exist to fix.

`ADD VALUE IF NOT EXISTS` is idempotent (PG 12+). Postgres cannot DROP an enum value, so
downgrade is intentionally a no-op — leaving an unused label is harmless.

Revision ID: 0105
Revises: 0104
"""
from alembic import op
import sqlalchemy as sa

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

_NEW_VALUES = ("delivery_failed", "mandatory_rejection")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite (tests) stores enums as VARCHAR — nothing to alter.
    existing = {r[0] for r in bind.execute(sa.text(
        "SELECT e.enumlabel FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'notificationtype'"
    ))}
    if not existing:
        return  # type absent (fresh DB builds it from the model with all values)
    for val in _NEW_VALUES:
        if val not in existing:
            op.execute(f"ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS '{val}'")


def downgrade() -> None:
    # Postgres provides no way to remove an enum label; an unused one is inert.
    pass
