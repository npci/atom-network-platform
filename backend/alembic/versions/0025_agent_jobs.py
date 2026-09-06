# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Add `agent_jobs` table for durable long-running job tracking.

Foundation for the resume-progress feature — every WS-streamed agent run,
every REST-triggered async task (code indexing, RAG re-ingest, phase B
steps), and every section-wise edit gets a row that survives across
client navigation, page reloads, and browser sessions.

Status lifecycle:
    pending → running → succeeded | failed | cancelled

Chunk-level streaming history lives in Redis (key `job:chunks:<job_id>`,
TTL 1h) — Postgres holds only the lifecycle metadata + final result.
This keeps the table small (one row per job, not per chunk) and makes
replay cheap (Redis LRANGE), while final results survive Redis eviction.

Visibility rules (enforced at the API layer, not the schema):
  - change-request-scoped jobs: visible to anyone who can read the change
    request (collaboration default, "started by A" attribution shown)
  - admin-only jobs (code indexing, RAG re-ingest): visible to original
    user + admins (Bucket B)

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


JOB_STATUS_VALUES = ("pending", "running", "succeeded", "failed", "cancelled")


def upgrade() -> None:
    # Create the enum type explicitly so we can drop it cleanly on downgrade.
    # `create_type=False` here is critical: we already create the type with
    # `checkfirst=True` two lines below. Without `create_type=False`, the
    # Column(Enum(...)) inside `op.create_table` would emit a SECOND
    # bare `CREATE TYPE agent_job_status` at table-create time, which
    # blows up with `psycopg2.errors.DuplicateObject` if the type already
    # exists from a previous failed migration attempt.
    job_status = postgresql.ENUM(
        *JOB_STATUS_VALUES,
        name="agent_job_status",
        create_type=False,
    )
    job_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "agent_jobs",
        # Primary key — uuid string for parity with other ID columns in the schema
        # (change_requests, brd, etc. all use String(36) UUIDs).
        sa.Column("id", sa.String(36), primary_key=True),

        # Optional change-request linkage. Admin-only jobs (code indexing,
        # RAG re-ingest) leave this NULL.
        sa.Column(
            "change_request_id",
            sa.String(36),
            sa.ForeignKey("change_requests.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),

        # Module identifier — matches the WS module key the frontend uses
        # (e.g. 'brd', 'tech_spec', 'research', 'product_kit', 'code_indexing').
        # Free-form string rather than enum so adding a new module doesn't
        # require a migration.
        sa.Column("module", sa.String(64), nullable=False, index=True),

        # Optional sub-classification for modules that fan out:
        #   product_kit  → 'circular' / 'product_doc' / etc.
        #   code_indexing → repo_id
        #   phase_b      → 'code_change' / 'code_review' / 'build' / etc.
        sa.Column("subtype", sa.String(128), nullable=True),

        # Lifecycle status — see top-of-file comment.
        sa.Column(
            "status",
            job_status,
            nullable=False,
            server_default="pending",
            index=True,
        ),

        # Progress 0-100. Optional; many WS streams won't bother computing
        # a percentage and will just update current_stage. UI falls back to
        # showing the stage label when progress_pct is NULL.
        sa.Column("progress_pct", sa.Integer, nullable=True),

        # Human-readable banner — what's happening right now.
        # Examples: "Retrieving knowledge base context", "Writing section 6 of 14",
        # "Indexing 1300 of 1300 files".
        sa.Column("current_stage", sa.String(255), nullable=True),

        # Lifecycle timestamps. updated_at is bumped by JobRegistry.update_job
        # and used by the orphan-sweeper Celery task (R-9) to detect stuck jobs.
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),

        # Attribution — who started the job. Used for visibility rules
        # (admin-only jobs filter on this) and for the "started 5 min ago by A"
        # banner copy.
        sa.Column(
            "started_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),

        # Final outputs. For a BRD job: {"markdown": "...", "docgen_job_id": "...", "docx_path": "..."}.
        # For a code-indexing job: {"chunks_created": 51247, "files_processed": 1300, ...}.
        # NULL while the job is in flight.
        sa.Column(
            "result_payload",
            postgresql.JSONB,
            nullable=True,
        ),

        # Error message on the failure path. Truncated to 4096 chars at the
        # write site.
        sa.Column("error_message", sa.Text, nullable=True),

        # Module-specific metadata — kept loose so we don't bloat the schema.
        # Examples:
        #   {"doc_type": "BRD", "version": 3}
        #   {"repo_id": "abc-123", "branch": "main", "commit_sha": "..."}
        #   {"phase_b_step": "code_review"}
        sa.Column(
            "metadata_",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # Common access patterns:
    #   - "give me all in-flight jobs for this change request" — covered by
    #     index on change_request_id; add a composite for the active filter.
    op.create_index(
        "ix_agent_jobs_change_status",
        "agent_jobs",
        ["change_request_id", "status"],
    )
    #   - "give me all in-flight admin jobs for this user" — covered by
    #     started_by_user_id index alone.
    #   - "the orphan sweeper wants jobs older than X with status='running'" —
    #     covered by status index + updated_at scan.
    op.create_index(
        "ix_agent_jobs_status_updated",
        "agent_jobs",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_jobs_status_updated", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_change_status", table_name="agent_jobs")
    op.drop_table("agent_jobs")
    job_status = postgresql.ENUM(
        *JOB_STATUS_VALUES,
        name="agent_job_status",
        create_type=False,
    )
    job_status.drop(op.get_bind(), checkfirst=True)
