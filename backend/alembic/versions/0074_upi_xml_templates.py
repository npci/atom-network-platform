# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add upi_xml_templates — cache for cert-simulator XML templates.

Stores the Mustache XML template per (api_name, flow_code). Three sources:
  catalog  — mirror of cert-agent's built-in templates (informational; resolver
             reads from `xml_template_resolver._CATALOG` for these directly)
  llm      — drafted by `xml_template_generator`. `approved_at IS NULL` until
             an operator reviews + clicks Approve in the SyncDiffModal.
             /cert-simulator/apply refuses to register flows for unapproved
             LLM templates.
  operator — manually authored or edited via the SyncDiffModal. Approved
             implicitly on submit (the form action is the approval).

Idempotent inspector-gated shape  .

Revision ID: 0074
Revises: 0073
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "upi_xml_templates" not in insp.get_table_names():
        op.create_table(
            "upi_xml_templates",
            sa.Column("api_name", sa.String(64), primary_key=True),
            sa.Column("flow_code", sa.String(32), nullable=False),
            sa.Column("xml_template", sa.Text, nullable=False),
            sa.Column(
                "placeholders_used",
                sa.JSON().with_variant(
                    sa.dialects.postgresql.JSONB(astext_type=sa.Text()), "postgresql"
                ),
                nullable=False,
            ),
            # 'catalog' | 'llm' | 'operator'
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("approved_by", sa.String(64), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_upi_xml_templates_flow_code",
            "upi_xml_templates",
            ["flow_code"],
        )
        # Partial unique guard: at most one unapproved LLM draft per api_name.
        # Approved rows are immutable from this table's perspective (operator
        # edits create a new row with source='operator').
        op.create_index(
            "ix_upi_xml_templates_pending_llm",
            "upi_xml_templates",
            ["api_name"],
            unique=True,
            postgresql_where=sa.text("source = 'llm' AND approved_at IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "upi_xml_templates" in insp.get_table_names():
        op.drop_index("ix_upi_xml_templates_pending_llm", table_name="upi_xml_templates")
        op.drop_index("ix_upi_xml_templates_flow_code", table_name="upi_xml_templates")
        op.drop_table("upi_xml_templates")
