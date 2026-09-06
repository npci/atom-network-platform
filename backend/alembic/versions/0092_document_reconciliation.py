# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""document_reconciliations — uploaded-doc vs ratified-plan reconciliation.

The async reconciliation gate: one row per upload that was checked against the
ratified Change-Analysis plan. Holds the detected conflicts + the user's
resolutions; downstream generation is blocked while a row is ``pending``.

Idempotent + inspector-gated  . Generic JSON (not JSONB) so the
SQLite test harness works without variants. Timestamps carry no server_default —
the ORM ``TimestampMixin`` sets them (matches 0085).

Revision ID: 0092
Revises: 0091
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "document_reconciliations" not in tables:
        op.create_table(
            "document_reconciliations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
            sa.Column("doc_kind", sa.String(32), nullable=False),
            sa.Column("doc_id", sa.String(36), nullable=True),
            sa.Column("doc_version", sa.Integer(), nullable=True),
            sa.Column("plan_version_before", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("conflicts", sa.JSON(), nullable=True),
            sa.Column("resolutions", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_document_reconciliations_change_request_id", "document_reconciliations", ["change_request_id"])
        op.create_index("ix_document_reconciliations_change_kind_status", "document_reconciliations", ["change_request_id", "doc_kind", "status"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "document_reconciliations" in set(insp.get_table_names()):
        op.drop_table("document_reconciliations")
