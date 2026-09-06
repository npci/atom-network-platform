# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add change_request_context table for cached taxonomy, retrieved chunks, and structured proposals.

One row per change request. Populated at end of Research stage (or lazily by
BRD/Tech Spec generation). Consumed by all downstream Phase A agents so they
share a single, consistent ground truth.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "change_request_contexts",
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id", ondelete="CASCADE"), primary_key=True),

        # Taxonomy classification
        sa.Column("taxonomy_primary", sa.String(64), nullable=True),
        sa.Column("taxonomy_labels", postgresql.JSONB, nullable=True),
        sa.Column("taxonomy_confidence", sa.Float, nullable=True),
        sa.Column("taxonomy_rationale", sa.Text, nullable=True),

        # Retrieved context — keep chunk metadata + text so agents don't re-query
        sa.Column("retrieved_chunks", postgresql.JSONB, nullable=True),

        # Structured proposals (APIs, fields, error codes, flow, ...)
        sa.Column("proposals", postgresql.JSONB, nullable=True),
        sa.Column("proposals_confidence", sa.String(32), nullable=True),  # high|medium-high|medium|low

        # Metadata
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_version", sa.Integer, nullable=True),  # research version used to build this

        # Timestamps — match app.models.base.TimestampMixin: updated_at is nullable,
        # set by SQLAlchemy onupdate hook (not Postgres). Otherwise an INSERT that
        # explicitly sends updated_at=None (mixin default) violates the NOT NULL.
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("change_request_contexts")
