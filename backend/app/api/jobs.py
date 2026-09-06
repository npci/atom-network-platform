# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""REST endpoints for the durable agent-job registry (R-1).

Surface:
    GET  /api/changes/{change_id}/jobs/active  — list in-flight jobs for a CR
    GET  /api/jobs/active                       — list current user's in-flight jobs (across CRs)
    GET  /api/jobs/{job_id}                     — single job status (incl. result if terminal)
    GET  /api/jobs/{job_id}/chunks?since_seq=N  — replay chunks since seq N
    POST /api/jobs/{job_id}/cancel              — request cooperative cancel

Visibility (matches the `defaults` decision: Y for change-scoped, X for admin-only):
  - Jobs with `change_request_id` set → visible to anyone who can read the CR.
    For now that's "any authenticated user"; tighten in a follow-up PR if we
    ever introduce per-CR ACLs.
  - Jobs with `change_request_id` NULL (admin-only: code indexing, RAG re-ingest)
    → visible to original user OR any admin.

R-3 onwards wires the WS handlers to populate the registry. R-1 ships
just the read/cancel surface so it's safe to merge before any handlers
are touched.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app.core.deps import CurrentUser, DbDep
from app.models.agent_job import AgentJob, AgentJobStatus
from app.models.user import UserRole
from app.services import job_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])


# ── Visibility helper ────────────────────────────────────────────────────────


def _can_see_job(job_dict: dict, user) -> bool:
    """Return True iff `user` is allowed to see `job_dict`.

    Rules:
      - Jobs scoped to a change request are visible to all authenticated users
        (Y option in the plan — collaboration default with attribution shown).
        If you ever need per-CR ACLs, add the check here.
      - Jobs without a change_request_id (admin tasks like code indexing,
        RAG re-ingest) are visible only to the user who started them or to
        admins (X option in the plan).
    """
    if job_dict.get("change_request_id"):
        return True
    is_admin = user.role.value == UserRole.ADMIN.value if hasattr(user.role, "value") else str(user.role) == "admin"
    return is_admin or job_dict.get("started_by_user_id") == user.id


# ── List in-flight jobs for a change request ────────────────────────────────


@router.get("/changes/{change_id}/jobs/active")
def list_change_jobs(
    change_id: str,
    db: DbDep,
    user: CurrentUser,
    module: str | None = Query(None, description="Filter by module (e.g. 'brd', 'tech_spec')"),
):
    """Return every active (pending/running) job for this change request.

    The frontend's JobsContext (R-2) calls this on every page mount to
    decide which screens should show a resume banner.
    """
    jobs = job_registry.get_active_jobs(
        db,
        change_request_id=change_id,
        module=module,
    )
    visible = [j for j in jobs if _can_see_job(j, user)]
    return {"jobs": visible, "count": len(visible)}


# ── List current user's in-flight jobs (across CRs + admin jobs) ────────────


@router.get("/jobs/active")
def list_my_jobs(
    db: DbDep,
    user: CurrentUser,
    module: str | None = Query(None),
):
    """Return every job the current user can see and is still active.

    Used by the sidebar's ActiveJobsTray (R-2) to surface running work
    across the whole platform — including admin-only jobs that aren't
    associated with a change request.

    For non-admin users: own jobs (CR-scoped or otherwise).
    For admin users: every active job in the system.
    """
    is_admin = user.role.value == UserRole.ADMIN.value if hasattr(user.role, "value") else str(user.role) == "admin"
    if is_admin:
        jobs = job_registry.get_active_jobs(db, module=module)
    else:
        jobs = job_registry.get_active_jobs(db, started_by_user_id=user.id, module=module)
        # Plus any CR-scoped jobs visible under rule Y
        cr_jobs = [
            j for j in job_registry.get_active_jobs(db, module=module)
            if j.get("change_request_id") and j.get("started_by_user_id") != user.id
        ]
        # Dedup by job id
        seen = {j["id"] for j in jobs}
        jobs.extend(j for j in cr_jobs if j["id"] not in seen)
    return {"jobs": jobs, "count": len(jobs)}


# ── Single job ───────────────────────────────────────────────────────────────


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    db: DbDep,
    user: CurrentUser,
    include_result: bool = Query(False, description="Include final result_payload (can be large)"),
):
    """Fetch a single job — status, progress, current_stage, optionally
    the final result_payload.

    The result_payload is only useful once the job is terminal; for in-flight
    jobs, the chunks endpoint is the right surface.
    """
    job = job_registry.get_job(db, job_id, include_result=include_result)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_see_job(job, user):
        raise HTTPException(status_code=403, detail="Forbidden")
    # Include the chunk_count so the client knows how much it'd replay.
    job["chunk_count"] = job_registry.chunk_count(job_id)
    return job


# ── Replay chunks ────────────────────────────────────────────────────────────


