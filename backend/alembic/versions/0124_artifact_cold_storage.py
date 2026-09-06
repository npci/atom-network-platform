# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""artifact_cold_storage_manifest — tracks compressed copies of old artifacts

Revision ID: 0124
Revises: 0123
Create Date: 2026-08-21

Closes architecture review Finding #8 ("No Data Tiering for Large
Payloads") via a NON-DESTRUCTIVE, additive design: this table only
records that a compressed COPY of a row's content was written to
workspace-local cold storage. It never touches the source table's
columns — `tech_specs.content`, `brds.content`, `a2a_messages.payload`,
etc. are left completely unmodified, so every existing read path keeps
working exactly as before with zero risk of a regression.

Idempotent + inspector-gated (repo convention — see 0087/0123 for the
same pattern).
"""
from alembic import op
import sqlalchemy as sa

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "artifact_cold_storage" not in insp.get_table_names():
        op.create_table(
            "artifact_cold_storage",
            sa.Column("id", sa.String(36), primary_key=True),
            # Which source table + row this manifest entry describes.
            # Not a foreign key on purpose: the source table varies
            # (tech_specs / brds / a2a_messages / llm_usage_records...)
            # and this table must survive even if the source row is later
            # deleted by an unrelated cleanup process.
            sa.Column("source_table", sa.String(64), nullable=False),
            sa.Column("source_id", sa.String(36), nullable=False),
            sa.Column("change_request_id", sa.String(36), nullable=True),
            # Where the compressed copy lives, relative to
            # settings.artifact_coldstore_dir.
            sa.Column("coldstore_path", sa.String(500), nullable=False),
            sa.Column("original_size_bytes", sa.Integer(), nullable=True),
            sa.Column("compressed_size_bytes", sa.Integer(), nullable=True),
            sa.Column("compressed_at", sa.DateTime(timezone=True), nullable=False),
            # Set by the (separate, flag-gated) archive-eligibility sweep once
            # a coldstore entry has aged past the archive threshold. This is
            # a FLAG, not an action — nothing in this codebase moves data to
            # external archive storage automatically; an operator/ops
            # process consumes rows where this is true.
            sa.Column("ready_for_archive", sa.Boolean(), nullable=False,
                     server_default=sa.text("false")),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        )
    existing_idx = {i["name"] for i in insp.get_indexes("artifact_cold_storage")} \
        if "artifact_cold_storage" in insp.get_table_names() else set()
    for col in ("source_table", "source_id", "change_request_id", "compressed_at", "ready_for_archive"):
        name = f"ix_artifact_cold_storage_{col}"
        if name not in existing_idx:
            op.create_index(name, "artifact_cold_storage", [col])


def downgrade():
    op.drop_table("artifact_cold_storage")
