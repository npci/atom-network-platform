# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""JobRegistry — durable long-running job tracking + chunk-replay buffer.

This module is the foundation for the resume-progress feature
(R-1 in the plan). WS handlers, REST handlers, and Celery tasks all
funnel through here so that a job survives:

  - the WebSocket disconnecting (component unmount on navigate-away)
  - the page reloading
  - the browser closing and re-opening (within the configured TTL)

Storage:
  - `agent_jobs` Postgres row: lifecycle metadata + final result.
  - Redis list `job:chunks:<job_id>`: every chunk emitted, in order.
    Used for the on-reconnect replay protocol. TTL 1 h after the
    last write — long enough to support resume across a coffee break,
    short enough to avoid unbounded growth.
  - Redis hash `job:meta:<job_id>`: a small mirror of (status,
    progress_pct, current_stage) refreshed on every update so a
    PubSub subscriber (R-2 onwards) can fan out without polling
    Postgres. Optional for R-1; populated regardless so R-2 can use it.

Failure modes:
  - Redis unavailable: chunk-append calls log a warning and continue.
    Postgres lifecycle remains intact; the only loss is the live
    chunk-replay buffer, so reconnecting clients won't see in-flight
    text but will see the final result once the job completes.
  - Postgres unavailable: caller's transaction fails — let it propagate.
    A job that never gets a row is fine (the WS stream still works the
    old way; the resume feature just doesn't activate).

Public surface (used by app.api.jobs and the WS handlers in R-3+):
    create_job(...)                     → str (job_id)
    update_job(job_id, **fields)        → None
    complete_job(job_id, result)        → None
    fail_job(job_id, error)             → None
    cancel_job(job_id)                  → None
    get_job(job_id)                     → dict | None
    get_active_jobs(change_id, ...)     → list[dict]
    append_chunk(job_id, text, seq=None)→ int (next_seq)
    get_chunks_since(job_id, since_seq) → list[(seq, text)]
    chunk_buffer_ttl_seconds            → 3600 (constant)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.error_taxonomy import client_safe_message
from app.models.agent_job import AgentJob, AgentJobStatus, ACTIVE_STATUSES

logger = logging.getLogger(__name__)


# Redis chunk-buffer TTL. 1 h is long enough that a user can:
#   - take a 30 min coffee break, navigate back, see the partial text
#   - close their laptop, reopen 45 min later, still see context
# Short enough that we don't accumulate stale 4-hour-old chunks for
# abandoned jobs.
CHUNK_BUFFER_TTL_SECONDS = 3600

# Cap on a single chunk size we'll store in Redis. Defensive against a
# misbehaving handler emitting a 10 MB chunk. Real chunks are <1 KB.
MAX_CHUNK_BYTES = 64 * 1024


# ── Redis client (lazy, shared) ──────────────────────────────────────────────

_redis_client = None


def _get_redis():
    """Lazy redis client. Returns None when redis is unavailable so callers
    can short-circuit gracefully (tests, dev without redis)."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        logger.warning("JobRegistry: redis unavailable (%s) — chunk replay disabled", e)
        _redis_client = None
        return None


def _chunks_key(job_id: str) -> str:
    return f"job:chunks:{job_id}"


def _meta_key(job_id: str) -> str:
    return f"job:meta:{job_id}"


# ── Lifecycle ────────────────────────────────────────────────────────────────


def create_job(
    db: Session,
    *,
    change_request_id: str | None,
    module: str,
    subtype: str | None = None,
    started_by_user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Create a new agent_jobs row, return its `job_id`.

    Status starts at `running` (not `pending`) — by the time create_job is
    called we've already accepted the user's request and started doing work.
    The `pending` state is reserved for queued jobs (R-9 introduces a queue
    for jobs that exceed a global concurrency cap; today we go straight
    to running).
    """
    job_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    job = AgentJob(
        id=job_id,
        change_request_id=change_request_id,
        module=module,
        subtype=subtype,
        status=AgentJobStatus.RUNNING,
        started_at=now,
        updated_at=now,
        started_by_user_id=started_by_user_id,
        metadata_=metadata or {},
    )
    db.add(job)
    db.commit()

    # Mirror to Redis meta hash so PubSub subscribers (R-2) see it
    # without polling Postgres.
    r = _get_redis()
    if r is not None:
        try:
            r.hset(_meta_key(job_id), mapping={
                "status":        "running",
                "progress_pct":  "",
                "current_stage": "",
                "started_at":    now.isoformat(),
            })
            r.expire(_meta_key(job_id), CHUNK_BUFFER_TTL_SECONDS)
        except Exception as e:
            logger.debug("JobRegistry.create_job: redis meta write failed (%s)", e)

    logger.info(
        "JobRegistry: created job=%s module=%s change=%s user=%s",
        job_id, module, change_request_id, started_by_user_id,
    )
    return job_id


def update_job(
    db: Session,
    job_id: str,
    *,
    status: AgentJobStatus | None = None,
    progress_pct: int | None = None,
    current_stage: str | None = None,
) -> None:
    """Update one or more lifecycle fields. Each call also bumps `updated_at`
    so the orphan-sweeper (R-9) can detect stuck jobs.

    Pass only the fields that changed. To set a field to NULL explicitly,
    don't use this — call complete_job / fail_job with the appropriate
    payload instead.
    """
    job = db.query(AgentJob).filter(AgentJob.id == job_id).first()
    if not job:
        logger.warning("JobRegistry.update_job: job_id=%s not found", job_id)
        return

    if status is not None:
        job.status = status
    if progress_pct is not None:
        # Clamp defensively — a misbehaving caller passing 105 shouldn't
        # produce an invalid DB state.
        job.progress_pct = max(0, min(100, int(progress_pct)))
    if current_stage is not None:
        # SCR #6: `current_stage` is serialised by AgentJob.to_dict() and
        # returned by GET /api/jobs/{job_id}, exactly like `error_message`.
        # The scrub was originally applied only in fail_job, but stage text is
        # not always authored either — api/cert_push.py feeds an upstream
        # cert-agent's `evt["message"]` straight into this parameter. Scrub
        # before the column cap, for the same reason fail_job does.
        job.current_stage = client_safe_message(
            current_stage, fallback="processing",
        )[:255]
    job.updated_at = datetime.now(timezone.utc)
    db.commit()

    # Refresh redis meta mirror so subscribers see the change immediately.
    r = _get_redis()
    if r is not None:
        try:
            payload: dict[str, str] = {"updated_at": job.updated_at.isoformat()}
            if status is not None:
                payload["status"] = status.value
            if progress_pct is not None:
                payload["progress_pct"] = str(job.progress_pct)
            if current_stage is not None:
                payload["current_stage"] = job.current_stage or ""
            r.hset(_meta_key(job_id), mapping=payload)
            r.expire(_meta_key(job_id), CHUNK_BUFFER_TTL_SECONDS)
        except Exception as e:
            logger.debug("JobRegistry.update_job: redis meta write failed (%s)", e)


def advance_stage_by_chars(
    db: Session,
    job_id: str,
    current_chars: int,
    milestones: list[tuple[int, str, int]],
    last_idx: int,
) -> int:
    """Advance a job's `current_stage` + `progress_pct` as streaming output
    crosses character-count thresholds. Returns the new milestone index;
    callers pass it back as `last_idx` on the next chunk.

    `milestones` is ordered `[(chars_threshold, label, progress_pct), ...]`.
    First entry should have threshold 0 so the initial stage fires on the
    first call (pass `last_idx=0`). Cheap when no advance happens — only
    a bounds check; DB write fires only on a milestone crossing.
    """
    new_idx = last_idx
    while new_idx < len(milestones) and current_chars >= milestones[new_idx][0]:
        new_idx += 1
    if new_idx > last_idx:
        _, label, pct = milestones[new_idx - 1]
        update_job(db, job_id, current_stage=label, progress_pct=pct)
    return new_idx


def complete_job(
    db: Session,
    job_id: str,
    *,
    result: dict[str, Any] | None = None,
    final_stage: str = "Completed",
) -> None:
    """Mark a job succeeded. `result` becomes the durable record of what
    the job produced (markdown, file paths, validation summary, etc.).

    The Redis chunk buffer is left alone — clients still reconnecting in
    the next hour can replay it. After CHUNK_BUFFER_TTL_SECONDS Redis
    expires it automatically; the final result_payload in Postgres is
    the long-term record.
    """
    job = db.query(AgentJob).filter(AgentJob.id == job_id).first()
    if not job:
        logger.warning("JobRegistry.complete_job: job_id=%s not found", job_id)
        return

    now = datetime.now(timezone.utc)
    job.status = AgentJobStatus.SUCCEEDED
    job.completed_at = now
    job.updated_at = now
    # SCR #6: same client-visible field as in update_job. A success path is the
    # least likely source of a leak, but several callers pass a stage string
    # built from a downstream response, so the same cheap gate applies.
    job.current_stage = client_safe_message(final_stage, fallback="Completed")[:255]
    job.progress_pct = 100
    if result is not None:
        job.result_payload = result
    db.commit()

    r = _get_redis()
    if r is not None:
        try:
            r.hset(_meta_key(job_id), mapping={
                "status":        "succeeded",
                "progress_pct":  "100",
                "current_stage": final_stage,
                "completed_at":  now.isoformat(),
            })
            r.expire(_meta_key(job_id), CHUNK_BUFFER_TTL_SECONDS)
        except Exception as e:
            logger.debug("JobRegistry.complete_job: redis meta write failed (%s)", e)

    logger.info("JobRegistry: completed job=%s module=%s", job_id, job.module)


def fail_job(
    db: Session,
    job_id: str,
    *,
    error: str,
    final_stage: str | None = None,
) -> None:
    """Mark a job failed with an error message. Truncates the message to
    4096 chars at the write site so an LLM-generated traceback can't bloat
    the row.

    SCR #6 (Information Exposure Through an Error Message): `error_message` is
    NOT an internal-only field. It is serialised by `AgentJob.to_dict()`,
    returned by `GET /api/jobs/{job_id}`, and rendered in the UI. Most callers
    pass `str(exc)` from a broad `except Exception`, so without scrubbing here a
    SQLAlchemy failure would put table names, column names and the SQL
    statement on a user-visible screen.

    Scrubbing at this chokepoint rather than at each of the ~20 call sites means
    a new caller cannot reintroduce the leak by forgetting to sanitise. The
    unredacted text is still available to operators: every one of those call
    sites logs it via `logger.exception`/`logger.error` first.
    """
    # fail_job is reached precisely because something went wrong upstream — and
    # that "something" may have left the session's transaction aborted (a
    # swallowed DB error). A bare query here would then raise
    # InFailedSqlTransaction and the FAILED status would never be recorded.
    # Recover the session first so the failure always lands.
    try:
        job = db.query(AgentJob).filter(AgentJob.id == job_id).first()
    except Exception:
        db.rollback()
        job = db.query(AgentJob).filter(AgentJob.id == job_id).first()
    if not job:
        logger.warning("JobRegistry.fail_job: job_id=%s not found", job_id)
        return

    now = datetime.now(timezone.utc)
    job.status = AgentJobStatus.FAILED
    job.completed_at = now
    job.updated_at = now
    # Scrub BEFORE truncating: a leak marker sitting past the 4096th character
    # must still be detected rather than sliced out of view.
    job.error_message = client_safe_message(error)[:4096]
    if final_stage:
        # Also client-visible via to_dict(); several callers build this string
        # from the same exception they pass as `error`.
        job.current_stage = client_safe_message(final_stage, fallback="Failed")[:255]
    db.commit()

    r = _get_redis()
    if r is not None:
        try:
            r.hset(_meta_key(job_id), mapping={
                "status":        "failed",
                "current_stage": job.current_stage or "Failed",
                "error_message": job.error_message[:512],   # truncated mirror
                "completed_at":  now.isoformat(),
            })
            r.expire(_meta_key(job_id), CHUNK_BUFFER_TTL_SECONDS)
        except Exception as e:
            logger.debug("JobRegistry.fail_job: redis meta write failed (%s)", e)

    logger.warning(
        "JobRegistry: failed job=%s module=%s error=%s",
        job_id, job.module, job.error_message[:200] if job.error_message else "",
    )


def cancel_job(db: Session, job_id: str) -> None:
    """Mark a job cancelled. Note: cancellation is COOPERATIVE — the
    actual handler must check `is_cancelled(job_id)` at boundaries.
    R-1 ships visible-only (cancel=A in the plan); the cancel button is
    deferred to a follow-up. This method exists now so the API layer can
    expose /api/jobs/{id}/cancel without a schema change later.
    """
    job = db.query(AgentJob).filter(AgentJob.id == job_id).first()
    if not job:
        logger.warning("JobRegistry.cancel_job: job_id=%s not found", job_id)
        return

    if job.status not in ACTIVE_STATUSES:
        logger.info(
            "JobRegistry.cancel_job: job=%s already terminal (%s) — no-op",
            job_id, job.status.value,
        )
        return

    now = datetime.now(timezone.utc)
    job.status = AgentJobStatus.CANCELLED
    job.completed_at = now
    job.updated_at = now
    job.current_stage = "Cancelled"
    db.commit()

    r = _get_redis()
    if r is not None:
        try:
            r.hset(_meta_key(job_id), mapping={
                "status":        "cancelled",
                "current_stage": "Cancelled",
                "completed_at":  now.isoformat(),
            })
            r.expire(_meta_key(job_id), CHUNK_BUFFER_TTL_SECONDS)
        except Exception as e:
            logger.debug("JobRegistry.cancel_job: redis meta write failed (%s)", e)

    logger.info("JobRegistry: cancelled job=%s module=%s", job_id, job.module)


def is_cancelled(db: Session, job_id: str) -> bool:
    """Cooperative-cancel probe. Handlers call this at boundaries
    (between LLM calls, between pipeline stages) and abort if True.
    Cheap — Redis lookup first, falls back to Postgres on miss."""
    r = _get_redis()
    if r is not None:
        try:
            status = r.hget(_meta_key(job_id), "status")
            if status == "cancelled":
                return True
        except Exception:
            pass
    job = db.query(AgentJob).filter(AgentJob.id == job_id).first()
    return bool(job and job.status == AgentJobStatus.CANCELLED)


# ── Read API ─────────────────────────────────────────────────────────────────


def get_job(db: Session, job_id: str, *, include_result: bool = False) -> dict | None:
    """Fetch one job. Pass `include_result=True` to include the result_payload
    (can be large for BRD/TSD jobs)."""
    job = db.query(AgentJob).filter(AgentJob.id == job_id).first()
    if not job:
        return None
    out = job.to_dict()
    if include_result:
        out["result_payload"] = job.result_payload
    return out


def get_active_jobs(
    db: Session,
    *,
    change_request_id: str | None = None,
    module: str | None = None,
    started_by_user_id: str | None = None,
) -> list[dict]:
    """Return all currently-active (pending or running) jobs matching
    the given filters. Used by the frontend on every screen mount to
    decide whether to show a resume banner.

    Visibility filtering happens at the API layer — this function is
    a pure DB read.
    """
    q = db.query(AgentJob).filter(AgentJob.status.in_(ACTIVE_STATUSES))
    if change_request_id is not None:
        q = q.filter(AgentJob.change_request_id == change_request_id)
    if module is not None:
        q = q.filter(AgentJob.module == module)
    if started_by_user_id is not None:
        q = q.filter(AgentJob.started_by_user_id == started_by_user_id)
    q = q.order_by(desc(AgentJob.started_at))
    return [j.to_dict() for j in q.all()]


# ── Chunk replay buffer (Redis) ──────────────────────────────────────────────


def append_chunk(job_id: str, text: str) -> int:
    """Append a chunk of streamed output to the job's Redis list.

    Returns the new sequence number (1-indexed list length after the push)
    so a caller that wants to send `{seq, text}` to the client can include
    a stable cursor.

    Fails silently if Redis is unavailable — the WS stream itself is
    unaffected; only the resume-on-reconnect feature degrades.
    """
    if not text:
        return 0
    r = _get_redis()
    if r is None:
        return 0

    payload = text
    if isinstance(payload, str):
        # Defensive size cap. Real WS chunks are <1 KB.
        if len(payload.encode("utf-8")) > MAX_CHUNK_BYTES:
            payload = payload[: MAX_CHUNK_BYTES // 2]   # rough — strip half
            logger.warning(
                "JobRegistry.append_chunk: oversized chunk (%d bytes) truncated for job=%s",
                len(text.encode("utf-8")), job_id,
            )

    try:
        # RPUSH returns the new length of the list — that's our seq number.
        seq = r.rpush(_chunks_key(job_id), payload)
        # Refresh TTL on every write so an active job doesn't expire mid-stream.
        r.expire(_chunks_key(job_id), CHUNK_BUFFER_TTL_SECONDS)
        return int(seq)
    except Exception as e:
        logger.debug("JobRegistry.append_chunk: redis write failed (%s)", e)
        return 0


def get_chunks_since(job_id: str, since_seq: int = 0) -> list[tuple[int, str]]:
    """Replay protocol: return every chunk for `job_id` with seq > since_seq.

    For a fresh client, since_seq=0 returns everything currently in the
    buffer. For a client that already received chunks 1..N (e.g., it had
    a flaky connection that briefly dropped), since_seq=N returns N+1
    onwards — cheap incremental catch-up.

    Returns [(seq, text), ...] in original order.
    """
    r = _get_redis()
    if r is None:
        return []

    try:
        # LRANGE with start = since_seq returns everything from position
        # since_seq (0-indexed) to the end. That maps to seq > since_seq
        # because we 1-index seq (rpush returns new length AFTER push).
        items = r.lrange(_chunks_key(job_id), since_seq, -1)
    except Exception as e:
        logger.debug("JobRegistry.get_chunks_since: redis read failed (%s)", e)
        return []

    # Reattach 1-indexed seq numbers.
    return [(since_seq + i + 1, t) for i, t in enumerate(items)]


def chunk_count(job_id: str) -> int:
    """Total chunks currently buffered for this job. Useful for the
    REST `/api/jobs/{id}` response to expose `chunk_count` so the
    client knows how many chunks it'd replay before subscribing."""
    r = _get_redis()
    if r is None:
        return 0
    try:
        return int(r.llen(_chunks_key(job_id)))
    except Exception:
        return 0


# ── WS-handler convenience: send + append_chunk in one call ─────────────────


class tracked_step:
    """Context manager: register a job in the registry, mark completed on
    success, failed on exception. Designed for REST handlers that already
    `await` synchronously (no WS chunk streaming needed).

    Usage::

        with job_registry.tracked_step(
            db, change_request_id=change_id, module='phase_b',
            subtype='code_review', user_id=user.id,
            initial_stage='Running code review',
        ) as job_id:
            result = await run_code_review(...)
            # success → exits cleanly → complete_job(result_payload=None)

    On exception: fail_job is called with the str(exception). The
    exception is RE-RAISED so the existing endpoint error path
    (HTTPException 500) still fires. Caller can attach a custom
    `result` to the success path by setting `tracker.result = {...}`
    on the manager (held as a member; not strictly required).

    Designed to be a 4-line patch around any handler that runs an
    expensive `await`. Doesn't change the response shape — banner
    visibility comes from the agent_jobs row + sidebar tray polling.
    """

    def __init__(
        self,
        db,
        *,
        change_request_id: str | None,
        module: str,
        subtype: str | None = None,
        user_id: str | None = None,
        initial_stage: str | None = None,
        metadata: dict | None = None,
    ):
        self.db = db
        self.change_request_id = change_request_id
        self.module = module
        self.subtype = subtype
        self.user_id = user_id
        self.initial_stage = initial_stage
        self.metadata = metadata or {}
        self.job_id: str | None = None
        self.result: dict | None = None         # caller can set before exit
        self.final_stage: str | None = None     # caller can set before exit

    def __enter__(self) -> str:
        self.job_id = create_job(
            self.db,
            change_request_id=self.change_request_id,
            module=self.module,
            subtype=self.subtype,
            started_by_user_id=self.user_id,
            metadata=self.metadata,
        )
        if self.initial_stage:
            update_job(self.db, self.job_id, current_stage=self.initial_stage)

        # R-9 — bind this job_id into the contextvar so every nested
        # call_llm/stream_llm gets `job_id` on its trace automatically.
        # Stash the token so __exit__ can restore the previous binding
        # cleanly (nested tracked_step blocks would shadow correctly).
        try:
            from app.core.llm import current_job_id
            self._cvar_token = current_job_id.set(self.job_id)
        except Exception:
            self._cvar_token = None

        return self.job_id

    def __exit__(self, exc_type, exc, tb) -> bool:
        # R-9 — restore the previous job_id binding before completing
        # the job. Order matters: we want any cleanup-time call_llm in
        # the caller's outer scope to NOT be tagged with this job_id.
        if getattr(self, "_cvar_token", None) is not None:
            try:
                from app.core.llm import current_job_id
                current_job_id.reset(self._cvar_token)
            except Exception:
                pass

        if not self.job_id:
            return False
        if exc_type is None:
            complete_job(
                self.db, self.job_id,
                result=self.result,
                final_stage=self.final_stage or "Completed",
            )
        else:
            try:
                fail_job(
                    self.db, self.job_id,
                    error=str(exc),
                    final_stage=self.final_stage or "Failed",
                )
            except Exception:
                pass
        return False   # never suppress the exception — caller's error path runs


async def ws_send_chunk(websocket, job_id: str | None, text: str) -> int:
    """Send a `{type: 'chunk', text, seq}` message over the WS AND mirror it
    into the job's Redis chunk buffer for replay-on-reconnect.

    Returns the new seq number from append_chunk (1-indexed; 0 if Redis is
    unavailable or `job_id` is None — in either case the WS send still happens).
    Used by R-3+ WS handlers as a drop-in replacement for the existing
    `await websocket.send_text(json.dumps({"type": "chunk", "text": text}))`
    pattern.
    """
    import json
    seq = 0
    if job_id:
        seq = append_chunk(job_id, text)
    payload: dict = {"type": "chunk", "text": text}
    if seq:
        payload["seq"] = seq
    if job_id:
        payload["job_id"] = job_id
    try:
        await websocket.send_text(json.dumps(payload))
    except Exception:
        # Client gone / WS closed — no-op. The job continues, the chunk
        # is in Redis, the next reconnection will replay it.
        pass
    return seq


# ── Sweeper helper (used by the orphan-detection Celery task in R-9) ────────


def sweep_orphan_jobs(db: Session, *, max_idle_minutes: int = 30,
                      reason: str | None = None) -> int:
    """Mark jobs as failed when they haven't been updated in
    `max_idle_minutes`. Run from a Celery beat task in R-9, and with
    max_idle_minutes=0 from app startup (any job still active at boot
    belonged to a process that no longer exists).

    `reason` overrides the default error_message — it is what the UI
    shows the user on the failed job.

    Returns the count of jobs swept.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_idle_minutes)
    stuck = (
        db.query(AgentJob)
        .filter(AgentJob.status.in_(ACTIVE_STATUSES))
        .filter(AgentJob.updated_at < cutoff)
        .all()
    )
    n = 0
    for job in stuck:
        job.status = AgentJobStatus.FAILED
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        job.error_message = reason or (
            f"Orphaned — no updates for >{max_idle_minutes} minutes; "
            "swept by orphan-job task."
        )
        n += 1
    if n:
        db.commit()
        logger.warning("JobRegistry.sweep_orphan_jobs: marked %d jobs failed", n)
    return n
