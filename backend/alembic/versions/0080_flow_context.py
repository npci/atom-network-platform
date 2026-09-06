# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Index-time API flow-map — flow_context table.

THE BOOK v3.4 (reuse-first §). One row per repo: which API carries the
transaction/credit-debit leg vs the meta APIs, + multi-leg flow sequences.
Generated at index time (parallel to module_context) and pulled by the reuse-first
approach gate. Idempotent + inspector-gated  . Generic JSON to match
the ORM.

Revision ID: 0080
Revises: 0079
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa


revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "flow_context" in set(insp.get_table_names()):
        return
    op.create_table(
        "flow_context",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repo_id", sa.String(36), sa.ForeignKey("code_repos.id"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("transaction_apis", sa.JSON(), nullable=True),
        sa.Column("meta_apis", sa.JSON(), nullable=True),
        sa.Column("flows", sa.JSON(), nullable=True),
        sa.Column("entry_points", sa.JSON(), nullable=True),
        sa.Column("base_commit_sha", sa.String(64), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_flow_context_repo_id", "flow_context", ["repo_id"], unique=True)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "flow_context" not in set(insp.get_table_names()):
        return
    op.drop_index("ix_flow_context_repo_id", table_name="flow_context")
    op.drop_table("flow_context")
