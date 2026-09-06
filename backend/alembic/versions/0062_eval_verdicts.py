# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add eval_verdicts table for evaluation harness Phase 1.

Stores one row per checkpoint evaluation run. Rows are append-only.
Retries and overrides insert new rows; previous_verdict_id links them.

Revision ID: 0062
Revises: 0061
Create Date: 2026-05-19

Renumbered from 0056 → 0062 on retrofit: the branch was cut from old main and
revision "0056" collided with 0056_npci_policy. Chains after main's head
0061_document_source; the table is position-independent (FKs only
change_requests.id), so the slot change is purely a rename.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if "eval_verdicts" in tables:
        return

    op.create_table(
        "eval_verdicts",
        sa.Column("id", sa.String(36), primary_key=True),

        # What was evaluated
        sa.Column(
            "change_request_id", sa.String(36),
            sa.ForeignKey("change_requests.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("checkpoint_id", sa.String(128), nullable=False, index=True),
        sa.Column("from_stage", sa.String(64), nullable=False),
        sa.Column("to_stage", sa.String(64), nullable=False),

        # Decision — stored as VARCHAR to avoid Postgres enum DDL complexity
        sa.Column("verdict",      sa.String(16), nullable=False, index=True),
        sa.Column("passed",       sa.Boolean,    nullable=False),
        sa.Column("policy_mode",  sa.String(32), nullable=False),
        sa.Column("confidence",   sa.Float,      nullable=True),

        # Scoring detail
        sa.Column("scores_json",      JSONB, nullable=False, server_default="{}"),
        sa.Column("hard_fail_codes",  JSONB, nullable=False, server_default="[]"),
        sa.Column("warn_codes",       JSONB, nullable=False, server_default="[]"),
        sa.Column("reasons_json",     JSONB, nullable=False, server_default="[]"),

        # Artifact traceability
        sa.Column("source_artifact_ids", JSONB, nullable=False, server_default="[]"),
        sa.Column("target_artifact_ids", JSONB, nullable=False, server_default="[]"),

        # Reproducibility metadata
        sa.Column("rubric_version",        sa.String(64),  nullable=False),
        sa.Column("deterministic_version", sa.String(64),  nullable=False),
        sa.Column("critic_model",          sa.String(128), nullable=True),
        sa.Column("judge_model",           sa.String(128), nullable=True),
        sa.Column("latency_ms",            sa.Integer,     nullable=False, server_default="0"),
        sa.Column("retry_recommended",     sa.Boolean,     nullable=False, server_default="false"),

        # Override / audit
        sa.Column("is_override",          sa.Boolean,     nullable=False, server_default="false"),
        sa.Column("override_actor",       sa.String(128), nullable=True),
        sa.Column("override_reason",      sa.Text,        nullable=True),
        sa.Column(
            "previous_verdict_id", sa.String(36),
            sa.ForeignKey("eval_verdicts.id", ondelete="SET NULL"),
            nullable=True,
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Composite index for the most common read pattern:
    # "latest verdict for this change + checkpoint"
    op.create_index(
        "ix_eval_verdicts_change_checkpoint",
        "eval_verdicts",
        ["change_request_id", "checkpoint_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()
    if "eval_verdicts" in tables:
        op.drop_index("ix_eval_verdicts_change_checkpoint", table_name="eval_verdicts")
        op.drop_table("eval_verdicts")
    bind.execute(sa.text("DROP TYPE IF EXISTS eval_verdict_value"))
    bind.execute(sa.text("DROP TYPE IF EXISTS eval_policy_mode"))
