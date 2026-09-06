# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Source document (seed BRD) uploaded at change creation.

The PM can attach a detailed BRD/requirements document when creating a change.
It does NOT replace any generated artifact — it is rich INPUT material injected
into the Phase A prompts (enhancer, research, canvas, BRD gen) so the pipeline
starts from facts instead of assumptions. Stored as extracted text on the
change row itself: one document per change, read at prompt-assembly time.

Idempotent + inspector-gated (repo migration pattern since 0035).
"""
import sqlalchemy as sa
from alembic import op

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("change_requests")}
    if "source_doc_name" not in cols:
        op.add_column("change_requests", sa.Column("source_doc_name", sa.String(500), nullable=True))
    if "source_doc_text" not in cols:
        op.add_column("change_requests", sa.Column("source_doc_text", sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("change_requests")}
    if "source_doc_text" in cols:
        op.drop_column("change_requests", "source_doc_text")
    if "source_doc_name" in cols:
        op.drop_column("change_requests", "source_doc_name")
