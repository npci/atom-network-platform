# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase B schema — Design to Build tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-14
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extend existing doccategory enum with java_source
    op.execute("ALTER TYPE doccategory ADD VALUE IF NOT EXISTS 'java_source'")

    # phase_b_runs
    op.create_table(
        "phase_b_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("in_progress", "completed", "blocked", name="phasebrunstatus"),
            nullable=False,
            server_default="in_progress",
        ),
        sa.Column(
            "current_step",
            sa.Enum(
                "code_change", "code_review", "is_review", "git",
                "build", "deploy", "test_gen", "test_exec", "triage", "completed",
                name="phasebstep",
            ),
            nullable=False,
            server_default="code_change",
        ),
        sa.Column("iteration_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("gitlab_repo", sa.String(500), nullable=True),
        sa.Column("gitlab_branch", sa.String(200), nullable=True, server_default="main"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # code_iterations
    op.create_table(
        "code_iterations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("phase_b_run_id", sa.String(36), sa.ForeignKey("phase_b_runs.id"), nullable=False),
        sa.Column("iteration_number", sa.Integer, nullable=False),
        sa.Column("generated_output", sa.Text, nullable=True),   # full agent response (markdown + file markers)
        sa.Column("files_changed", sa.JSON, nullable=True),      # [{path, content}] parsed from markers
        sa.Column("user_feedback", sa.Text, nullable=True),
        sa.Column(
            "trigger",
            sa.Enum(
                "initial", "user_feedback", "code_review_feedback",
                "is_review_feedback", "build_failure", "deploy_failure", "uat_failure",
                name="iterationtrigger",
            ),
            nullable=False,
            server_default="initial",
        ),
        sa.Column("approved", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # code_review_results
    op.create_table(
        "code_review_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code_iteration_id", sa.String(36), sa.ForeignKey("code_iterations.id"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("clean", "issues_found", name="reviewstatus"),
            nullable=False,
        ),
        sa.Column("issues", sa.JSON, nullable=True),  # [{rule, severity, file, line, message, fix}]
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # is_review_results
    op.create_table(
        "is_review_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code_iteration_id", sa.String(36), sa.ForeignKey("code_iterations.id"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("clean", "issues_found", name="isreviewstatus"),
            nullable=False,
        ),
        sa.Column("findings", sa.JSON, nullable=True),  # [{cwe, severity, file, line, description, remediation}]
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # git_events
    op.create_table(
        "git_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("phase_b_run_id", sa.String(36), sa.ForeignKey("phase_b_runs.id"), nullable=False),
        sa.Column("branch_name", sa.String(500), nullable=True),
        sa.Column("commit_sha", sa.String(100), nullable=True),
        sa.Column("mr_url", sa.String(1000), nullable=True),
        sa.Column("mr_iid", sa.Integer, nullable=True),
        sa.Column(
            "status",
            sa.Enum("branch_created", "committed", "mr_raised", "merged", name="giteventstatus"),
            nullable=False,
            server_default="branch_created",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # build_runs
    op.create_table(
        "build_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("phase_b_run_id", sa.String(36), sa.ForeignKey("phase_b_runs.id"), nullable=False),
        sa.Column("iteration_number", sa.Integer, nullable=False),
        sa.Column("jenkins_build_number", sa.Integer, nullable=True),
        sa.Column("jenkins_job_name", sa.String(500), nullable=True),
        sa.Column(
            "status",
            sa.Enum("queued", "running", "success", "failure", name="buildrunstatus"),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("build_log", sa.Text, nullable=True),
        sa.Column("artifact_path", sa.String(1000), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # deployment_runs
    op.create_table(
        "deployment_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("phase_b_run_id", sa.String(36), sa.ForeignKey("phase_b_runs.id"), nullable=False),
        sa.Column("build_run_id", sa.String(36), sa.ForeignKey("build_runs.id"), nullable=True),
        sa.Column("iteration_number", sa.Integer, nullable=False),
        sa.Column("target_server", sa.String(500), nullable=True),
        sa.Column(
            "status",
            sa.Enum("running", "success", "failure", name="deployrunstatus"),
            nullable=False,
            server_default="running",
        ),
        sa.Column("deploy_log", sa.Text, nullable=True),
        sa.Column("health_check_url", sa.String(500), nullable=True),
        sa.Column("health_check_passed", sa.Boolean, nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # uat_test_cases
    op.create_table(
        "uat_test_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("phase_b_run_id", sa.String(36), sa.ForeignKey("phase_b_runs.id"), nullable=False),
        sa.Column("suite_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("test_id", sa.String(50), nullable=True),
        sa.Column(
            "category",
            sa.Enum("new_feature", "regression", name="testcasecategory"),
            nullable=False,
            server_default="new_feature",
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("preconditions", sa.Text, nullable=True),
        sa.Column("http_method", sa.String(10), nullable=True),
        sa.Column("endpoint", sa.String(500), nullable=True),
        sa.Column("request_headers", sa.JSON, nullable=True),
        sa.Column("request_payload", sa.JSON, nullable=True),
        sa.Column("expected_status", sa.Integer, nullable=True),
        sa.Column("expected_response", sa.JSON, nullable=True),
        sa.Column("pass_criteria", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # uat_test_runs
    op.create_table(
        "uat_test_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("phase_b_run_id", sa.String(36), sa.ForeignKey("phase_b_runs.id"), nullable=False),
        sa.Column("suite_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("iteration_number", sa.Integer, nullable=False),
        sa.Column("base_url", sa.String(500), nullable=True),
        sa.Column("total", sa.Integer, nullable=True),
        sa.Column("passed", sa.Integer, nullable=True),
        sa.Column("failed", sa.Integer, nullable=True),
        sa.Column("skipped", sa.Integer, nullable=True),
        sa.Column(
            "status",
            sa.Enum("running", "completed", name="testrunstatus"),
            nullable=False,
            server_default="running",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # uat_test_results
    op.create_table(
        "uat_test_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("test_run_id", sa.String(36), sa.ForeignKey("uat_test_runs.id"), nullable=False),
        sa.Column("test_case_id", sa.String(36), sa.ForeignKey("uat_test_cases.id"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pass", "fail", "skip", "error", name="testresultstatus"),
            nullable=False,
        ),
        sa.Column("actual_status", sa.Integer, nullable=True),
        sa.Column("actual_response", sa.JSON, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
    )

    # uat_triage_results
    op.create_table(
        "uat_triage_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("test_run_id", sa.String(36), sa.ForeignKey("uat_test_runs.id"), nullable=False),
        sa.Column("test_result_id", sa.String(36), sa.ForeignKey("uat_test_results.id"), nullable=False),
        sa.Column(
            "verdict",
            sa.Enum("code_bug", "test_case_issue", "env_issue", name="triageverdict"),
            nullable=False,
        ),
        sa.Column("ai_reasoning", sa.Text, nullable=True),
        sa.Column(
            "user_override",
            sa.Enum("code_bug", "test_case_issue", "env_issue", name="triageuseroverride"),
            nullable=True,
        ),
        sa.Column(
            "final_verdict",
            sa.Enum("code_bug", "test_case_issue", "env_issue", name="triagefinalverdict"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("uat_triage_results")
    op.drop_table("uat_test_results")
    op.drop_table("uat_test_runs")
    op.drop_table("uat_test_cases")
    op.drop_table("deployment_runs")
    op.drop_table("build_runs")
    op.drop_table("git_events")
    op.drop_table("is_review_results")
    op.drop_table("code_review_results")
    op.drop_table("code_iterations")
    op.drop_table("phase_b_runs")
