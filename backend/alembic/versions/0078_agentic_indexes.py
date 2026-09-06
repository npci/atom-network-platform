# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic codegen M-D — indexes, uniques, and the active-run constraint.

THE BOOK v3.3, §13.2 feature migration 4 of 4. Adds the FK / lookup indexes
and uniqueness constraints for the tables created in M-A..M-C, kept separate so
each concern is independently reversible:

    * FK + lookup indexes on every agentic table
    * unique (run_id, seq) on agentic_events       (append-only ordering)
    * unique (repo_id, module_path) on module_context
    * unique (change_request_id, input_hash) on change_reports
    * xsd_java_links.confidence index               (§13.2)
    * PARTIAL-UNIQUE active run on agentic_runs(change_request_id)
      WHERE status='active'  — at most one live run per change (§3)

Idempotent + inspector-gated (guarded by existing index names).

Revision ID: 0078
Revises: 0077
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


# (index_name, table, [columns], unique)
_INDEXES = [
    ("ix_agentic_runs_change_request_id", "agentic_runs", ["change_request_id"], False),
    ("ix_agentic_run_repos_run_id", "agentic_run_repos", ["run_id"], False),
    ("ix_agentic_run_repos_repo_id", "agentic_run_repos", ["repo_id"], False),
    ("ix_agentic_events_run_id", "agentic_events", ["run_id"], False),
    ("uq_agentic_events_run_seq", "agentic_events", ["run_id", "seq"], True),
    ("ix_change_manifests_run_id", "change_manifests", ["run_id"], False),
    ("ix_verification_runs_run_id", "verification_runs", ["run_id"], False),
    ("ix_review_findings_run_id", "review_findings", ["run_id"], False),
    ("ix_module_context_repo_id", "module_context", ["repo_id"], False),
    ("uq_module_context_repo_path", "module_context", ["repo_id", "module_path"], True),
    ("ix_repo_path_context_repo_id", "repo_path_context", ["repo_id"], False),
    ("ix_xsd_schema_nodes_repo_id", "xsd_schema_nodes", ["repo_id"], False),
    ("ix_xsd_schema_edges_from_node_id", "xsd_schema_edges", ["from_node_id"], False),
    ("ix_xsd_schema_edges_to_node_id", "xsd_schema_edges", ["to_node_id"], False),
    ("ix_xsd_java_links_repo_id", "xsd_java_links", ["repo_id"], False),
    ("ix_xsd_java_links_node_id", "xsd_java_links", ["node_id"], False),
    ("ix_xsd_java_links_confidence", "xsd_java_links", ["confidence"], False),
    ("ix_change_reports_change_request_id", "change_reports", ["change_request_id"], False),
    ("uq_change_reports_cr_input", "change_reports", ["change_request_id", "input_hash"], True),
]

_ACTIVE_RUN_IX = "uq_agentic_runs_active"


def _existing(insp, table) -> set[str]:
    names = {ix["name"] for ix in insp.get_indexes(table)}
    names |= {uc["name"] for uc in insp.get_unique_constraints(table)}
    return names


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    cache: dict[str, set[str]] = {}
    for name, table, cols, unique in _INDEXES:
        if table not in tables:
            continue
        existing = cache.setdefault(table, _existing(insp, table))
        if name not in existing:
            op.create_index(name, table, cols, unique=unique)
            existing.add(name)

    # Partial-unique active run: at most one non-terminal run per change (§3).
    # SQLite (>=3.8) and Postgres both support partial indexes.
    if "agentic_runs" in tables:
        existing = cache.setdefault("agentic_runs", _existing(insp, "agentic_runs"))
        if _ACTIVE_RUN_IX not in existing:
            op.create_index(
                _ACTIVE_RUN_IX,
                "agentic_runs",
                ["change_request_id"],
                unique=True,
                postgresql_where=sa.text("status = 'active'"),
                sqlite_where=sa.text("status = 'active'"),
            )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "agentic_runs" in tables:
        existing = _existing(insp, "agentic_runs")
        if _ACTIVE_RUN_IX in existing:
            op.drop_index(_ACTIVE_RUN_IX, table_name="agentic_runs")

    for name, table, _cols, _unique in reversed(_INDEXES):
        if table not in tables:
            continue
        if name in _existing(insp, table):
            op.drop_index(name, table_name=table)
