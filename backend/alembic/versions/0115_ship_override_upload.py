# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-item shipment overrides for the Phase C "Communicate Change" flow.

Adds the same six-column override slot to three tables — `product_kit_documents`,
`tech_specs`, `xsds` — so a user can upload a hand-authored file that substitutes
the generated artifact for that item on the next kit ship. `override_path`
present is the sole signal that a substitution exists; when absent, the ship
envelope keeps its current behaviour (attach the generated docx/pptx/mp4/xlsx).

Idempotent (inspector-gated) per the §3.1 pattern so re-runs against an already-
migrated DB are safe.

Revision ID: 0115
Revises: 0114
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


_TABLES = ("product_kit_documents", "tech_specs", "xsds")

# Column definitions are shared across the three tables so the override slot
# is byte-identical everywhere and the envelope builder can read it uniformly.
def _override_columns() -> list[sa.Column]:
    return [
        sa.Column("override_path", sa.String(length=500), nullable=True),
        sa.Column("override_filename", sa.String(length=255), nullable=True),
        sa.Column("override_sha256", sa.String(length=64), nullable=True),
        sa.Column("override_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("override_mime_type", sa.String(length=120), nullable=True),
        sa.Column("override_uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "override_uploaded_by",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in _TABLES:
        existing = {c["name"] for c in insp.get_columns(table)}
        for col in _override_columns():
            if col.name not in existing:
                op.add_column(table, col)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = [c.name for c in _override_columns()]
    for table in _TABLES:
        existing = {c["name"] for c in insp.get_columns(table)}
        for name in names:
            if name in existing:
                op.drop_column(table, name)
