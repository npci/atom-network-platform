# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic codegen M-B — repo build graph + workspace + module knowledge.

THE BOOK v3.3, §13.2 feature migration 2 of 4:

    code_repos.role / depends_on / locations   (inter-repo build graph, §20/§5)
    change_requests.agentic_enabled            (per-change opt-in, §1.3)
    module_context                             (index-time hierarchical context, §19)
    repo_path_context                          (MODULE_NOTES DB fallback, §14)

NOTE — index-time commit provenance (§5 ``indexed_commit_sha``) is NOT added
here: the existing ``code_repo_state.last_ingested_sha`` (migration 0065)
already records the last ingested commit SHA per repo, so stale-index
detection reuses it rather than duplicating a column. Deviation from the
(now stale) plan, justified by ground truth.

Idempotent + inspector-gated. Indexes/uniques are in M-D (0070).

Revision ID: 0076
Revises: 0075
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    is_pg = bind.dialect.name == "postgresql"
    bool_false = sa.text("false") if is_pg else sa.text("0")
    tables = set(insp.get_table_names())

    code_repo_cols = {c["name"] for c in insp.get_columns("code_repos")}
    if "role" not in code_repo_cols:
        op.add_column("code_repos", sa.Column("role", sa.String(20), nullable=True))
    if "depends_on" not in code_repo_cols:
        op.add_column("code_repos", sa.Column("depends_on", sa.JSON(), nullable=True))
    if "locations" not in code_repo_cols:
        op.add_column("code_repos", sa.Column("locations", sa.JSON(), nullable=True))

    cr_cols = {c["name"] for c in insp.get_columns("change_requests")}
    if "agentic_enabled" not in cr_cols:
        op.add_column(
            "change_requests",
            sa.Column("agentic_enabled", sa.Boolean(), nullable=False, server_default=bool_false),
        )

    if "module_context" not in tables:
        op.create_table(
            "module_context",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("repo_id", sa.String(36), sa.ForeignKey("code_repos.id"), nullable=False),
            sa.Column("module_path", sa.String(1000), nullable=False),
            sa.Column("parent_module_path", sa.String(1000), nullable=True),
            sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("key_types", sa.JSON(), nullable=True),
            sa.Column("entry_points", sa.JSON(), nullable=True),
            sa.Column("functional_flow", sa.Text(), nullable=True),
            sa.Column("conventions", sa.Text(), nullable=True),
            sa.Column("gotchas", sa.Text(), nullable=True),
            sa.Column("why", sa.Text(), nullable=True),
            sa.Column("java_version", sa.String(20), nullable=True),
            sa.Column("depends_on", sa.JSON(), nullable=True),
            sa.Column("base_commit_sha", sa.String(64), nullable=True),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "repo_path_context" not in tables:
        op.create_table(
            "repo_path_context",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("repo_id", sa.String(36), sa.ForeignKey("code_repos.id"), nullable=False),
            sa.Column("path", sa.String(1000), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "repo_path_context" in tables:
        op.drop_table("repo_path_context")
    if "module_context" in tables:
        op.drop_table("module_context")

    cr_cols = {c["name"] for c in insp.get_columns("change_requests")}
    if "agentic_enabled" in cr_cols:
        op.drop_column("change_requests", "agentic_enabled")

    code_repo_cols = {c["name"] for c in insp.get_columns("code_repos")}
    for col in ("locations", "depends_on", "role"):
        if col in code_repo_cols:
            op.drop_column("code_repos", col)
