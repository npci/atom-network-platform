# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Human-language dependency preflight for agentic code generation.

When code gen can't run, the failure used to be SILENT — a run got created, the
task was enqueued, and (because no worker consumed it, or Redis auth was wrong,
or git/credentials were missing) it sat frozen at "Getting ready" with nothing
in the app logs. This module checks every dependency the pipeline needs and
returns CLEAR, human-readable problems, surfaced in three places:

* at ``POST /agentic/start`` + ``/quick-start`` → the human is told immediately
  (HTTP 503) instead of getting a zombie run;
* at the top of ``drive_run`` → a worker-host problem is recorded on the run as
  a plain-language ``error`` + ``preflight_failed`` event, not a cryptic trace
  deep in a phase;
* via ``GET /agentic/preflight`` → the UI can show availability up-front.

Every problem string is written for a human operator, naming the exact thing to
fix. Empty list = all dependencies for code gen are in place.
"""
from __future__ import annotations

import logging
import re

from app.core.config import settings

logger = logging.getLogger("app.agentic")


def _mask(url: str) -> str:
    """Redact the password in a broker/db URL for safe display in an error."""
    return re.sub(r"://([^:/@]*):([^@]*)@", r"://\1:***@", url or "")


def _agentic_queue_problems() -> list[str]:
    """Verify at least one live worker is SUBSCRIBED to the agentic queue.

    `agentic.drive` / `.reverify` / `.push` / `.recover` are routed to
    ``settings.celery_agentic_queue`` (default "agentic"), while everything else
    goes to ``celery_default_queue``. A worker launched WITHOUT ``-Q`` consumes
    only the default queue, so every agentic message accumulates in Redis unread:
    runs are created but never driven, and — because `agentic.recover` is routed
    to that same unread queue — the recovery sweep that would rescue them is
    disabled by the identical mistake.

    Uses `inspect().active_queues()`, which reports what each worker is ACTUALLY
    consuming, unlike `ping()` which only proves a process is alive. Fail-OPEN:
    if inspection is unavailable (older broker, restricted control channel), we
    return no problems rather than blocking a start on a diagnostic we couldn't
    run — a false "everything is broken" is worse than the check not firing.
    """
    queue = settings.celery_agentic_queue
    try:
        from app.services.celery_tasks import celery_app
        active = celery_app.control.inspect(timeout=2.0).active_queues() or {}
    except Exception as e:  # noqa: BLE001 — diagnostic only, never block on it
        logger.warning("codegen preflight: active_queues inspect errored: %s", e)
        return []
    if not active:
        return []                      # inspection returned nothing usable → fail open
    subscribed = {
        q.get("name")
        for queues in active.values()
        for q in (queues or [])
        if isinstance(q, dict)
    }
    if queue in subscribed:
        return []
    return [
        f"No Celery worker is consuming the '{queue}' queue, so an agentic run would be "
        f"created but never start (it would sit at 'Getting ready' forever, with nothing "
        f"in the logs). Agentic tasks are routed to '{queue}', but the running worker(s) "
        f"only consume: {', '.join(sorted(s for s in subscribed if s)) or '(none)'}. "
        f"Restart the worker with the queue bound explicitly — "
        f"`celery -A app.services.celery_tasks worker "
        f"-Q {queue},{settings.celery_default_queue}` — or run a dedicated worker for "
        f"'{queue}' (see docs/OPERATIONAL_RUNBOOKS.md §3)."
    ]


def check_dependencies(*, include_worker: bool = True, include_build: bool = False) -> list[str]:
    """Return human-readable problems blocking agentic code gen (empty = healthy).

    ``include_worker`` pings the Celery worker pool — only meaningful from the API
    process (a worker can't check whether it itself is running). ``include_build``
    additionally requires the mvn/javac build toolchain; off by default because a
    missing build toolchain only DEGRADES verification (CI verifies) rather than
    blocking generation.
    """
    problems: list[str] = []

    # 1. Redis — the job queue. If it's down, a run can't even be scheduled.
    try:
        import redis
        redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()
    except Exception as e:  # noqa: BLE001 — any failure here means no queue
        problems.append(
            f"The job queue (Redis) is unreachable at {_mask(settings.redis_url)} — runs "
            "cannot be scheduled. Check that Redis is running and that REDIS_URL is correct, "
            f"INCLUDING the password if Redis requires one. [{type(e).__name__}: {str(e)[:120]}]"
        )
        return problems  # nothing downstream is reachable without the broker

    # 2. A background worker must be alive — else the run is created but never driven
    #    (the classic "stuck at Getting ready, nothing in the logs").
    if include_worker:
        replies = []
        try:
            from app.services.celery_tasks import celery_app
            replies = celery_app.control.ping(timeout=2.0) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("codegen preflight: worker ping errored: %s", e)
        if not replies:
            problems.append(
                "No background worker is running, so a code-gen run would be created but never "
                "start (it would sit at 'Getting ready' forever). Start the Celery worker on the "
                "backend host: `celery -A app.services.celery_tasks worker --loglevel=info "
                f"-Q {settings.celery_agentic_queue},{settings.celery_default_queue}` "
                "(with the same REDIS_URL — including the password — that the API uses)."
            )
        else:
            # A LIVE worker is not enough: agentic.drive is ROUTED to a dedicated queue
            # (task_routes in celery_tasks.py). A worker started without `-Q` consumes
            # ONLY task_default_queue, so the drive message lands in a queue nobody
            # reads — the run is created, never picked up, and the UI sits on "Getting
            # ready" forever with an empty event stream and NOTHING in any log. A plain
            # ping cannot see this (the worker answers, it just isn't subscribed), which
            # is exactly how this shipped silently. Inspect the actual subscriptions.
            problems.extend(_agentic_queue_problems())

    # 3. git + GitLab credentials — needed to clone the code being changed.
    try:
        from app.agents import toolchain_report
        report = toolchain_report.build_toolchain_report()
    except Exception as e:  # noqa: BLE001
        report = None
        problems.append(f"Could not run the toolchain preflight [{type(e).__name__}: {str(e)[:120]}].")
    if report is not None:
        if "git" in report.blocking_missing:
            problems.append("`git` is not installed on the backend host — repositories cannot be cloned.")
        if not report.gitlab_token_present:
            problems.append(
                "GITLAB_TOKEN is not configured — repositories cannot be cloned (authentication "
                "required). Set GITLAB_TOKEN in the backend/worker environment."
            )
        if not settings.gitlab_url:
            problems.append("GITLAB_URL is not configured — the platform doesn't know where to clone repositories from.")
        if include_build and not report.build_ready:
            missing = [t for t in ("mvn", "javac") if not (report.tools.get(t) and report.tools[t].found)]
            problems.append(
                f"Build toolchain missing ({', '.join(missing) or 'mvn/javac'}) — generated changes "
                "will be marked UNVERIFIED (a human/CI must verify the build)."
            )

    # 4. An LLM provider must be configured with a key, or the agent can't think.
    prov = (settings.llm_provider or "").lower()
    key_for = {
        "claude": settings.anthropic_api_key,
        "ainxt": settings.ainxt_api_key,
        "openai": settings.openai_api_key,
        "gemini": settings.gemini_api_key,
    }
    if prov in key_for and not (key_for[prov] or "").strip():
        problems.append(
            f"No API key configured for the LLM provider '{prov}' (set the matching *_API_KEY) — "
            "the code-gen agent cannot call the model."
        )

    return problems


def assert_ready_or_message(*, include_worker: bool = True) -> str | None:
    """Convenience: return a single multi-line human message if anything is broken,
    else None. Used by the API to raise a 503 and by the worker to record run.error."""
    problems = check_dependencies(include_worker=include_worker)
    if not problems:
        return None
    return "Agentic code generation can't run right now:\n" + "\n".join(f"• {p}" for p in problems)
