# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic codegen M-C — XSD schema graph + JAXB links + change reports.

THE BOOK v3.3, §13.2 feature migration 3 of 4 (§7):

    xsd_schema_nodes   — one row per schema file
    xsd_schema_edges   — include/import edges between nodes
    xsd_java_links     — element->Java binding with evidence + confidence
    change_reports     — deterministic context/impact report cache (§4/§7)

Idempotent + inspector-gated. Indexes/uniques are in M-D (0070).

Revision ID: 0077
Revises: 0076
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "xsd_schema_nodes" not in tables:
        op.create_table(
            "xsd_schema_nodes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("repo_id", sa.String(36), sa.ForeignKey("code_repos.id"), nullable=False),
            sa.Column("path", sa.String(1000), nullable=False),
            sa.Column("target_namespace", sa.String(500), nullable=True),
            sa.Column("base_commit_sha", sa.String(64), nullable=True),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "xsd_schema_edges" not in tables:
        op.create_table(
            "xsd_schema_edges",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("from_node_id", sa.String(36), sa.ForeignKey("xsd_schema_nodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("to_node_id", sa.String(36), sa.ForeignKey("xsd_schema_nodes.id", ondelete="CASCADE"), nullable=True),
            sa.Column("edge_type", sa.String(20), nullable=False),
            sa.Column("schema_location", sa.String(1000), nullable=True),
            sa.Column("namespace", sa.String(500), nullable=True),
        )

    if "xsd_java_links" not in tables:
        op.create_table(
            "xsd_java_links",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("repo_id", sa.String(36), sa.ForeignKey("code_repos.id"), nullable=False),
            sa.Column("node_id", sa.String(36), sa.ForeignKey("xsd_schema_nodes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("xpath", sa.String(1000), nullable=True),
            sa.Column("symbol_chunk_id_or_path", sa.String(1000), nullable=True),
            sa.Column("source", sa.String(40), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("base_commit_sha", sa.String(64), nullable=True),
            sa.Column("evidence_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "change_reports" not in tables:
        op.create_table(
            "change_reports",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False),
            sa.Column("input_hash", sa.String(64), nullable=False),
            sa.Column("content", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    for t in ("change_reports", "xsd_java_links", "xsd_schema_edges", "xsd_schema_nodes"):
        if t in tables:
            op.drop_table(t)
