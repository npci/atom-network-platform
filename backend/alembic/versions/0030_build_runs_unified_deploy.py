# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Unified build+deploy on `build_runs` — Session 23.

Adds columns to `build_runs` so a single row captures the full host-side
build+deploy+startup pipeline (clone two repos, mvn package, copy artifacts,
start services). The legacy `build_log` column keeps the unified log; the
new `deploy_log` / `startup_log` hold the slices the parser carved out for
the UI's three collapsible sections.

Why on `build_runs` (not `deployment_runs`): the operator's host script
runs build and deploy together and we no longer have a separate "deploy
button" step in Phase B. Keeping all three log slices on the same row
avoids a join + matches the new single-step UX.

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-05
"""
from alembic import op
import sqlalchemy as sa


revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Inputs (audit + retry parity)
    op.add_column("build_runs", sa.Column("core_branch", sa.String(length=200), nullable=True))
    op.add_column("build_runs", sa.Column("upi2_branch", sa.String(length=200), nullable=True))
    op.add_column("build_runs", sa.Column("host", sa.String(length=200), nullable=True))

    # Carved-out log sections — UI shows three collapsibles.
    op.add_column("build_runs", sa.Column("deploy_log", sa.Text(), nullable=True))
    op.add_column("build_runs", sa.Column("startup_log", sa.Text(), nullable=True))

    # Parsed structured outcomes for the UI tables.
    # `deployed_artifacts`: list[{name, path}] for artifacts copied into the deploy tree.
    # `services_started`:   list[{name, pid}]  for services confirmed via ps -ef.
    op.add_column("build_runs", sa.Column("deployed_artifacts", sa.JSON(), nullable=True))
    op.add_column("build_runs", sa.Column("services_started", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("build_runs", "services_started")
    op.drop_column("build_runs", "deployed_artifacts")
    op.drop_column("build_runs", "startup_log")
    op.drop_column("build_runs", "deploy_log")
    op.drop_column("build_runs", "host")
    op.drop_column("build_runs", "upi2_branch")
    op.drop_column("build_runs", "core_branch")
