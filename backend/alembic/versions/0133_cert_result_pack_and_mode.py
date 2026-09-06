"""cert_runs + cert_test_results: pack_ref/pack_id + the mode axis (SIM-6/I-6b)

Revision ID: 0133
Revises: 0132
Create Date: 2026-08-31

The shared migration COMBINED_EXECUTION_PLAN §5 allocated to whichever of
SIM-6 / I-6b landed first — one migration, both threads' columns. SIM-6
landed first. `pack_ref`/`pack_id`: every result records which contract
graded it (§3.4 — a multi-round certification against different packs is
unreadable without it). `npci_mode`/`partner_mode`: which class executed each
side (simulator | application) — I-6b layers the mode axis on the harness
axis, never merging them into one enum.

Slots BETWEEN 0132 and 0134 by re-pointing 0134's down_revision, exactly as
the allocation intends. Additive only. Idempotent + inspector-gated.
"""
from alembic import op
import sqlalchemy as sa

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("pack_ref", sa.String(120), nullable=True),
    sa.Column("pack_id", sa.String(80), nullable=True),
    sa.Column("npci_mode", sa.String(20), nullable=True),
    sa.Column("partner_mode", sa.String(20), nullable=True),
)


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in ("cert_runs", "cert_test_results"):
        existing = {c["name"] for c in insp.get_columns(table)}
        for col in _COLUMNS:
            if col.name not in existing:
                op.add_column(table, col._copy())


def downgrade():
    for table in ("cert_runs", "cert_test_results"):
        for col in _COLUMNS:
            op.drop_column(table, col.name)
