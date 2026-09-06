# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: WebSocket-streaming bridge for ws_product_kit.
#
# WHY a dedicated module: the host's WebSocket handler in agents.py speaks a
# specific protocol — `{type: "chunk"|"job_id"|"history"|"error", ...}`.
# The engine's natural emit shape is `JobProgress` (status + message + counts
# + open_questions). We translate one to the other in this single place so
# the WS handler stays thin and the engine doesn't grow WS knowledge.
#
# WHY also stream the markdown companion: the existing ProductKit page
# renders incoming `chunk` events with ReactMarkdown. To keep the user
# experience identical to the legacy flow, we stream the rendered Markdown
# (produced by `excel_writer.exporters.to_markdown`) as a single `chunk`
# message at the very end — once the workbook is rendered + validated. The
# stage progress fires as `progress` events along the way (the existing
# frontend hook ignores unknown types, so progress events are non-breaking).

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from app.excel_testcase_engine.adapters import jobs as jobs_adapter
from app.excel_testcase_engine.excel_writer.exporters import to_markdown
from app.excel_testcase_engine.observability import get_logger
from app.excel_testcase_engine.orchestrator.graph import (
    compute_progress_pct,
    run_workflow,
)
from app.excel_testcase_engine.orchestrator.status import JobProgress, JobStatus
from app.excel_testcase_engine.schemas.workbook_plan import WorkbookPlan

LOGGER = get_logger("excel_engine.stream")


def _wb_plan_for_job(job_id: str, artifacts_root: Path) -> WorkbookPlan | None:
    """Reload the rendered plan from artifacts so we can produce the markdown
    companion for `chunk` streaming. WHY a re-read instead of plumbing the
    plan through state: keeps `run_workflow` returning a Path (the simple
    contract) and lets us also reuse this for resume from another process."""

    p = artifacts_root / job_id / "03-rendered_plan.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return WorkbookPlan.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("stream.replan_load_failed", error=repr(exc))
        return None


async def run_workflow_for_ws(
    *,
    websocket,                # fastapi.WebSocket — duck-typed
    brief: str,
    options: dict,
    registry_job_id: str,
    artifacts_root: Path,
    job_registry,             # app.services.job_registry module
    db,                       # SQLAlchemy session — used by job_registry helpers
) -> dict:
    """Drive the engine for a ws_product_kit cert_test_cases turn.

    Returns a result dict with:
      - markdown: str  — the rendered companion text
      - xlsx_path: str — absolute path of the rendered workbook
      - md_path:   str — absolute path of the markdown companion file
      - docx_path: str — absolute path of the docx companion file

    Caller persists `markdown` as the assistant Conversation entry and uses
    the file paths to update the host's job registry / ProductKitDocument.
    """

    # WHY a queue: the engine's progress callback runs synchronously from
    # whichever thread/task LangGraph dispatches the node on. We can't await
    # the WS send from that callback. The queue lets the WS coroutine drain
    # events in order while the engine keeps running concurrently.
    queue: asyncio.Queue[JobProgress | None] = asyncio.Queue(maxsize=512)

    def on_progress(progress: JobProgress) -> None:
        try:
            queue.put_nowait(progress)
        except asyncio.QueueFull:
            LOGGER.warning("stream.queue_full", job=registry_job_id)

    job_id = str(uuid.uuid4())

    async def drain():
        """Forward engine progress events to the WS in the host protocol.

        Pushes progress_pct to BOTH the WS event payload (so the loader can
        render a real progress bar live) AND the host job_registry (so the
        sidebar / banner show movement when the user navigates away and
        back). Single source of truth — every event flows through here.
        """

        while True:
            event = await queue.get()
            if event is None:
                return
            stage_value = event.status.value if hasattr(event.status, "value") else str(event.status)
            pct = compute_progress_pct(stage_value, int(event.current or 0), int(event.total or 0))

            # WHY drain owns ALL registry writes (single source of truth):
            # graph._emit used to also call jobs_adapter.update_stage which
            # caused 2× DB writes for stage transitions. We removed _emit's
            # write so every progress event — whether from _emit or the
            # writer's bypass callback — funnels through this one path.
            #
            # Uses jobs_adapter.update_stage (not job_registry.update_job
            # directly) so it opens its own SessionLocal via the configured
            # session_factory rather than reusing the WS handler's request-
            # scoped `db` session. Decouples the drain task's DB lifecycle
            # from the WS handler's transaction — a rollback in one no
            # longer affects the other, and it's safe even if a future
            # change adds an `await` inside a sync section.
            #
            # Best-effort — telemetry must never fail the run.
            try:
                jobs_adapter.update_stage(
                    registry_job_id,
                    event.message or stage_value,
                    progress_pct=pct,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("stream.update_stage_failed", error=repr(exc))

            try:
                await websocket.send_text(json.dumps({
                    "type": "progress",
                    "job_id": registry_job_id,
                    "current_stage": event.message or stage_value,
                    "stage": stage_value,
                    "message": event.message,
                    "current": event.current,
                    "total": event.total,
                    "progress_pct": pct,    # ← new: lets the frontend draw a real bar live
                    "open_questions": event.open_questions,
                }))
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("stream.send_failed", error=repr(exc))

    drain_task = asyncio.create_task(drain())

    try:
        rendered_path = await run_workflow(
            brief=brief,
            options=options,
            on_progress=on_progress,
            job_id=job_id,
            registry_job_id=registry_job_id,
        )
    finally:
        await queue.put(None)  # signal drain to stop
        try:
            await asyncio.wait_for(drain_task, timeout=2.0)
        except asyncio.TimeoutError:
            drain_task.cancel()

    # Load the rendered plan, render markdown, stream it as a `chunk` so
    # the existing frontend ReactMarkdown renders it without UI changes.
    plan = _wb_plan_for_job(job_id, artifacts_root)
    markdown = to_markdown(plan) if plan else f"# Workbook generated\n\nFile: `{rendered_path.name}`"

    # WHY use job_registry.ws_send_chunk: it persists the chunk to the
    # durable chunk buffer so resume-on-reconnect picks up the same content.
    # Tolerate WS-already-closed: the markdown is still saved server-side via
    # the result_payload + ProductKitDocument, so the client recovers via REST.
    try:
        await job_registry.ws_send_chunk(websocket, registry_job_id, markdown)
    except Exception as exc:  # noqa: BLE001
        # Fallback: send a plain chunk if the registry helper fails for any reason.
        LOGGER.warning("stream.chunk_persist_failed_fallback_send", error=repr(exc))
        try:
            await websocket.send_text(json.dumps({"type": "chunk", "text": markdown}))
        except Exception as inner:  # noqa: BLE001
            LOGGER.info("stream.chunk_send_after_close", error=repr(inner))

    # Companion file paths — engine's _attach_files writes them next to the
    # rendered workbook (via excel_writer.exporters.write_companions). They
    # share the workbook's basename, only the extension differs.
    rp = Path(rendered_path)
    md_path = rp.with_suffix(".md")
    docx_path = rp.with_suffix(".docx")
    json_path = rp.with_suffix(".json")

    return {
        "markdown":  markdown,
        "xlsx_path": str(rp),
        "md_path":   str(md_path) if md_path.exists() else "",
        "docx_path": str(docx_path) if docx_path.exists() else "",
        "json_path": str(json_path) if json_path.exists() else "",
    }


__all__ = ["run_workflow_for_ws"]
