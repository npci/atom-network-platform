"""Script-based Build + UAT runs and log-driven triage reports.

Revision ID: 0137
Revises: 0136
Create Date: 2026-09-02

Phase B rework: the Build trigger now takes the build+deploy script path as a
request parameter (validated against PHASE_B_SCRIPT_ROOT), UAT test-gen and
test-exec collapse into ONE script-based step whose log is the artefact, and
the Triage step becomes an AI pass over those logs plus the dev/tester
walkthrough. Three additive pieces:

  - build_runs.script_path       — which script this build actually ran
  - uat_test_runs.script_path    — same for the combined UAT step
  - uat_test_runs.log            — the UAT script's captured output
  - phase_b_triage_reports       — one row per AI triage invocation

Additive only; NULL on all legacy rows. Idempotent + inspector-gated (repo
convention).
"""
from alembic import op
import sqlalchemy as sa

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    build_cols = {c["name"] for c in insp.get_columns("build_runs")}
    if "script_path" not in build_cols:
        op.add_column("build_runs", sa.Column("script_path", sa.String(1000), nullable=True))

    uat_cols = {c["name"] for c in insp.get_columns("uat_test_runs")}
    if "script_path" not in uat_cols:
        op.add_column("uat_test_runs", sa.Column("script_path", sa.String(1000), nullable=True))
    if "log" not in uat_cols:
        op.add_column("uat_test_runs", sa.Column("log", sa.Text(), nullable=True))

    if "phase_b_triage_reports" not in insp.get_table_names():
        op.create_table(
            "phase_b_triage_reports",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("phase_b_run_id", sa.String(36),
                      sa.ForeignKey("phase_b_runs.id"), nullable=False),
            sa.Column("build_run_id", sa.String(36),
                      sa.ForeignKey("build_runs.id"), nullable=True),
            sa.Column("uat_test_run_id", sa.String(36),
                      sa.ForeignKey("uat_test_runs.id"), nullable=True),
            sa.Column("report", sa.JSON(), nullable=True),
            sa.Column("walkthrough", sa.JSON(), nullable=True),
            sa.Column("created_by", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade():
    op.drop_table("phase_b_triage_reports")
    op.drop_column("uat_test_runs", "log")
    op.drop_column("uat_test_runs", "script_path")
    op.drop_column("build_runs", "script_path")
