# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance skills — append-only versioned EA / InfoSec review rulebooks.

One row per uploaded skill version; active = highest version per skill_type.
No update/delete paths (uploads INSERT max+1), so the table doubles as the
audit trail of which rulebook was in force for any governance review run.

Idempotent + inspector-gated (safe to re-run).

Revision ID: 0119
Revises: 0116
"""
from alembic import op
import sqlalchemy as sa

revision = "0119"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "governance_skills" in set(insp.get_table_names()):
        return
    op.create_table(
        "governance_skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("skill_type", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("rules_json", sa.JSON(), nullable=True),
        sa.Column("uploaded_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("skill_type", "version", name="uq_governance_skill_version"),
    )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "governance_skills" in set(insp.get_table_names()):
        op.drop_table("governance_skills")
