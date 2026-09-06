# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""llm_usage_records — per-call LLM token/cost ledger for the Usage dashboard

Revision ID: 0087_llm_usage_records
Revises: 0086_workflow_version_default_v2
Create Date: 2026-06-23

Idempotent + inspector-gated : safe to re-run against an already-migrated DB.
"""
import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "llm_usage_records" not in insp.get_table_names():
        op.create_table(
            "llm_usage_records",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("change_request_id", sa.String(36), nullable=True),
            sa.Column("run_id", sa.String(36), nullable=True),
            sa.Column("kind", sa.String(32), nullable=True),
            sa.Column("section", sa.String(64), nullable=True),
            sa.Column("model", sa.String(80), nullable=True),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost_usd", sa.Float(), nullable=True),
        )
    existing_idx = {i["name"] for i in insp.get_indexes("llm_usage_records")} \
        if "llm_usage_records" in insp.get_table_names() else set()
    for col in ("ts", "change_request_id", "run_id"):
        name = f"ix_llm_usage_records_{col}"
        if name not in existing_idx:
            op.create_index(name, "llm_usage_records", [col])


def downgrade():
    op.drop_table("llm_usage_records")
