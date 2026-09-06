# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Record cert completion signoff ACK on cert_runs.

When a cert run is all-PASS (failed=0, passed=total>0), NPCI emits an
explicit `cert_completion_signoff` A2A task to the partner. The partner
acknowledges it; this column records the ACK timestamp.

Distinct from `partner_acknowledged_at` (added in 0049), which records
the receipt of cert_test_response. Signoff is a separate, stronger
non-repudiation event: NPCI is asserting "this run is final and the
partner is certified".

Revision ID: 0050
Revises: 0049
Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("cert_runs")}
    if "completion_signed_off_at" not in cols:
        op.add_column(
            "cert_runs",
            sa.Column(
                "completion_signed_off_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("cert_runs")}
    if "completion_signed_off_at" in cols:
        op.drop_column("cert_runs", "completion_signed_off_at")
