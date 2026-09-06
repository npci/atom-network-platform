# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Product Kit deck (.pptx) path.

D6 of the deck-renderer arc. Companion artefact to the existing
`docx_path` column: when a `product_deck` row's content is generated,
the renderer writes a .pptx alongside the .docx and stamps the path
here. NULL means either (a) the doc isn't `product_deck`, (b) the
LLM failed to emit a valid JSON outline, or (c) the renderer raised.
All three cases are non-fatal — the Product Kit ships docx-only.

Idempotent (inspector-gated) — safe to re-run.

Revision ID: 0043
Revises: 0042
Create Date: 2026-05-10
"""
from alembic import op
import sqlalchemy as sa


revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("product_kit_documents")}
    if "pptx_path" not in cols:
        op.add_column(
            "product_kit_documents",
            sa.Column("pptx_path", sa.String(500), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("product_kit_documents")}
    if "pptx_path" in cols:
        op.drop_column("product_kit_documents", "pptx_path")
