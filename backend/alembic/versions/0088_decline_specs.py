# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-feature Decline & Timeout design artifact.

Adds the ``decline_specs`` table: the authored, human-approved source of truth
for WHICH failure cases (business declines, timeouts, negative acks) a feature's
certification pack must cover. Authored during BRD/TSD by the ``decline_designer``
agent; consumed deterministically by the cert engine (one test per approved row).

  decline_specs.spec_json    JSONB   serialized FeatureDeclineSpec
                                     (rows + excluded + minted new_codes)
  decline_specs.status       enum    draft | approved  (only approved drives cert)

One JSON blob rather than normalized rows: the spec is authored, reviewed, and
consumed as a single unit — the same shape the engine validates against.

Idempotent inspector-gated table create. JSONB on Postgres / JSON variant on the
SQLite test backend (mirrors 0040).

Revision ID: 0088
Revises: 0087
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None

_TABLE = "decline_specs"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("change_request_id", sa.String(length=36), nullable=False),
        sa.Column(
            "spec_json",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        # Reuse the existing shared ArtifactStatus enum (created in 0001 as
        # `artifactstatus`); create_type=False so we don't re-CREATE TYPE.
        # String variant on the SQLite test backend.
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft", "approved", name="artifactstatus", create_type=False
            ).with_variant(sa.String(length=20), "sqlite"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("approved_by", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["change_request_id"], ["change_requests.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_decline_specs_change_request_id", _TABLE, ["change_request_id"], unique=False
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    op.drop_index("ix_decline_specs_change_request_id", table_name=_TABLE)
    op.drop_table(_TABLE)
