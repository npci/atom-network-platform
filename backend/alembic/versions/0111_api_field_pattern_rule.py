# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""API Registry — manual regex/pattern constraint per field (tier-3).

Adds ``api_fields.pattern_rule``: a human-entered validation regex for a field,
the fallback when neither the XSD nor the code harvest captures a constraint
(e.g. imperative Java validation the tier-1 annotation scan can't see). Editable
+ lockable like the other constraint cells; the UI ships a live tester so the
author verifies the regex before saving.

Idempotent + inspector-gated (safe to re-run).

Revision ID: 0111
Revises: 0110
"""
from alembic import op
import sqlalchemy as sa

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "api_fields" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("api_fields")}
    if "pattern_rule" not in cols:
        op.add_column("api_fields", sa.Column("pattern_rule", sa.String(500), nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "api_fields" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("api_fields")}
    if "pattern_rule" in cols:
        op.drop_column("api_fields", "pattern_rule")
