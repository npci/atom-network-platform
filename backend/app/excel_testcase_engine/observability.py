# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Structured logging, metrics, and exception classes shared across the package.

A single logger is configured for the whole package via ``configure_logging``.
Production deployments should call this once at startup; in tests it is invoked
implicitly the first time a logger is requested. Output is JSON for machine
consumers and pretty for TTY.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import structlog

_CONFIGURED = False


def configure_logging(level: str | int | None = None, *, json_logs: bool | None = None) -> None:
    """Configure structlog once. Idempotent."""

    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved_level = level or os.getenv("UPI_LOG_LEVEL", "INFO")
    if isinstance(resolved_level, str):
        resolved_level = getattr(logging, resolved_level.upper(), logging.INFO)
    # stdout, not stderr: this package's INFO ("stage.start"/"stage.end") is normal
    # activity, and structlog's default PrintLoggerFactory already writes to stdout
    # — so the previous stderr basicConfig split this engine's own INFO across BOTH
    # streams. Matches the platform-wide console rule in app/core/log_buffer.py
    # (DEBUG/INFO → stdout, WARNING+ → stderr).
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=resolved_level)

    if json_logs is None:
        json_logs = not sys.stdout.isatty() or os.getenv("UPI_LOG_JSON") == "1"

    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger; configures logging on first use."""

    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)


@contextmanager
def stage_timer(logger: structlog.stdlib.BoundLogger, stage: str, **fields: Any) -> Iterator[dict]:
    """Time a pipeline stage and emit structured start/end events."""

    started = time.perf_counter()
    out: dict[str, Any] = {"stage": stage, **fields}
    logger.info("stage.start", **out)
    try:
        yield out
        out["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        logger.info("stage.end", **out)
    except Exception as exc:
        out["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        out["error"] = repr(exc)
        logger.error("stage.failed", **out)
        raise


class MetricsRecorder:
    """Append JSONL run metrics to a single file. Filesystem-only, no remote sink."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or Path("outputs") / "metrics.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


# --- Exceptions --------------------------------------------------------------


class EngineError(Exception):
    """Base class for all package-level errors."""


class ConfigurationError(EngineError):
    """Raised when YAML config is malformed or required env is missing."""


class LLMProviderError(EngineError):
    """LLM provider raised an error after retries were exhausted."""


class PlanningError(EngineError):
    """Planner could not produce a schema-valid WorkbookPlan after retries."""


class WriterError(EngineError):
    """Writer could not produce schema-valid RenderedTestCase batches."""


class ValidationFailure(EngineError):
    """Raised when a critical defect is unrepairable. Carries the report for the caller."""

    def __init__(self, message: str, report: Any) -> None:
        super().__init__(message)
        self.report = report
