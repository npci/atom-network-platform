# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic codegen M-A — orchestration / run-state tables.

THE BOOK v3.3, §13.2 feature migration 1 of 4. Creates the durable spine for
the agentic XSD-driven code-change state machine (§3/§9/§10/§11):

    agentic_runs, agentic_run_repos, agentic_events,
    change_manifests, verification_runs, review_findings

Idempotent + inspector-gated  so re-runs against an already-
migrated DB are no-ops. Indexes and uniques are added together in M-D
(0070) per the four-migration split. JSON columns use the generic ``JSON``
type to match the ORM (``app/models/agentic.py``).

Revision ID: 0075
Revises: 0074
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "agentic_runs" not in tables:
        op.create_table(
            "agentic_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
            sa.Column("phase", sa.String(40), nullable=False, server_default="pending"),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("attempts_json", sa.JSON(), nullable=True),
            sa.Column("selected_repo_ids", sa.JSON(), nullable=True),
            sa.Column("lease_owner", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("manifest_hash", sa.String(64), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("platform", sa.String(20), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "agentic_run_repos" not in tables:
        op.create_table(
            "agentic_run_repos",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("repo_id", sa.String(36), sa.ForeignKey("code_repos.id"), nullable=False),
            sa.Column("base_commit_sha", sa.String(64), nullable=True),
            sa.Column("branch", sa.String(200), nullable=True),
            sa.Column("mr_url", sa.String(1000), nullable=True),
            sa.Column("push_state", sa.String(40), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "agentic_events" not in tables:
        op.create_table(
            "agentic_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "change_manifests" not in tables:
        op.create_table(
            "change_manifests",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("manifest_hash", sa.String(64), nullable=False),
            sa.Column("selected_repo_ids", sa.JSON(), nullable=True),
            sa.Column("per_repo", sa.JSON(), nullable=True),
            sa.Column("operations", sa.JSON(), nullable=True),
            sa.Column("verification", sa.JSON(), nullable=True),
            sa.Column("review", sa.JSON(), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("approved_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "verification_runs" not in tables:
        op.create_table(
            "verification_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("round", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("smoke_passed", sa.Boolean(), nullable=True),
            sa.Column("raw_output", sa.Text(), nullable=True),
            sa.Column("llm_reasoning", sa.Text(), nullable=True),
            sa.Column("decision", sa.String(20), nullable=True),
            sa.Column("plan", sa.JSON(), nullable=True),
            sa.Column("gates", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "review_findings" not in tables:
        op.create_table(
            "review_findings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("round", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("severity", sa.String(20), nullable=True),
            sa.Column("category", sa.String(20), nullable=True),
            sa.Column("repo_id", sa.String(36), nullable=True),
            sa.Column("file", sa.String(1000), nullable=True),
            sa.Column("line", sa.Integer(), nullable=True),
            sa.Column("why", sa.Text(), nullable=True),
            sa.Column("suggested_fix", sa.Text(), nullable=True),
            sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("reviewer_model", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    for t in (
        "review_findings", "verification_runs", "change_manifests",
        "agentic_events", "agentic_run_repos", "agentic_runs",
    ):
        if t in tables:
            op.drop_table(t)
