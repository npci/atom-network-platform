# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Segmented video-script + generation metadata on product_kit_documents.

Adds four nullable columns used by the Phase A video-generation feature
(promo_video / explainer_video):

  script_json        JSONB   serialized VideoScript (8s-segmented script)
  video_provider     str     provider used to generate the clips
  video_model        str     target video model (e.g. veo-3.1-generate-preview)
  video_duration_sec int     total intended duration

The final merged MP4 reuses the existing ``file_path`` column (already served
by the video download endpoint), so no new column is needed for it.

Idempotent inspector-gated add_column. JSONB on Postgres / JSON variant on the
SQLite test backend (mirrors 0088).

Revision ID: 0089
Revises: 0088
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0089"
down_revision = "0088"
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
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}

    for col in ("video_duration_sec", "video_model", "video_provider", "script_json"):
        if col in cols:
            op.drop_column(_TABLE, col)
