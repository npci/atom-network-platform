# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Repair: actually add the product_kit_documents video columns.

`0089_product_kit_video_fields` adds four nullable columns (`script_json`,
`video_provider`, `video_model`, `video_duration_sec`). Its own code is and
always was correct — every committed version binds the inspector properly — so
a database that ran it has the columns. Some do not, and the cause is upstream
of 0089 itself: revision id 0088/0089 were once claimed by TWO branches at once
(see db56f88d, "resolve migration collision (0088/0089)"). While that collision
stood, `alembic upgrade head` aborted on duplicate revisions, and a database
recovered by stamping forward past the failure ends up recorded as having run
0089 without its DDL ever executing.

The damage is not limited to the video feature: `ProductKitDocument` declares
all four, so SQLAlchemy emits them in EVERY select against the table, and any
query touching product-kit documents fails with

    UndefinedColumn: column product_kit_documents.script_json does not exist

That is what silently disabled the Phase C feasibility resolver — it loads
product-kit docs for context, and `auto_resolve_background` logs-and-swallows,
so the PM simply never got a suggested reply and nothing surfaced anywhere.

This revision repairs those databases. Inspector-gated, so it is a plain no-op
on any database where 0089 did run — which is every database built from scratch
after the collision was resolved.

Revision ID: 0123
Revises: 0122
Create Date: 2026-08-10
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

_TABLE = "product_kit_documents"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}

    if "script_json" not in cols:
        op.add_column(
            _TABLE,
            sa.Column(
                "script_json",
                postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
                nullable=True,
            ),
        )
    if "video_provider" not in cols:
        op.add_column(_TABLE, sa.Column("video_provider", sa.String(length=40), nullable=True))
    if "video_model" not in cols:
        op.add_column(_TABLE, sa.Column("video_model", sa.String(length=80), nullable=True))
    if "video_duration_sec" not in cols:
        op.add_column(_TABLE, sa.Column("video_duration_sec", sa.Integer(), nullable=True))


def downgrade() -> None:
    # Deliberately a no-op: 0089 owns these columns, and dropping them here
    # would leave a database that downgraded only this revision without them.
    pass
