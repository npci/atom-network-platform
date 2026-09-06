"""cert_flow_states — persist the certification lifecycle phase (CERT-0)

Revision ID: 0131
Revises: 0130
Create Date: 2026-08-30

The certification state machine (`precert_engine/state_machine.py`) is driven
by `cert_agent/flow.py`, whose phase previously lived only in process memory —
"a run is a single process". The certification loop (CERT-6) stands on the
phase, the round and the transition history surviving the process, and on
`halted_reason` recording why the loop stopped.

Also adds the CERT-6 round-audit columns to `cert_runs` (`dispatched_by`,
`previous_run_id`, `fix_notification_message_id`) — carried here because 0132
is allocated to case specs/variants and 0133–0135 are pre-allocated to the
SIM/ITA threads (COMBINED_EXECUTION_PLAN §5).

Additive only. Idempotent + inspector-gated (repo convention).
"""
from alembic import op
import sqlalchemy as sa

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "cert_flow_states" not in insp.get_table_names():
        op.create_table(
            "cert_flow_states",
            sa.Column("cflow_id", sa.String(64), primary_key=True),
            sa.Column("change_request_id", sa.String(36), nullable=False),
            sa.Column("partner_id", sa.String(36), nullable=False),
            sa.Column("phase", sa.String(30), nullable=False,
                      server_default="NOT_STARTED"),
            sa.Column("current_round", sa.Integer(), nullable=False,
                      server_default="1"),
            # Append-only [trigger, phase, at] triples, accumulated across
            # rounds — see app/services/cert_agent/flow_store.py.
            sa.Column("history", sa.JSON(), nullable=False),
            sa.Column("halted_reason", sa.String(200), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    insp = sa.inspect(bind)  # re-inspect: the table may have just been created
    existing_idx = {i["name"] for i in insp.get_indexes("cert_flow_states")}
    if "ix_cert_flow_states_change_partner" not in existing_idx:
        op.create_index("ix_cert_flow_states_change_partner", "cert_flow_states",
                        ["change_request_id", "partner_id"])

    existing_cols = {c["name"] for c in insp.get_columns("cert_runs")}
    for col_name, col_type in (
        ("dispatched_by", sa.String(20)),               # operator | auto
        ("previous_run_id", sa.String(36)),
        ("fix_notification_message_id", sa.String(36)),
    ):
        if col_name not in existing_cols:
            op.add_column("cert_runs", sa.Column(col_name, col_type, nullable=True))


def downgrade():
    op.drop_table("cert_flow_states")
    for col_name in ("dispatched_by", "previous_run_id",
                     "fix_notification_message_id"):
        op.drop_column("cert_runs", col_name)
