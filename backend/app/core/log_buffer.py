# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Log buffer — file-backed rotating log store + console stream routing.

All uvicorn workers write log records to a shared JSON-lines file.
The /api/logs REST endpoint reads recent entries from the file.
The /api/logs/stream SSE endpoint tails the file in real time.

This works correctly with multiple uvicorn workers (each worker writes
to the same file) and survives restarts (log history persists on disk).

Console routing (``install`` below) follows the standard Unix convention:

    stdout   DEBUG, INFO                   — "what the system is doing"
    stderr   WARNING, ERROR, CRITICAL      — "what needs attention"

so `docker compose logs backend 2>/dev/null` shows normal activity and
`... 1>/dev/null` shows only problems. Uvicorn's own loggers are folded into the
same rule (see ``align_uvicorn_loggers``). This is a routing decision ONLY: the
format, the level thresholds and the set of lines emitted are unchanged, and the
file sinks (app.jsonl here; codegen/commands/build/verify/llm_calls in
``app.core.diag`` and ``app.core.observability``) are untouched by it.
"""
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque

from app.core.config import settings


# ── Log redaction filter ────────────────────────────────────────────────────
# Defense-in-depth: strip credentials / secrets from log messages before they
# reach any handler (file, console, SSE). Patterns kept intentionally broad.

_LOG_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "bearer [REDACTED]"),
    # Whole header value (`.+`, not `\S+`): an `Authorization: Bearer <jwt>` line
    # otherwise only loses the word "Bearer" and leaks the token after it.
    (re.compile(r"(?i)\b(private-token|authorization)\s*[:=]\s*.+"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD))\s*=\s*\S+"), r"\1=[REDACTED]"),
    # Credentials embedded in a URL authority (`scheme://user:pass@host`).
    #
    # The userinfo segment is `[^:/\s]*` — a STAR, not a plus — because the
    # password-only form `scheme://:pass@host` has an EMPTY username, and a `+`
    # there fails to match it and leaks the password verbatim. That is not a
    # hypothetical shape: it is exactly what this project deploys. All three
    # REDIS_URL values in docker-compose.yml are
    # `redis://:${REDIS_PASSWORD}@redis:6379/0`, so the one URL form most likely
    # to appear in a connection-error log was the one form not being redacted.
    # Verified against `redis://:pw@`, `postgresql://:pw@` and `amqp://:pw@`,
    # and confirmed not to alter non-credential text such as
    # `https://host/path?x=1`, `http://example.com:8080/api` or `mailto:a@b.c`.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s]*):[^@/\s]+@"), r"\1:[REDACTED]@"),
    # Same credentials, but in a URL with NO `//` after the scheme:
    # `scheme:user:pass@host` and `scheme:/user:pass@host`. The pattern above
    # requires the `//`, so it does not fire on these. urlparse() reports the
    # scheme as normal for both, which means such a URL passes an
    # `if parsed.scheme != "https"` style validation gate and can then reach a
    # log line (see `_validate_endpoint_url` in api/partners.py). The optional
    # single `/` is matched so the `scheme:/user:pass@host` variant is covered.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*:/?)([^:/\s@]*):[^@/\s]+@"), r"\1\2:[REDACTED]@"),
)


def redact_text(text: str) -> str:
    """Apply every redaction pattern to a string.

    Extracted so the traceback path can reuse the exact same patterns as the
    message path. When these were two separate code paths, one of them (the
    traceback) simply did not redact at all.
    """
    for pattern, repl in _LOG_REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactionFilter(logging.Filter):
    """Scrub sensitive patterns from log record messages AND tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            # Format message with args first, then redact the result
            try:
                record.msg = record.msg % record.args
                record.args = None
            except Exception:
                # `%` failed (usually a placeholder/arg count mismatch). The
                # unformatted args still hold whatever was passed in, so they
                # must not survive: logging's own error handler prints
                # "Arguments: (...)" to stderr, and `Formatter.format` would
                # retry the same `%` and raise again. Redact each arg
                # individually and keep them, so the record still renders.
                try:
                    record.args = tuple(
                        redact_text(a) if isinstance(a, str) else a
                        for a in (record.args if isinstance(record.args, tuple)
                                  else (record.args,))
                    )
                except Exception:
                    record.args = None
        record.msg = redact_text(str(record.msg))

        # ── TRACEBACKS ───────────────────────────────────────────────────────
        # An exception's own text is a first-class leak channel and was
        # completely unfiltered before. `logger.exception(...)` /
        # `exc_info=True` is used at 153 sites in this codebase, and the
        # exception message frequently contains the thing that failed —
        # including connection URLs with embedded passwords, e.g.
        #   ConnectionError: Error connecting to redis://:PASSWORD@redis:6379/0
        # `BufferHandler.emit` formats `record.exc_info` into the log FILE
        # independently of `record.msg`, so redacting only the message left the
        # credential in `entry["exc"]` (and in the console traceback).
        #
        # `exc_text` is the stdlib's own cache of the formatted traceback:
        # `Formatter.format` populates it and REUSES it for later handlers, so
        # filling it here with a redacted rendering means every downstream
        # handler gets the safe version instead of re-formatting the raw one.
        if record.exc_info:
            try:
                import traceback as _tb
                record.exc_text = redact_text(
                    "".join(_tb.format_exception(*record.exc_info))
                )
            except Exception:
                # Never let redaction failure drop a log record: a missing
                # traceback is far better than an unredacted one, and far
                # better than a crash inside the logging path.
                record.exc_text = "<traceback withheld: redaction failed>"
            record.exc_info = None
        if getattr(record, "stack_info", None):
            try:
                record.stack_info = redact_text(str(record.stack_info))
            except Exception:
                record.stack_info = None
        return True