@router.get("/jobs/{job_id}/chunks")
def get_job_chunks(
    job_id: str,
    db: DbDep,
    user: CurrentUser,
    since_seq: int = Query(0, ge=0, description="Return chunks with seq > since_seq"),
):
    """Replay protocol: return every chunk for this job with seq > `since_seq`.

    Used by the WS reconnect handler (R-3+) to catch a client up after it
    navigates back to a screen mid-stream. The client tracks the highest seq
    it has received; on reconnect it sends `replay_request` over the WS,
    which delegates to this endpoint internally OR returns chunks via the
    WS protocol — either path returns the same data.

    For a fresh client (since_seq=0), this returns the entire current buffer.
    """
    # Fetch the job first so we can do the visibility check before exposing
    # any chunks (which may contain sensitive content for admin-only jobs).
    job = job_registry.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_see_job(job, user):
        raise HTTPException(status_code=403, detail="Forbidden")

    chunks = job_registry.get_chunks_since(job_id, since_seq=since_seq)
    return {
        "job_id":       job_id,
        "since_seq":    since_seq,
        "chunks":       [{"seq": s, "text": t} for (s, t) in chunks],
        "count":        len(chunks),
        "job_status":   job["status"],
    }


# ── Cancel ───────────────────────────────────────────────────────────────────


@router.get("/admin/jobs/stats")
def admin_jobs_stats(db: DbDep, user: CurrentUser):
    """Admin-only — aggregate counts of agent_jobs rows by status, module,
    and stale-detection. Used for an operations dashboard / monitoring.

    R-9 — pairs with the orphan sweeper to give operators a single
    endpoint to answer 'what's running, what's stuck, what failed'.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func as sa_func

    from app.models.agent_job import AgentJob, AgentJobStatus, ACTIVE_STATUSES
    from app.models.user import UserRole

    is_admin = (
        user.role.value == UserRole.ADMIN.value
        if hasattr(user.role, "value") else str(user.role) == "admin"
    )
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    # Counts by status (over all time — operators can filter via timestamp later)
    status_rows = (
        db.query(AgentJob.status, sa_func.count(AgentJob.id))
        .group_by(AgentJob.status)
        .all()
    )
    by_status = {
        (s.value if hasattr(s, "value") else str(s)): n
        for (s, n) in status_rows
    }

    # Counts by module among currently-active jobs
    module_rows = (
        db.query(AgentJob.module, sa_func.count(AgentJob.id))
        .filter(AgentJob.status.in_(ACTIVE_STATUSES))
        .group_by(AgentJob.module)
        .all()
    )
    active_by_module = {m: n for (m, n) in module_rows}

    # "Potentially stuck" — running, no update in last 30 min. The sweeper
    # will catch these on its next pass; surfacing them here lets operators
    # see them BEFORE the sweep marks them failed.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    stuck_count = (
        db.query(sa_func.count(AgentJob.id))
        .filter(AgentJob.status.in_(ACTIVE_STATUSES))
        .filter(AgentJob.updated_at < cutoff)
        .scalar() or 0
    )

    # Total active across all modules
    active_total = sum(active_by_module.values())

    return {
        "by_status":         by_status,
        "active_by_module":  active_by_module,
        "active_total":      active_total,
        "stuck_count":       stuck_count,
        "stuck_threshold_minutes": 30,
    }


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    db: DbDep,
    user: CurrentUser,
):
    """Request cooperative cancellation of a job.

    Note (matches the `cancel=A` decision in the plan): R-1 ships the
    surface; the actual mid-pipeline check (`is_cancelled(job_id)` calls
    inside WS handlers + docgen pipeline + Celery tasks) lands as a
    follow-up. Calling this endpoint TODAY marks the job cancelled in
    the registry and the UI shows the cancelled banner; the underlying
    handler will keep running until it would have completed naturally.

    Even with that limitation, this is useful — the user gets immediate
    UI feedback that "their request was honoured", and the cancelled-row
    state is what the resume protocol uses to decide not to re-show the
    banner on the next mount.
    """
    job = job_registry.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_see_job(job, user):
        raise HTTPException(status_code=403, detail="Forbidden")

    # Cancellation is allowed by the original starter or by an admin.
    is_admin = user.role.value == UserRole.ADMIN.value if hasattr(user.role, "value") else str(user.role) == "admin"
    if not is_admin and job.get("started_by_user_id") != user.id:
        raise HTTPException(status_code=403, detail="Only the original user or an admin can cancel")

    if job["status"] not in ("pending", "running"):
        # Idempotent — already terminal, no-op.
        return {"job_id": job_id, "status": job["status"], "ok": True, "noop": True}

    job_registry.cancel_job(db, job_id)
    return {"job_id": job_id, "status": "cancelled", "ok": True}
