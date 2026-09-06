# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""a2a_messages: delivery retry columns

Outbound A2A sends to partner banks were single-attempt. On failure the row was flipped to
`delivery_failed` and nothing ever retried it — the table had no way to express "try again
later", so a bank that missed its Product Kit simply never got it and nobody was told.

Adds the state the retry sweeper (`a2a.retry_failed_deliveries`) schedules on:
    attempts       int  NOT NULL DEFAULT 0   -- delivery attempts made so far
    next_retry_at  timestamptz NULL          -- when a retry is due; NULL = none scheduled
    last_error_at  timestamptz NULL          -- most recent failure, for triage

Plus a partial index on the sweeper's hot query (rows actually due for retry).

Idempotent (inspector-gated) so re-runs against an already-migrated DB are safe.

Revision ID: 0104
Revises: 0103
"""
from alembic import op
import sqlalchemy as sa

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None

_TABLE = "a2a_messages"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns(_TABLE)}

    if "attempts" not in cols:
        op.add_column(_TABLE, sa.Column("attempts", sa.Integer(), nullable=False,
                                        server_default="0"))
    if "next_retry_at" not in cols:
        op.add_column(_TABLE, sa.Column("next_retry_at", sa.DateTime(timezone=True),
                                        nullable=True))
    if "last_error_at" not in cols:
        op.add_column(_TABLE, sa.Column("last_error_at", sa.DateTime(timezone=True),
                                        nullable=True))

    # Backfill: every existing row has had exactly one attempt made against it.
    op.execute(f"UPDATE {_TABLE} SET attempts = 1 WHERE attempts = 0")

    idx = {i["name"] for i in insp.get_indexes(_TABLE)}
    if "ix_a2a_messages_next_retry_at" not in idx:
        # Partial index — the sweeper only ever scans rows with a retry scheduled.
        if bind.dialect.name == "postgresql":
            op.execute(
                "CREATE INDEX ix_a2a_messages_next_retry_at ON a2a_messages (next_retry_at) "
                "WHERE next_retry_at IS NOT NULL"
            )
        else:  # SQLite (tests) has no partial-index support in op.create_index
            op.create_index("ix_a2a_messages_next_retry_at", _TABLE, ["next_retry_at"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    idx = {i["name"] for i in insp.get_indexes(_TABLE)}
    if "ix_a2a_messages_next_retry_at" in idx:
        op.drop_index("ix_a2a_messages_next_retry_at", table_name=_TABLE)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    for c in ("last_error_at", "next_retry_at", "attempts"):
        if c in cols:
            op.drop_column(_TABLE, c)