# ── Log file path ────────────────────────────────────────────────────────────

_LOG_DIR = Path(os.environ.get("LOG_DIR", "/tmp/cm-platform-logs"))
_LOG_FILE = _LOG_DIR / "app.jsonl"
_MAX_LINES = 5000  # rotate after this many lines


def _ensure_log_dir():
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── In-process buffer for SSE listeners ──────────────────────────────────────

_lock = threading.Lock()
_listeners: list = []  # SSE subscriber queues (in-process only)


def _notify_listeners(entry: dict) -> None:
    with _lock:
        for q in _listeners:
            try:
                q.put_nowait(entry)
            except Exception:
                pass


def subscribe():
    """Return a queue that will receive every new log entry from this worker."""
    import queue
    q: queue.Queue = queue.Queue(maxsize=500)
    with _lock:
        _listeners.append(q)
    return q


def unsubscribe(q) -> None:
    with _lock:
        try:
            _listeners.remove(q)
        except ValueError:
            pass


# ── File operations ──────────────────────────────────────────────────────────

def _append_entry(entry: dict) -> None:
    """Append a log entry to the shared JSON-lines file."""
    _ensure_log_dir()
    try:
        line = json.dumps(entry, default=str) + "\n"
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # logging should never crash the app

    # Rotate if file is too large
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > _MAX_LINES:
            with open(_LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-(_MAX_LINES // 2):])
    except Exception:
        pass


def get_recent(n: int = 200) -> list[dict]:
    """Return the last *n* log entries from the shared log file (oldest first)."""
    _ensure_log_dir()
    if not _LOG_FILE.exists():
        return []
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for line in lines[-(n):]:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries
    except Exception:
        return []


# ── Logging handler ──────────────────────────────────────────────────────────

LEVEL_MAP = {
    logging.DEBUG:    "DEBUG",
    logging.INFO:     "INFO",
    logging.WARNING:  "WARNING",
    logging.ERROR:    "ERROR",
    logging.CRITICAL: "CRITICAL",
}


class BufferHandler(logging.Handler):
    """Python logging handler that writes records to file + notifies SSE."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts":      datetime.now(timezone.utc).isoformat(),
                "level":   LEVEL_MAP.get(record.levelno, record.levelname),
                "logger":  record.name,
                "message": self.format(record),
            }
            # Prefer `exc_text`: `RedactionFilter` puts the REDACTED traceback
            # there and clears `exc_info`. Re-formatting `exc_info` here would
            # rebuild the raw traceback and undo that scrubbing, which is
            # exactly the bug this branch used to have — the message was
            # redacted while `entry["exc"]` carried the credential verbatim.
            # `redact_text` is still applied as a backstop for records that
            # reach this handler without passing the filter.
            exc_text = getattr(record, "exc_text", None)
            if not exc_text and record.exc_info:
                import traceback
                exc_text = "".join(traceback.format_exception(*record.exc_info))
            if exc_text:
                entry["exc"] = redact_text(exc_text)
            _append_entry(entry)
            _notify_listeners(entry)
        except Exception:
            self.handleError(record)


LOG_FORMAT = "%(asctime)s  %(levelname)-5s  [%(name)s]  %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _MaxLevelFilter(logging.Filter):
    """Pass only records STRICTLY BELOW ``ceiling`` — the stdout half of the
    console severity split.

    ``Handler.setLevel`` gives us a *floor* but the stdlib has no *ceiling*, so
    sending DEBUG/INFO to stdout while WARNING+ goes to stderr needs this: the
    stdout handler carries the ceiling, the stderr handler carries the floor.
    Without it both handlers would fire for a WARNING and every warning would
    appear twice on a merged view.
    """

    def __init__(self, ceiling: int) -> None:
        super().__init__()
        self.ceiling = ceiling

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.ceiling


def align_uvicorn_loggers() -> None:
    """Make uvicorn's loggers obey the root console severity split.

    Uvicorn's built-in LOGGING_CONFIG hard-codes `uvicorn.error` → stderr and
    `uvicorn.access` → stdout with `propagate: False`. Clearing its handlers and
    re-enabling propagation routes those records through the root handlers, so
    an INFO startup line lands on stdout like every other INFO line.

    Must be called AGAIN after the server boots: uvicorn applies its dictConfig
    when it starts, i.e. after `app.main` is imported, which re-adds the very
    handlers we remove here. Idempotent and safe to call repeatedly.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def install(level: int = logging.DEBUG) -> None:
    """Attach *BufferHandler* to the root logger and configure console format."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, BufferHandler):
            return  # already installed

    # Allow all records through the root logger
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    # Redaction is attached PER HANDLER, never to the root logger.
    # (Also closes Checkmarx SCR finding #2, "Filtering Sensitive Logs", which
    # identified the same root-logger misattachment independently.)
    #
    # `Logger.callHandlers` runs the emitting logger's own filters plus each
    # HANDLER's filters — it never consults an ancestor logger's filter list. So
    # `root.addFilter(...)` only ever scrubbed records logged directly against
    # root, while every `logging.getLogger(__name__)` module in this codebase
    # (i.e. essentially all of them) bypassed it and leaked secrets to both the
    # console and app.jsonl. Handler-level filters run no matter which logger
    # emitted the record, which is the property we need.
    # Regression test: tests/core/test_log_redaction_propagation.py.
    #
    # NOTE: one RedactionFilter INSTANCE per handler. The filter rewrites
    # `record.msg` in place and clears `record.args`, and the same record object
    # is passed to every handler in turn; sharing one instance is still correct
    # (the second pass is a no-op on already-scrubbed text) but per-handler
    # instances keep each sink independent, matching diag.py's wiring.
    def _redacted(h: logging.Handler) -> logging.Handler:
        h.addFilter(RedactionFilter())
        return h

    # Buffer handler — writes to file + feeds SSE
    handler = BufferHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root.addHandler(_redacted(handler))

    # ── Console handlers — severity-ROUTED so the two streams mean something ──
    #
    #   stdout = "what the system is doing"  (DEBUG/INFO)
    #   stderr = "what needs attention"      (WARNING/ERROR/CRITICAL)
    #
    # Previously a single bare `logging.StreamHandler()` sent EVERYTHING to
    # stderr (that's the stdlib default when no stream is passed), so `2>` could
    # not isolate problems and `1>` showed nothing. Format, level threshold and
    # volume are all unchanged — only the destination differs.
    #
    # NOTE: because these are two file descriptors, a MERGED view (`docker logs`,
    # `2>&1 | cat`) can show a WARNING marginally out of order against a
    # neighbouring INFO. That is inherent to the split, not a bug: `app.jsonl`
    # (BufferHandler above) remains the single strictly-ordered record with a
    # millisecond `ts` per entry, and is what /api/logs + the Admin viewer read.
    console_fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setLevel(logging.INFO)                        # unchanged threshold
    stdout_h.addFilter(_MaxLevelFilter(logging.WARNING))   # ceiling: INFO and below
    stdout_h.setFormatter(console_fmt)
    root.addHandler(_redacted(stdout_h))

    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setLevel(logging.WARNING)
    stderr_h.setFormatter(console_fmt)
    root.addHandler(_redacted(stderr_h))

    # stdout is BLOCK-buffered when piped (stderr is line-buffered), so without
    # this INFO would reach `docker logs` in delayed ~4-8 KB bursts while
    # warnings appeared instantly — looking like lag and reordering. The
    # Dockerfiles also set PYTHONUNBUFFERED=1; this covers non-container runs.
    # Guarded: `reconfigure` needs a real TextIOWrapper and pytest's capture
    # object (or a redirected StringIO) does not have it.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass

    # Uvicorn ships its OWN handlers (uvicorn.error → stderr, uvicorn.access →
    # stdout) with `propagate: False`, and nothing here passes --log-config. That
    # would keep INFO-level startup/lifecycle lines on stderr, contradicting the
    # split. Drop its handlers and let the records propagate to root instead, so
    # framework and application lines obey ONE rule. Re-applied on FastAPI
    # startup (app.main) because uvicorn runs dictConfig after app import.
    align_uvicorn_loggers()

    # Reduce noise from chatty libraries
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
