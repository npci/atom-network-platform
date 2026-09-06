# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance skill SLOTS — multiple skills per type (name + enabled).

The org ships several skills per type (the InfoSec repo carries four: secret
scan, SAST, CBoN, SCA); the platform previously held ONE active skill per type
(highest version wins), forcing them to be hand-combined into a single bundle.

This adds slot semantics: every upload lands under a ``name`` (derived from the
SKILL.md frontmatter), the active row of a slot is its highest version, and a
stage executes EVERY enabled slot of its type. Version numbering stays GLOBAL
per type (the existing (skill_type, version) uniqueness is untouched), so
pinned {type, version, checksum} audit anchors on runs remain unambiguous.
Existing rows backfill to name='default' via the server default and keep
working unchanged. ``enabled`` lets an admin retire a slot without breaking
the append-only audit trail (rows are never deleted).

Idempotent + inspector-gated (safe to re-run).

Revision ID: 0122
Revises: 0121
"""
from alembic import op
import sqlalchemy as sa

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("governance_skills")}
    if "name" not in cols:
        op.add_column("governance_skills",
                      sa.Column("name", sa.String(120), nullable=False,
                                server_default="default"))
    if "enabled" not in cols:
        op.add_column("governance_skills",
                      sa.Column("enabled", sa.Boolean(), nullable=False,
                                server_default=sa.text("true")))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("governance_skills")}
    if "enabled" in cols:
        op.drop_column("governance_skills", "enabled")
    if "name" in cols:
        op.drop_column("governance_skills", "name")
