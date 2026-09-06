# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Log viewer API.

GET  /api/logs        — return last N buffered log entries (JSON)
GET  /api/logs/stream — Server-Sent Events stream of real-time log entries
"""
import asyncio
import json
import os
import queue
from typing import AsyncGenerator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.core.deps import AdminUser
from app.core import log_buffer

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("")
def get_logs(
    n: int = Query(default=200, ge=1, le=1000, description="Number of recent entries to return"),
    _: AdminUser = None,
):
    """Return the last *n* log entries from the shared log file."""
    return {"entries": log_buffer.get_recent(n), "count": n}


@router.get("/stream")
async def stream_logs(_: AdminUser = None):
    """
    Server-Sent Events endpoint. Combines:
    1. In-process log entries (from this worker's BufferHandler)
    2. File-based tail (catches logs from other workers)

    Each SSE event has the form:
        data: {"ts":"...","level":"INFO","logger":"...","message":"..."}\n\n
    """
    q = log_buffer.subscribe()

    # Track file position for tailing cross-worker logs
    log_file = log_buffer._LOG_FILE
    file_pos = 0
    if log_file.exists():
        file_pos = os.path.getsize(log_file)

    seen_messages = set()  # deduplicate between in-process and file-tail

    async def event_generator() -> AsyncGenerator[str, None]:
        nonlocal file_pos
        yield ": connected\n\n"
        try:
            while True:
                # 1. Drain in-process queue (fast path — same worker)
                while True:
                    try:
                        entry = q.get_nowait()
                        key = f"{entry.get('ts')}:{entry.get('message', '')[:80]}"
                        seen_messages.add(key)
                        # Keep seen set bounded
                        if len(seen_messages) > 2000:
                            seen_messages.clear()
                        yield f"data: {json.dumps(entry)}\n\n"
                    except queue.Empty:
                        break

                # 2. Tail the log file (catches other workers' entries)
                try:
                    if log_file.exists():
                        current_size = os.path.getsize(log_file)
                        if current_size < file_pos:
                            # File was rotated
                            file_pos = 0
                        if current_size > file_pos:
                            with open(log_file, "r", encoding="utf-8") as f:
                                f.seek(file_pos)
                                new_data = f.read()
                                file_pos = f.tell()
                            for line in new_data.strip().split("\n"):
                                if not line.strip():
                                    continue
                                try:
                                    entry = json.loads(line)
                                    key = f"{entry.get('ts')}:{entry.get('message', '')[:80]}"
                                    if key not in seen_messages:
                                        seen_messages.add(key)
                                        yield f"data: {json.dumps(entry)}\n\n"
                                except json.JSONDecodeError:
                                    pass
                except Exception:
                    pass

                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            pass
        finally:
            log_buffer.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":       "no-cache",
            "X-Accel-Buffering":   "no",   # disable nginx buffering
            "Connection":          "keep-alive",
        },
    )
