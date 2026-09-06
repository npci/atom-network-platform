# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""negotiation_messages.event_kind: widen 20 → 40

The blocker/negotiation thread split introduced the event kind
`blocker_status_update` — 21 characters, one over the existing `VARCHAR(20)`. Inserting
it raised `StringDataRightTruncation`, so a PM pushing an interim blocker status would
have hit a 500 (the dual-write is wrapped in try/except, so the timeline row would simply
have gone missing rather than surfacing the cause).

Widening only; no data is altered and every existing value still fits.

Revision ID: 0106
Revises: 0105
"""
from alembic import op
import sqlalchemy as sa

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None

_TABLE = "negotiation_messages"
_COL = "event_kind"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    col = next((c for c in insp.get_columns(_TABLE) if c["name"] == _COL), None)
    if col is None:
        return
    length = getattr(col["type"], "length", None)
    if length is not None and length >= 40:
        return  # already widened
    op.alter_column(_TABLE, _COL,
                    existing_type=sa.String(20), type_=sa.String(40),
                    existing_nullable=True)


def downgrade() -> None:
    # Narrowing would truncate any 'blocker_status_update' rows written since the
    # upgrade, so this is intentionally not reversed automatically.
    pass
