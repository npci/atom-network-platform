# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: Jobs adapter.
#
# WHY: the standalone engine had its own SQLite job store. The host project
# already has a Redis + Postgres-backed durable job registry at
# `app.services.job_registry` (Slice R-1, R-5, R-9 — used by the resume-on-
# disconnect feature). Reusing it gives us:
#   - Free durable resume across server restarts.
#   - Free integration with the existing JobsContext on the frontend.
#   - Free observability hookups (Slice 28).
# Building a parallel SQLite store would have created two sources of truth
# for "what jobs are running" and broken the resume sidebar.
#
# WHY a thin wrapper rather than direct calls in the engine:
#   - The host's `create_job(...)` requires a `change_request_id` and a
#     `module` — both irrelevant to a unit test of the engine. The wrapper
#     lets tests inject an in-memory fake.
#   - The host's API uses positional kwargs; we surface a stable shape the
#     engine can rely on even if the host signature drifts in future slices.

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

logger = logging.getLogger("excel_engine.jobs")

_create_job: Callable | None = None
_update_job: Callable | None = None
_complete_job: Callable | None = None
_fail_job: Callable | None = None
_get_job: Callable | None = None
_session_factory: Callable[[], Session] | None = None


def configure(
    *,
    create_job: Callable,
    update_job: Callable,
    complete_job: Callable,
    fail_job: Callable,
    get_job: Callable,
    session_factory: Callable[[], Session],
) -> None:
    global _create_job, _update_job, _complete_job, _fail_job, _get_job, _session_factory
    _create_job = create_job
    _update_job = update_job
    _complete_job = complete_job
    _fail_job = fail_job
    _get_job = get_job
    _session_factory = session_factory


def update_stage(job_id: str, stage: str, progress_pct: int | None = None) -> None:
    """Engine reports a stage transition (planning, writing, validating, ...).

    `progress_pct` is an optional 0-100 estimate of overall completion.
    The host's `update_job` clamps to [0, 100] internally so passing a value
    outside the range is harmless. Callers omit it for transitions that
    can't easily be quantified.
    """

    if _update_job is None or _session_factory is None:
        return
    db = _session_factory()
    try:
        kwargs: dict = {"current_stage": stage}
        if progress_pct is not None:
            kwargs["progress_pct"] = int(progress_pct)
        _update_job(db, job_id, **kwargs)
    except Exception as exc:  # noqa: BLE001
        # WHY swallow: the engine should never fail because telemetry failed.
        # We log at warning so the host operator sees it; the workflow proceeds.
        logger.warning("excel_engine jobs.update_stage failed: %s", exc)
    finally:
        db.close()


def get(job_id: str) -> dict | None:
    """Read-only access for the API layer (cost summary, file paths)."""

    if _get_job is None or _session_factory is None:
        return None
    db = _session_factory()
    try:
        return _get_job(db, job_id, include_result=True)
    finally:
        db.close()


def attach_result(job_id: str, *, xlsx_path: str, md_path: str | None, docx_path: str | None) -> None:
    """Deprecated no-op kept for callers that still reference it.

    Historically tried to write file paths to the job row via
    `update_job(metadata_patch=...)` / `update_job(result=...)`. The host's
    `update_job` accepts neither, so every call ended up emitting a warning
    without actually persisting anything.

    File paths now flow through `mark_complete(summary=...)` — the
    orchestrator stashes them into `state["metrics"]["files"]` and they end
    up in `result_payload` via `complete_job(result=summary)`. That's the
    sole supported channel; the host download endpoint reads from there.

    Left as a no-op so any older callers don't NameError, and so we get a
    single trace-level breadcrumb for forensics if needed.
    """

    logger.debug(
        "excel_engine jobs.attach_result: deprecated no-op (paths flow via mark_complete summary)"
    )
    return None


def mark_complete(job_id: str, *, summary: dict[str, Any] | None = None) -> None:
    if _complete_job is None or _session_factory is None:
        return
    db = _session_factory()
    try:
        _complete_job(db, job_id, result=summary or {}, final_stage="Workbook completed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("excel_engine jobs.mark_complete failed: %s", exc)
    finally:
        db.close()


def mark_failed(job_id: str, *, error: str, stage: str = "Failed") -> None:
    if _fail_job is None or _session_factory is None:
        return
    db = _session_factory()
    try:
        # SCR #6: pass `error` WHOLE. `fail_job` scrubs before it truncates,
        # precisely so a leak marker sitting past the cut-off is still detected
        # rather than sliced out of view. Truncating here first defeated that:
        # a 1,200-character SQLAlchemy error whose `[SQL: ...]` tail sat beyond
        # character 1,000 arrived pre-trimmed, the marker was already gone, and
        # the surviving prefix was stored and served verbatim. Reproduced before
        # this change and covered by
        # tests/core/test_job_error_message_scrubbing.py.
        _fail_job(db, job_id, error=error, final_stage=stage)
    except Exception as exc:  # noqa: BLE001
        logger.warning("excel_engine jobs.mark_failed failed: %s", exc)
    finally:
        db.close()


__all__ = [
    "configure", "update_stage", "get",
    "attach_result", "mark_complete", "mark_failed",
]
