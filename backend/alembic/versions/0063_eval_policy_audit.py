# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add eval_policy_audit table for policy change governance.

Revision ID: 0063
Revises: 0062
Create Date: 2026-05-25

Renumbered from 0057 → 0063 on retrofit (collided with 0057_resolver_recommendations).
Chains after 0062_eval_verdicts.
"""
import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()
    if "eval_policy_audit" in tables:
        return

    op.create_table(
        "eval_policy_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("checkpoint_id", sa.String(128), nullable=False),
        sa.Column("old_policy_mode", sa.String(32), nullable=False),
        sa.Column("new_policy_mode", sa.String(32), nullable=False),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_username", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("app_env", sa.String(32), nullable=False, server_default="development"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_eval_policy_audit_checkpoint_id", "eval_policy_audit", ["checkpoint_id"])
    op.create_index("ix_eval_policy_audit_created_at", "eval_policy_audit", ["created_at"])
    op.create_index("ix_eval_policy_audit_actor_user_id", "eval_policy_audit", ["actor_user_id"])
    op.create_index(
        "ix_eval_policy_audit_checkpoint_created",
        "eval_policy_audit",
        ["checkpoint_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()
    if "eval_policy_audit" not in tables:
        return
    op.drop_index("ix_eval_policy_audit_checkpoint_created", table_name="eval_policy_audit")
    op.drop_index("ix_eval_policy_audit_actor_user_id", table_name="eval_policy_audit")
    op.drop_index("ix_eval_policy_audit_created_at", table_name="eval_policy_audit")
    op.drop_index("ix_eval_policy_audit_checkpoint_id", table_name="eval_policy_audit")
    op.drop_table("eval_policy_audit")
