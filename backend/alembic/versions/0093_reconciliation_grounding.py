# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Grounding JSON on document_reconciliations — code-back for folded plan deltas.

Stores the ``delta_grounding`` pass output (impact / schema adds / impacted paths /
risk / questions) computed during the 'applying' phase, so the fold merges
precomputed structured grounding and the panel can surface risk / questions before
approval.

Idempotent inspector-gated add_column. Generic JSON (matches the existing
conflicts/resolutions columns; JSON works on Postgres and the sqlite test backend).

Revision ID: 0093
Revises: 0092
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa


revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None

_TABLE = "document_reconciliations"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "grounding" not in cols:
        op.add_column(_TABLE, sa.Column("grounding", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "grounding" in cols:
        op.drop_column(_TABLE, "grounding")
