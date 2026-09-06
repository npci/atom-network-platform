# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""multi-role users: user_roles assignment table

Users can be assigned MULTIPLE roles and switch their ACTIVE role (`users.role`)
among them. This table holds the assignable set (composite PK user_id+role).
Backfill seeds each user's current active role so nothing regresses — every
existing user ends up with exactly one assigned role equal to their active role.

Idempotent + inspector-gated (safe to re-run). On Postgres the `userrole` enum
type already exists (created by `users.role`), so we reuse it with
`create_type=False`; SQLite stores the enum as VARCHAR.

Revision ID: 0107
Revises: 0106
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None

_ROLE_VALUES = [
    "product_owner", "product_manager", "tech_lead",
    "infosec_reviewer", "risk_reviewer", "admin",
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_roles" in insp.get_table_names():
        return  # already applied

    if bind.dialect.name == "postgresql":
        role_type = postgresql.ENUM(*_ROLE_VALUES, name="userrole", create_type=False)
    else:
        role_type = sa.String(32)  # SQLite (tests) stores enums as VARCHAR

    op.create_table(
        "user_roles",
        sa.Column(
            "user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("role", role_type, primary_key=True),
    )

    # Backfill: each existing user's current active role becomes an assigned role.
    rows = bind.execute(sa.text("SELECT id, role FROM users")).fetchall()
    if rows:
        op.bulk_insert(
            sa.table(
                "user_roles",
                sa.column("user_id", sa.String),
                sa.column("role", sa.String),
            ),
            [{"user_id": r[0], "role": r[1]} for r in rows],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_roles" in insp.get_table_names():
        op.drop_table("user_roles")
