# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Partner lifecycle state machine — REAL DDL.

Replaces a teammate-branch stub that admitted the original 0028 file was
"lost from disk; only the running container had it." That stub is unsafe —
it doesn't materialise the schema on a fresh DB, so models would reference
columns that don't exist on first ORM access.

This migration creates everything the code path now assumes:

  * Extends `partnertype` enum with `cert_engine` (NPCI's internal cert-agent
    submodule is modelled as a partner so the same A2A plumbing handles
    CERT_TEST_REQUEST / CERT_TEST_RESPONSE round-trips).
  * Extends `assignmentstatus` enum with the ten new lifecycle values
    (received, accepted, applied, tested, ready_for_certification,
    certifying, certified, ready_for_production, in_production,
    withdrawn). Old values stay dormant — Postgres can't drop enum values
    cleanly and we still have legacy rows on prod.
  * Extends `a2ataskttype` with `change_acknowledgement` — partner accepts
    the change → assignment moves to `accepted`.
  * Extends `doccategory` with `npci_xml_spec` — UPI wire-format reference
    XML samples + spec.md.
  * Adds `blocked_at` + `blocked_reason` (Text) to `change_partner_assignments`
    as concurrent flags orthogonal to status.
  * Creates `assignment_status_history` for the audit trail every transition
    must emit (set via `services/assignment_status.set_status`).

Renumbered from 0028 → 0031 to chain after main's HNSW (0028) /
tsvector (0029) / unified-build-deploy (0030) migrations.

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-06
"""
from alembic import op
import sqlalchemy as sa


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


# Enum extensions need raw SQL — alembic doesn't have an op for ADD VALUE.
# Two complications:
#   1. Not every column we logically associate with an enum is actually a
#      Postgres ENUM type. `partner_agents.partner_type` is JSON (multi-
#      value) and `document_chunks.doc_category` is VARCHAR(100) on every
#      env we've seen, so their "matching" enum types may not exist at all.
#   2. Even when the enum exists, its name in pg_catalog can be `partnertype`
#      OR something else if a previous migration created it under a custom
#      name. We probe before mutating.
#
# `IF NOT EXISTS` on the value keeps repeated runs idempotent.
_NEW_ASSIGNMENT_STATUSES = (
    "received",
    "accepted",
    "applied",
    "tested",
    "ready_for_certification",
    "certifying",
    "certified",
    "ready_for_production",
    "in_production",
    "withdrawn",
)


def _enum_exists(bind, enum_name: str) -> bool:
    """Check pg_type for the enum. Returns False if absent or if the bind
    isn't Postgres (e.g. a unit-test SQLite session)."""
    try:
        row = bind.execute(
            sa.text(
                "SELECT 1 FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typname = :name AND t.typtype = 'e' "
                "LIMIT 1"
            ),
            {"name": enum_name},
        ).first()
        return row is not None
    except Exception:
        return False


def _add_enum_value_if_type_exists(bind, enum_name: str, value: str) -> None:
    """ALTER TYPE … ADD VALUE IF NOT EXISTS, but only when the type exists.

    Silently skips when the underlying column isn't an ENUM (e.g.
    `doc_category` is a plain VARCHAR(100), `partner_type` is JSON).
    Postgres ≥12 allows ALTER TYPE ADD VALUE inside a transaction, which
    is what alembic provides. Older versions would need autocommit; if
    you ever target one, switch the bind to AUTOCOMMIT before this call.
    """
    if not _enum_exists(bind, enum_name):
        return
    op.execute(sa.text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'"))


def upgrade() -> None:
    bind = op.get_bind()
    # ── enum extensions (skip silently when the underlying column isn't an enum) ──
    _add_enum_value_if_type_exists(bind, "partnertype",        "cert_engine")
    _add_enum_value_if_type_exists(bind, "a2ataskttype",       "change_acknowledgement")
    _add_enum_value_if_type_exists(bind, "doccategory",        "npci_xml_spec")
    for v in _NEW_ASSIGNMENT_STATUSES:
        _add_enum_value_if_type_exists(bind, "assignmentstatus", v)

    # ── change_partner_assignments — concurrent block flags ──
    # Use IF NOT EXISTS via inspector to stay idempotent against the prod
    # state where these were applied out-of-band.
    insp = sa.inspect(bind)
    existing_cols = {c["name"] for c in insp.get_columns("change_partner_assignments")}
    if "blocked_at" not in existing_cols:
        op.add_column(
            "change_partner_assignments",
            sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "blocked_reason" not in existing_cols:
        op.add_column(
            "change_partner_assignments",
            sa.Column("blocked_reason", sa.Text(), nullable=True),
        )

    # ── assignment_status_history ──
    if not insp.has_table("assignment_status_history"):
        op.create_table(
            "assignment_status_history",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "assignment_id",
                sa.String(length=36),
                sa.ForeignKey("change_partner_assignments.id"),
                nullable=False,
            ),
            sa.Column("from_status", sa.String(length=50), nullable=True),
            sa.Column("to_status", sa.String(length=50), nullable=False),
            sa.Column(
                "actor_user_id",
                sa.String(length=36),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column(
                "actor_partner_id",
                sa.String(length=36),
                sa.ForeignKey("partner_agents.id"),
                nullable=True,
            ),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_assignment_status_history_assignment_id",
            "assignment_status_history",
            ["assignment_id"],
        )
        op.create_index(
            "ix_assignment_status_history_created_at",
            "assignment_status_history",
            ["created_at"],
        )


def downgrade() -> None:
    """Best-effort downgrade.

    Postgres can't drop individual enum values, so the enum extensions
    are intentionally not reversed — leaving them is harmless. We only
    drop the additive columns and the new audit table.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("assignment_status_history"):
        op.drop_index(
            "ix_assignment_status_history_created_at",
            table_name="assignment_status_history",
        )
        op.drop_index(
            "ix_assignment_status_history_assignment_id",
            table_name="assignment_status_history",
        )
        op.drop_table("assignment_status_history")

    existing_cols = {c["name"] for c in insp.get_columns("change_partner_assignments")}
    if "blocked_reason" in existing_cols:
        op.drop_column("change_partner_assignments", "blocked_reason")
    if "blocked_at" in existing_cols:
        op.drop_column("change_partner_assignments", "blocked_at")
