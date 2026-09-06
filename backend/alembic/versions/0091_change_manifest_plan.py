# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Change-manifest plan column — add change_manifests.plan.

A nullable JSON column persisting the human-ratified plan/SPEC the manifest was
frozen against. build_manifest() folds the plan into ``manifest_hash`` so a
re-ratified plan forces re-approval, and freeze_manifest() persists it on the row
as the audit trail of what each approval was granted against. Without this column
freeze_manifest() raised ``TypeError: 'plan' is an invalid keyword argument for
ChangeManifest``, which broke every XSD/code manifest freeze.

Idempotent + inspector-gated  . Generic JSON (not JSONB) so the
SQLite test harness works without a variant.

Revision ID: 0091
Revises: 0090
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "change_manifests" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("change_manifests")}
    if "plan" not in cols:
        op.add_column("change_manifests", sa.Column("plan", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "change_manifests" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("change_manifests")}
    if "plan" in cols:
        op.drop_column("change_manifests", "plan")
