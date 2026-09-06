# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase C schema — Partner Collaboration via A2A protocol (10 tables).

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. partner_agents
    op.create_table(
        "partner_agents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("partner_type", sa.String(20), nullable=False),
        sa.Column("endpoint_url", sa.String(1000), nullable=True),
        sa.Column("api_key", sa.String(200), nullable=True),
        sa.Column("api_key_hash", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("agent_card_url", sa.String(1000), nullable=True),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 2. change_partner_assignments
    op.create_table(
        "change_partner_assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="assigned"),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 3. a2a_sessions
    op.create_table(
        "a2a_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False),
        sa.Column("jwt_token_hash", sa.String(200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 4. a2a_messages
    op.create_table(
        "a2a_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=True),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("task_type", sa.String(50), nullable=False),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="sent"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 5. partner_progress
    op.create_table(
        "partner_progress",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("assignment_id", sa.String(36), sa.ForeignKey("change_partner_assignments.id"), nullable=False),
        sa.Column("step", sa.String(30), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 6. negotiation_threads
    op.create_table(
        "negotiation_threads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 7. negotiation_messages
    op.create_table(
        "negotiation_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("negotiation_threads.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("ai_draft", sa.Text, nullable=True),
        sa.Column("approved_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 8. cert_runs
    op.create_table(
        "cert_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False),
        sa.Column("run_number", sa.Integer, nullable=False, server_default="1"),
        sa.Column("total", sa.Integer, nullable=True),
        sa.Column("passed", sa.Integer, nullable=True),
        sa.Column("failed", sa.Integer, nullable=True),
        sa.Column("skipped", sa.Integer, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 9. cert_test_results
    op.create_table(
        "cert_test_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cert_run_id", sa.String(36), sa.ForeignKey("cert_runs.id"), nullable=False),
        sa.Column("test_case_id", sa.String(100), nullable=True),
        sa.Column("direction", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expected_response", sa.JSON, nullable=True),
        sa.Column("actual_response", sa.JSON, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 10. cert_triage
    op.create_table(
        "cert_triage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cert_test_result_id", sa.String(36), sa.ForeignKey("cert_test_results.id"), nullable=False),
        sa.Column("ai_verdict", sa.String(30), nullable=False),
        sa.Column("ai_reasoning", sa.Text, nullable=True),
        sa.Column("user_override", sa.String(50), nullable=True),
        sa.Column("final_verdict", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("cert_triage")
    op.drop_table("cert_test_results")
    op.drop_table("cert_runs")
    op.drop_table("negotiation_messages")
    op.drop_table("negotiation_threads")
    op.drop_table("partner_progress")
    op.drop_table("a2a_messages")
    op.drop_table("a2a_sessions")
    op.drop_table("change_partner_assignments")
    op.drop_table("partner_agents")
