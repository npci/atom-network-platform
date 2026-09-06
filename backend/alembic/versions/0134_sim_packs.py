"""sim_packs + sim_pack_publications — the Capability Pack store (SIM-2/SIM-5)

Revision ID: 0134
Revises: 0132
Create Date: 2026-08-31

Takes the number COMBINED_EXECUTION_PLAN §5 pre-allocated to exactly these
tables, slotting into the chain by re-pointing revision ids as the
allocation intends. 0133 (result columns) landed with SIM-6 and this now
revises it.

`sim_packs` is the platform-side store AND — per the 2026-08-31 greenfield
decision (the simulator is built new in Python inside this repo, the old Java
stack is forgotten) — the store the simulator runtime reads directly.
`sim_pack_publications` records pushes to simulator instances; a pack
"published" to a simulator that did not store it is the failure that table
exists to make visible.

Additive only. Idempotent + inspector-gated (repo convention).
"""
from alembic import op
import sqlalchemy as sa

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if "sim_packs" not in tables:
        op.create_table(
            "sim_packs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("pack_ref", sa.String(120), nullable=False, unique=True),
            sa.Column("pack_id", sa.String(80), nullable=False),
            sa.Column("change_request_id", sa.String(36), nullable=True),
            sa.Column("base_pack_ref", sa.String(120), nullable=True),
            sa.Column("engine_min", sa.String(20), nullable=False, server_default="1.0"),
            sa.Column("requires", sa.JSON(), nullable=True),
            sa.Column("content", sa.JSON(), nullable=False),
            sa.Column("coverage", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("created_by", sa.String(200), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "sim_pack_publications" not in tables:
        op.create_table(
            "sim_pack_publications",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("pack_ref", sa.String(120), nullable=False),
            sa.Column("pack_id", sa.String(80), nullable=False),
            sa.Column("target", sa.String(500), nullable=False),
            sa.Column("response_status", sa.Integer(), nullable=True),
            sa.Column("echoed_pack_id", sa.String(80), nullable=True),
            sa.Column("published_by", sa.String(200), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
        )

    insp = sa.inspect(bind)
    existing_idx = {i["name"] for i in insp.get_indexes("sim_packs")}
    if "ix_sim_packs_pack_id" not in existing_idx:
        op.create_index("ix_sim_packs_pack_id", "sim_packs", ["pack_id"])
    if "ix_sim_packs_change_request_id" not in existing_idx:
        op.create_index("ix_sim_packs_change_request_id", "sim_packs",
                        ["change_request_id"])


def downgrade():
    op.drop_table("sim_pack_publications")
    op.drop_table("sim_packs")
