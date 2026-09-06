# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Record partner acknowledgement of cert run results.

After NPCI ships cert run results to the partner over A2A
(CERT_TEST_RESPONSE), the partner now sends back an explicit
CERT_ACKNOWLEDGEMENT — a non-repudiation receipt that the run results
were not only delivered (SDK-level) but processed and persisted on the
partner side (application-level).

This column stores the application-level ACK timestamp; the existing
a2a_messages audit row holds the wire-level delivery confirmation.

Revision ID: 0049
Revises: 0048
Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("cert_runs")}
    if "partner_acknowledged_at" not in cols:
        op.add_column(
            "cert_runs",
            sa.Column(
                "partner_acknowledged_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("cert_runs")}
    if "partner_acknowledged_at" in cols:
        op.drop_column("cert_runs", "partner_acknowledged_at")
