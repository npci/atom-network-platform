# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Initial schema — all tables

Revision ID: 0001
Revises:
Create Date: 2026-04-12
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # users
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "product_owner", "product_manager", "tech_lead",
                "infosec_reviewer", "risk_reviewer", "admin",
                name="userrole",
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # change_requests
    op.create_table(
        "change_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("initial_prompt", sa.Text, nullable=False),
        sa.Column("enhanced_prompt", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "prompt_enhancement", "research", "canvas", "brd",
                "tech_spec", "xsd", "product_kit", "completed",
                name="changestatus",
            ),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # conversations
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column(
            "module",
            sa.Enum(
                "prompt_enhancer", "researcher", "canvas", "brd",
                "tech_spec", "xsd", "product_kit",
                name="conversationmodule",
            ),
            nullable=False,
        ),
        sa.Column("role", sa.Enum("user", "assistant", name="messagerole"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # research_outputs
    op.create_table(
        "research_outputs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("market_research", sa.Text, nullable=True),
        sa.Column("product_knowledge", sa.Text, nullable=True),
        sa.Column("rbi_compliance", sa.Text, nullable=True),
        sa.Column("combined_report", sa.Text, nullable=True),
        sa.Column("version", sa.Integer, default=1, nullable=False),
        sa.Column("status", sa.Enum("draft", "approved", name="artifactstatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # product_canvases
    op.create_table(
        "product_canvases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("version", sa.Integer, default=1, nullable=False),
        sa.Column("status", sa.Enum("draft", "approved", name="artifactstatus2"), nullable=False),
        sa.Column("approved_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # brds
    op.create_table(
        "brds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("file_path", sa.String(1000), nullable=True),
        sa.Column("version", sa.Integer, default=1, nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "submitted", "revision", "approved", name="brdstatus"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # approvals
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "artifact_type",
            sa.Enum("brd", "tech_spec", "xsd", "product_canvas", name="approvalartifacttype"),
            nullable=False,
        ),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("approver_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", name="approvalstatus"),
            nullable=False,
        ),
        sa.Column("comments", sa.Text, nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # tech_specs
    op.create_table(
        "tech_specs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("file_path", sa.String(1000), nullable=True),
        sa.Column("version", sa.Integer, default=1, nullable=False),
        sa.Column("status", sa.Enum("draft", "approved", name="artifactstatus3"), nullable=False),
        sa.Column("approved_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # xsds
    op.create_table(
        "xsds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("file_path", sa.String(1000), nullable=True),
        sa.Column("version", sa.Integer, default=1, nullable=False),
        sa.Column("is_required", sa.Boolean, default=False, nullable=False),
        sa.Column("status", sa.Enum("draft", "downloaded", name="xsdstatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # product_kit_documents
    op.create_table(
        "product_kit_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column(
            "doc_type",
            sa.Enum(
                "product_doc", "product_deck", "promo_video", "explainer_video",
                "faq", "cert_test_cases", "circular", "manifest", "prototype_screens",
                name="productkitdoctype",
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("file_path", sa.String(1000), nullable=True),
        sa.Column("version", sa.Integer, default=1, nullable=False),
        sa.Column("status", sa.Enum("draft", "approved", name="artifactstatus4"), nullable=False),
        sa.Column("approved_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # feedback
    op.create_table(
        "feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
        sa.Column("module", sa.String(100), nullable=False),
        sa.Column("artifact_id", sa.String(36), nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # notifications
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column(
            "type",
            sa.Enum("approval_request", "approval_done", "revision_ready", "info", name="notificationtype"),
            nullable=False,
        ),
        sa.Column("related_id", sa.String(36), nullable=True),
        sa.Column("is_read", sa.Boolean, default=False, nullable=False),
        sa.Column("email_sent", sa.Boolean, default=False, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # document_chunks (pgvector)
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_file", sa.String(1000), nullable=False),
        sa.Column(
            "doc_category",
            sa.Enum(
                "rbi_guideline", "upi_product_doc", "past_brd", "api_spec", "xsd",
                name="doccategory",
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", sa.Text, nullable=True),  # stored as vector(1536) via pgvector
        sa.Column("chunk_index", sa.Integer, default=0, nullable=False),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Apply pgvector type to the embedding column post-creation
    op.execute("ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(1536) USING embedding::vector")

    # Indexes
    op.create_index("ix_change_requests_created_by", "change_requests", ["created_by"])
    op.create_index("ix_change_requests_status", "change_requests", ["status"])
    op.create_index("ix_conversations_change_request_id", "conversations", ["change_request_id"])
    op.create_index("ix_approvals_approver_id", "approvals", ["approver_id"])
    op.create_index("ix_approvals_artifact_id", "approvals", ["artifact_id"])
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_is_read", "notifications", ["is_read"])
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding ON document_chunks USING ivfflat (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_table("document_chunks")
    op.drop_table("notifications")
    op.drop_table("feedback")
    op.drop_table("product_kit_documents")
    op.drop_table("xsds")
    op.drop_table("tech_specs")
    op.drop_table("approvals")
    op.drop_table("brds")
    op.drop_table("product_canvases")
    op.drop_table("research_outputs")
    op.drop_table("conversations")
    op.drop_table("change_requests")
    op.drop_table("users")

    for enum_name in [
        "userrole", "changestatus", "conversationmodule", "messagerole",
        "artifactstatus", "artifactstatus2", "artifactstatus3", "artifactstatus4",
        "brdstatus", "approvalartifacttype", "approvalstatus", "xsdstatus",
        "productkitdoctype", "notificationtype", "doccategory",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")

    op.execute("DROP EXTENSION IF EXISTS vector")
