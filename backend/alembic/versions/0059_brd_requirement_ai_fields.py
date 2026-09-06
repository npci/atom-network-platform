# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add AI-classification fields to brd_requirements.

`source`       — 'manual' (PM-entered) or 'ai' (extracted+classified from the
                 BRD by the brd_extractor agent). Lets the UI badge AI rows and
                 the generate endpoint regenerate without clobbering manual ones.
`ai_rationale` — one-line LLM explanation of why a requirement was classified
                 mandatory/optional. Shown as a tooltip in the Negotiation Hub.

Idempotent + inspector-gated .
"""
from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "brd_requirements" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("brd_requirements")}
    if "source" not in cols:
        op.add_column(
            "brd_requirements",
            sa.Column("source", sa.String(10), nullable=False, server_default="manual"),
        )
    if "ai_rationale" not in cols:
        op.add_column(
            "brd_requirements",
            sa.Column("ai_rationale", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "brd_requirements" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("brd_requirements")}
    if "ai_rationale" in cols:
        op.drop_column("brd_requirements", "ai_rationale")
    if "source" in cols:
        op.drop_column("brd_requirements", "source")
