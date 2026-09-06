# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Blockers table — Tier 2 of the message-intent split.

Promotes BLOCKER from a freeform query to a structured workflow with
its own table + state machine. While `status='open'`, the assignment
is held in BLOCKED state; PM resolves via /resolve which sends
BLOCKER_RESOLUTION back to the partner (optionally with a patched
artifact reference).

Schema:
  blockers:
    id                          uuid pk
    change_request_id           uuid fk
    partner_id                  uuid fk
    assignment_id               uuid fk
    blocker_id                  varchar(64)        partner-supplied (e.g. BLK-001)
    severity                    enum               critical/high/medium/low
    status                      enum               open/resolved/wontfix
    description                 text
    impact                      text nullable
    investigation_done          jsonb nullable     list of investigation steps
    options_considered          jsonb nullable     list of {option, eta, impact}
    requested_action_from_npci  text nullable
    payload                     jsonb nullable     full inbound payload for audit
    created_at                  timestamptz
    resolved_at                 timestamptz nullable
    resolved_by                 uuid fk users.id nullable
    resolution_action           text nullable      which option was picked
    resolution_text             text nullable
    resolution_artifact_ref     varchar(500) nullable

Indexed on (change_request_id, partner_id) for the dashboard's
"open blockers per partner" query.

Revision ID: 0045
Revises: 0044
Create Date: 2026-05-10
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


_SEVERITY_ENUM = sa.Enum(
    "critical", "high", "medium", "low",
    name="blockerseverity",
    create_type=True,
)
_STATUS_ENUM = sa.Enum(
    "open", "resolved", "wontfix",
    name="blockerstatus",
    create_type=True,
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "blockers" in insp.get_table_names():
        return

    op.create_table(
        "blockers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_request_id", sa.String(36), sa.ForeignKey("change_requests.id"), nullable=False, index=True),
        sa.Column("partner_id", sa.String(36), sa.ForeignKey("partner_agents.id"), nullable=False, index=True),
        sa.Column("assignment_id", sa.String(36), sa.ForeignKey("change_partner_assignments.id"), nullable=False, index=True),
        sa.Column("blocker_id", sa.String(64), nullable=False),
        sa.Column("severity", _SEVERITY_ENUM, nullable=False),
        sa.Column("status", _STATUS_ENUM, nullable=False, server_default="open"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("impact", sa.Text(), nullable=True),
        sa.Column("investigation_done", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.Column("options_considered", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.Column("requested_action_from_npci", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolution_action", sa.Text(), nullable=True),
        sa.Column("resolution_text", sa.Text(), nullable=True),
        sa.Column("resolution_artifact_ref", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "blockers" in insp.get_table_names():
        op.drop_table("blockers")
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS blockerstatus")
        op.execute("DROP TYPE IF EXISTS blockerseverity")
