# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Accuracy upgrade S1 — Change Analysis + Decision Ledger + impacted paths + workflow_version.

Creates the data spine for code-grounded analysis before the BRD and the
change-request-level decision ledger that binds every downstream document.

Idempotent + inspector-gated  . Generic JSON (not JSONB) so the
SQLite test harness works without variants.

Revision ID: 0085
Revises: 0084
Create Date: 2026-06-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "change_analyses" not in tables:
        op.create_table(
            "change_analyses",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
            sa.Column("technical_analysis", sa.JSON(), nullable=True),
            sa.Column("functional_plan", sa.JSON(), nullable=True),
            sa.Column("flow_spec", sa.JSON(), nullable=True),
            sa.Column("analysis_sha", sa.JSON(), nullable=True),
            sa.Column("validated_against_brd_id", sa.String(36), nullable=True),
            sa.Column("validated_against_brd_version", sa.Integer(), nullable=True),
            sa.Column("validated_against_brd_hash", sa.String(64), nullable=True),
            sa.Column("pm_ratified_by", sa.String(36), nullable=True),
            sa.Column("pm_ratified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("tech_ratified_by", sa.String(36), nullable=True),
            sa.Column("tech_ratified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("run_id", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_change_analyses_change_request_id", "change_analyses", ["change_request_id"])

    if "decision_ledger_entries" not in tables:
        op.create_table(
            "decision_ledger_entries",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
            sa.Column("question_key", sa.String(128), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("question", sa.Text(), nullable=True),
            sa.Column("options", sa.JSON(), nullable=True),
            sa.Column("chosen", sa.Text(), nullable=True),
            sa.Column("evidence", sa.JSON(), nullable=True),
            sa.Column("directive", sa.Text(), nullable=True),
            sa.Column("decided_by", sa.String(36), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decided_against", sa.JSON(), nullable=True),
            sa.Column("supersedes_id", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_decision_ledger_change_request_id", "decision_ledger_entries", ["change_request_id"])
        op.create_index("ix_decision_ledger_question_key", "decision_ledger_entries", ["question_key"])

    if "change_impacted_paths" not in tables:
        op.create_table(
            "change_impacted_paths",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
            sa.Column("repo_id", sa.String(36), nullable=False),
            sa.Column("path", sa.String(1024), nullable=False),
            sa.Column("namespace", sa.String(512), nullable=True),
            sa.Column("kind", sa.String(32), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_change_impacted_paths_change_request_id", "change_impacted_paths", ["change_request_id"])
        op.create_index("ix_change_impacted_paths_repo_path", "change_impacted_paths", ["repo_id", "path"])

    if "change_requests" in tables:
        cols = {c["name"] for c in insp.get_columns("change_requests")}
        if "workflow_version" not in cols:
            op.add_column(
                "change_requests",
                sa.Column("workflow_version", sa.Integer(), nullable=False, server_default="1"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "change_requests" in tables:
        cols = {c["name"] for c in insp.get_columns("change_requests")}
        if "workflow_version" in cols:
            op.drop_column("change_requests", "workflow_version")
    for tbl in ("change_impacted_paths", "decision_ledger_entries", "change_analyses"):
        if tbl in tables:
            op.drop_table(tbl)
