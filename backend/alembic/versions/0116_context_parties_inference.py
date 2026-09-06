# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add `parties_inference` to `change_request_contexts`.

Backs the v3 clarification UX (agent-inferred parties → one multi-select
question replacing the four yes/no fan-out). Populated by
`services/context_cache._build` after `extract_proposals`; consumed by
`question_generator.build_scope_signal_questions` to pre-check the
"parties involved" clarification.

Idempotent + inspector-gated per §3.1 so re-runs against an already-
migrated DB are safe.

Revision ID: 0116
Revises: 0115
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


_TABLE = "change_request_contexts"
_COLUMN = "parties_inference"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if _COLUMN not in cols:
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if _COLUMN in cols:
        op.drop_column(_TABLE, _COLUMN)
