"""integration_exchanges — one row per tunnelled HTTP exchange (ITA I-9)

Revision ID: 0135
Revises: 0132
Create Date: 2026-08-31

The observability half of the tunnel: a failed exchange must be diagnosable
from the row alone (alias, method/path, bytes each way, timing, the §5.2
error code), and `correlation_id` threads the Simulator's call, the A2A hop
and this row (architecture review A12).

Takes the number COMBINED_EXECUTION_PLAN §5 pre-allocated to exactly this
table. `down_revision` was 0132 while 0134 (sim_packs) was unbuilt; 0134
landed 2026-08-31 and this now revises it, per the slot-between re-pointing
the allocation intends. 0133 (pack_ref/pack_id/npci_mode/partner_mode) stays
reserved for SIM-6/I-6b and will slot in the same way. Additive only.
Idempotent + inspector-gated (repo convention).
"""
from alembic import op
import sqlalchemy as sa

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "integration_exchanges" not in insp.get_table_names():
        op.create_table(
            "integration_exchanges",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("exchange_id", sa.String(64), nullable=False),
            sa.Column("direction", sa.String(10), nullable=False),
            sa.Column("alias", sa.String(100), nullable=False),
            sa.Column("method", sa.String(10), nullable=False),
            sa.Column("path", sa.String(1000), nullable=False),
            sa.Column("status", sa.Integer(), nullable=True),
            sa.Column("error_code", sa.String(40), nullable=True),
            sa.Column("request_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("response_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("elapsed_ms", sa.Integer(), nullable=True),
            sa.Column("dropped_headers", sa.JSON(), nullable=True),
            sa.Column("correlation_id", sa.String(64), nullable=True),
            sa.Column("cert_context", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    insp = sa.inspect(bind)  # re-inspect: the table may have just been created
    existing_idx = {i["name"] for i in insp.get_indexes("integration_exchanges")}
    if "ix_integration_exchanges_exchange_id" not in existing_idx:
        op.create_index("ix_integration_exchanges_exchange_id",
                        "integration_exchanges", ["exchange_id"])


def downgrade():
    op.drop_table("integration_exchanges")
