# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""admin_action_audit — audit trail for state-changing admin actions

Revision ID: 0128
Revises: 0127
Create Date: 2026-08-24

Closes THREAT_MODEL.md T8 ("No comprehensive admin ACTION audit log (vs.
auth-event audit)") — distinct from the existing `auth_audit` table,
which records AUTHENTICATION events only. This table records WHAT an
authenticated admin did: action name, resource type/id, and a
before/after snapshot of the changed fields (secret-bearing fields
redacted at the application layer — see app/core/admin_action_audit.py).

Also carries the HTTP-level columns (`http_method`, `path`,
`status_code`, `source`) used by the generic middleware writer
(app/core/admin_action_audit_middleware.py), which records EVERY
mutating /api/admin/* request so audit coverage does not depend on each
endpoint remembering to call `record()` — the actual coverage gap T8
names. `source` distinguishes rich endpoint-authored rows from generic
middleware-authored ones. These columns are added here (rather than in a
follow-on revision) because 0128 has not shipped to any environment yet;
the `_ensure_columns` helper below still adds them idempotently for any
local database that already ran an earlier form of this revision.

Additive only. Idempotent + inspector-gated (repo convention).
"""
from alembic import op
import sqlalchemy as sa

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "admin_action_audit" not in insp.get_table_names():
        op.create_table(
            "admin_action_audit",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("username", sa.String(150), nullable=True),
            sa.Column("action", sa.String(80), nullable=False),
            sa.Column("resource_type", sa.String(64), nullable=True),
            sa.Column("resource_id", sa.String(36), nullable=True),
            sa.Column("before", sa.JSON(), nullable=True),
            sa.Column("after", sa.JSON(), nullable=True),
            sa.Column("ip", sa.String(64), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            # HTTP-level facts for generically-recorded (middleware) rows.
            sa.Column("http_method", sa.String(10), nullable=True),
            sa.Column("path", sa.String(500), nullable=True),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("source", sa.String(20), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Idempotently add the HTTP-level columns for any database that already
    # created this table from an earlier form of this revision (local dev).
    insp = sa.inspect(bind)  # re-inspect: the table may have just been created
    existing_cols = {c["name"] for c in insp.get_columns("admin_action_audit")}
    for col_name, col_type in (
        ("http_method", sa.String(10)),
        ("path", sa.String(500)),
        ("status_code", sa.Integer()),
        ("source", sa.String(20)),
    ):
        if col_name not in existing_cols:
            op.add_column("admin_action_audit", sa.Column(col_name, col_type, nullable=True))

    existing_idx = {i["name"] for i in insp.get_indexes("admin_action_audit")}
    for col in ("user_id", "action", "resource_id", "source"):
        name = f"ix_admin_action_audit_{col}"
        if name not in existing_idx:
            op.create_index(name, "admin_action_audit", [col])


def downgrade():
    op.drop_table("admin_action_audit")
