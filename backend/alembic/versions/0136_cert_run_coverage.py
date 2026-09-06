"""cert_runs.coverage — persist the C-1 coverage note per round (CERT-7)

Revision ID: 0136
Revises: 0135
Create Date: 2026-08-31

The case builder's `BuildResult` leads with what is NOT covered — uncoverable
APIs, unconstrained changed fields, §3.1 combination gaps, the baseline
fallback flag — but until now that honesty lived only in a log line. CERT-7's
reporting surface needs it per ROUND, exactly as it stood when the round was
built (the same copy-don't-reference rule as `cert_case_specs.expected`), so
the orchestrator now stamps it onto the run row at persist time.

One nullable JSON column: NULL on legacy rows and on harnesses that do not
build from the registry. 0133/0134 remain reserved for the SIM/ITA column
sets. Additive only. Idempotent + inspector-gated (repo convention).
"""
from alembic import op
import sqlalchemy as sa

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("cert_runs")}
    if "coverage" not in existing:
        op.add_column("cert_runs", sa.Column("coverage", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("cert_runs", "coverage")
