# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Multi-repo support for Phase B — `phase_b_run_repos` join table.

Each Phase B run can now span multiple registered `CodeRepo` rows, with
per-run branch override and per-repo MR tracking. Code-gen output is split
across repos by file marker prefix `[repo-label]`, and one MR is opened
per repo at git-push time.

Schema:
    phase_b_run_repos
        id              uuid PK
        run_id          uuid FK → phase_b_runs(id) ON DELETE CASCADE
        repo_id         uuid FK → code_repos(id)
        branch          varchar(200) — per-run branch name (same name across
                        all repos in a run by default; operators can override)
        mr_url          varchar(1000) — set after push_to_gitlab succeeds
        mr_iid          integer       — GitLab merge_request iid
        mr_state        varchar(40)   — "opened" | "merged" | "closed" | NULL
        created_at      timestamptz
        updated_at      timestamptz
        UNIQUE(run_id, repo_id)

Backfill:
    For every existing phase_b_runs row where gitlab_repo is set, look up
    the matching code_repos.id by `code_repos.gitlab_repo` string match and
    insert one phase_b_run_repos row preserving the original branch + MR
    metadata. Rows whose gitlab_repo doesn't resolve to a registered repo
    are left orphaned in legacy form (the singular columns stay valid for
    display) — operators can re-run the affected CRs through the new
    multi-repo flow if they want richer tracking.

The legacy `phase_b_runs.gitlab_repo` and `gitlab_branch` columns are KEPT
(not dropped) — they represent the "primary" repo for the run and continue
to be populated for backward-compatible single-repo display in older UI
states. New multi-repo runs populate the singular fields with the FIRST
repo in the run for the same display fallback.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-04
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "phase_b_run_repos",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("phase_b_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "repo_id",
            sa.String(length=36),
            sa.ForeignKey("code_repos.id"),
            nullable=False,
        ),
        sa.Column("branch", sa.String(length=200), nullable=False),
        sa.Column("mr_url", sa.String(length=1000), nullable=True),
        sa.Column("mr_iid", sa.Integer(), nullable=True),
        sa.Column("mr_state", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "repo_id", name="uq_phase_b_run_repos_run_repo"),
    )
    op.create_index(
        "ix_phase_b_run_repos_run_id",
        "phase_b_run_repos",
        ["run_id"],
        unique=False,
    )

    # Backfill — preserve existing single-repo run history. We can do this
    # in pure SQL since both phase_b_runs and code_repos are populated.
    # gen_random_uuid() requires pgcrypto; use a CTE that joins on the
    # gitlab_repo string match so each existing run gets exactly one
    # phase_b_run_repos row when there's a corresponding code_repos entry.
    op.execute(
        sa.text(
            """
            INSERT INTO phase_b_run_repos
                (id, run_id, repo_id, branch, created_at, updated_at)
            SELECT
                gen_random_uuid()::text,
                pbr.id,
                cr.id,
                COALESCE(pbr.gitlab_branch, cr.gitlab_branch, 'main'),
                COALESCE(pbr.started_at, CURRENT_TIMESTAMP),
                CURRENT_TIMESTAMP
            FROM phase_b_runs pbr
            JOIN code_repos cr ON cr.gitlab_repo = pbr.gitlab_repo
            WHERE pbr.gitlab_repo IS NOT NULL
              AND pbr.gitlab_repo != ''
            ON CONFLICT (run_id, repo_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_phase_b_run_repos_run_id", table_name="phase_b_run_repos")
    op.drop_table("phase_b_run_repos")
