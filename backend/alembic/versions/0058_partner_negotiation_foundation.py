# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner negotiation foundation — BRD requirements, round states,
cross-partner clusters, and extended counter_proposal classification fields.

New tables:
  brd_requirements         — PM-configured per-change: mandatory/optional flags
  negotiation_round_states — per (change, partner) round 1/2 deadline tracking
  negotiation_clusters     — cross-partner groupings of similar counter-proposals
  negotiation_cluster_members — maps counter_proposals → clusters

New columns on `counter_proposals`:
  request_category    — structured type (timeline|scope|limits|api_contract|…)
  brd_classification  — mandatory_violation|optional_in_tolerance|optional_escalated
  auto_disposition    — auto_rejected|auto_accepted|escalated_to_pm|pending
  cluster_id          — FK → negotiation_clusters (nullable)

New columns on `change_requests`:
  negotiation_finalized_at — timestamp when PM locked the negotiation
  negotiation_version      — increments when NPCI creates a new version (v2, v3…)

Revision ID: 0058
Revises:    0057
Create Date: 2026-05-26

NOTE: renumbered 0056→0058 to resolve a revision-id collision when the
partner-negotiation branch merged with the feasibility-resolver branch
(both independently used 0056/0057). Chained after the already-applied
0057_resolver_recommendations so live DBs roll forward cleanly.
"""
from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())

    # ── brd_requirements ──────────────────────────────────────────────────────
    if "brd_requirements" not in existing_tables:
        op.create_table(
            "brd_requirements",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False, index=True),
            # Short label shown to PM in the BRD panel (e.g. "Go-live date", "Per-txn limit")
            sa.Column("label", sa.String(200), nullable=False),
            # The actual requirement text from the BRD document
            sa.Column("description", sa.Text, nullable=True),
            # Which request_category this requirement belongs to
            # (timeline|scope|limits|api_contract|dependency|cert_role|general)
            sa.Column("category", sa.String(50), nullable=False, server_default="general"),
            # PM toggle: true = mandatory (violation auto-rejects the CP)
            sa.Column("is_mandatory", sa.Boolean, nullable=False, server_default="false"),
            # JSON config for tolerance evaluation by the classifier agent.
            # Example: {"date_shift_days": 14} means ±14 days auto-accepted;
            # {"percent_change": 10} means ±10% of current value auto-accepted.
            # NULL means the AI decides tolerance case-by-case.
            sa.Column("tolerance_config", sa.JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    # ── negotiation_round_states ──────────────────────────────────────────────
    if "negotiation_round_states" not in existing_tables:
        op.create_table(
            "negotiation_round_states",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False, index=True),
            sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False, index=True),
            # 1 or 2 — enforced at application level; matches MAX_NEGOTIATION_ROUNDS
            sa.Column("round_number", sa.Integer, nullable=False),
            # Starts when partner sends PROPOSAL_ACKNOWLEDGED (round 1) or when
            # NPCI sends a counter-back (round 2)
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            # started_at + 24h (configurable via env NEGOTIATION_ROUND_HOURS)
            sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
            # open | closed_by_pm | silently_accepted | responded
            sa.Column("status", sa.String(30), nullable=False, server_default="open"),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            # When status = silently_accepted, this carries the auto-sent CP id
            sa.Column("silent_acceptance_cp_id", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    # ── negotiation_clusters ──────────────────────────────────────────────────
    if "negotiation_clusters" not in existing_tables:
        op.create_table(
            "negotiation_clusters",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False, index=True),
            # Stable key: f"{request_category}::{topic_slug}" — used to find the
            # right cluster when new CPs arrive so we can add members without
            # creating duplicate clusters.
            sa.Column("cluster_key", sa.String(200), nullable=False),
            # Category all members share (timeline|scope|limits|api_contract|…)
            sa.Column("category", sa.String(50), nullable=False),
            # Short human-readable summary of what the cluster is asking for
            sa.Column("topic_summary", sa.String(500), nullable=True),
            # How many distinct partners are in this cluster
            sa.Column("partner_count", sa.Integer, nullable=False, server_default="0"),
            # AI-generated paragraph summarising all the justifications in this cluster
            sa.Column("ai_summary", sa.Text, nullable=True),
            # AI recommendation: "accept" | "modify" | "reject"
            sa.Column("ai_recommendation", sa.String(20), nullable=True),
            # 0.0 – 1.0; confidence in the recommendation
            sa.Column("confidence_score", sa.Float, nullable=True),
            # PM decision after reviewing: accept | modify | reject | pending
            sa.Column("pm_decision", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("pm_decision_text", sa.Text, nullable=True),
            # Modified value PM approved (JSON, nullable — set on 'modify' decisions)
            sa.Column("pm_modified_value", sa.JSON, nullable=True),
            sa.Column("pm_decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("pm_decided_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
            # Points to another cluster that contradicts this one (conflict alert)
            sa.Column("conflict_with_cluster_id", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    # ── negotiation_cluster_members ───────────────────────────────────────────
    if "negotiation_cluster_members" not in existing_tables:
        op.create_table(
            "negotiation_cluster_members",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("cluster_id", sa.String(36), sa.ForeignKey("negotiation_clusters.id"), nullable=False, index=True),
            sa.Column("counter_proposal_id", sa.String(36), sa.ForeignKey("counter_proposals.id"), nullable=False, index=True),
            sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False),
            sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    # ── counter_proposals: add new classification columns ─────────────────────
    cp_cols = {c["name"] for c in insp.get_columns("counter_proposals")}

    if "request_category" not in cp_cols:
        op.add_column(
            "counter_proposals",
            sa.Column("request_category", sa.String(50), nullable=True),
        )
    if "brd_classification" not in cp_cols:
        op.add_column(
            "counter_proposals",
            # mandatory_violation | optional_in_tolerance | optional_escalated | uncategorized
            sa.Column("brd_classification", sa.String(30), nullable=True),
        )
    if "auto_disposition" not in cp_cols:
        op.add_column(
            "counter_proposals",
            # auto_rejected | auto_accepted | escalated_to_pm | pending
            sa.Column("auto_disposition", sa.String(30), nullable=True, server_default="pending"),
        )
    if "cluster_id" not in cp_cols:
        op.add_column(
            "counter_proposals",
            sa.Column("cluster_id", sa.String(36), nullable=True),
        )

    # ── change_requests: governance lock + versioning ─────────────────────────
    cr_cols = {c["name"] for c in insp.get_columns("change_requests")}

    if "negotiation_finalized_at" not in cr_cols:
        op.add_column(
            "change_requests",
            sa.Column("negotiation_finalized_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "negotiation_version" not in cr_cols:
        op.add_column(
            "change_requests",
            # Starts at 1; incremented each time NPCI publishes a new version
            sa.Column("negotiation_version", sa.Integer, nullable=False, server_default="1"),
        )


def downgrade() -> None:
    op.drop_table("negotiation_cluster_members")
    op.drop_table("negotiation_clusters")
    op.drop_table("negotiation_round_states")
    op.drop_table("brd_requirements")

    for col in ("request_category", "brd_classification", "auto_disposition", "cluster_id"):
        op.drop_column("counter_proposals", col)

    for col in ("negotiation_finalized_at", "negotiation_version"):
        op.drop_column("change_requests", col)
