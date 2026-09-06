# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Make v2 (BRD→XSD→TSD) the default workflow_version.

The XSD-before-Tech-Spec order is now the standard flow, so the
``change_requests.workflow_version`` server-default flips 1 → 2: any insert that
doesn't specify a version gets v2 (the app always stamps 2 explicitly too).

Existing rows are deliberately NOT backfilled — an in-flight legacy v1 change
keeps the order it actually executed; re-interpreting a mid-flow change as v2
could skip its XSD stage. Postgres-only alter; fresh SQLite DBs already get "2"
from the model default. Idempotent + inspector-gated  .

Revision ID: 0086
Revises: 0085
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "change_requests" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("change_requests")}
    if "workflow_version" not in cols:
        return
    if bind.dialect.name == "postgresql":
        op.alter_column("change_requests", "workflow_version", server_default="2")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column("change_requests", "workflow_version", server_default="1")
