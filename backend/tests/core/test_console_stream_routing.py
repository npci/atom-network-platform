# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the console stdout/stderr severity routing in app.core.log_buffer.

The contract under test (and the reason it exists): a bare
``logging.StreamHandler()`` defaults to stderr, so before this split EVERY app
log line — INFO included — went to stderr and neither `1>` nor `2>` could
separate normal activity from problems. Now:

    stdout   DEBUG, INFO
    stderr   WARNING, ERROR, CRITICAL

These tests pin the routing, the no-double-emit property (the whole point of
``_MaxLevelFilter``), and the fact that format/threshold were NOT changed.

Pure: no network, no DB, no LLM. Each test installs into a throwaway root
logger and restores the real one, so pytest's own capture is never disturbed.
"""
from __future__ import annotations

import io
import logging
import sys

import pytest

from app.core import log_buffer


@pytest.fixture
def clean_root(monkeypatch, tmp_path):
    """Give `install()` a pristine root logger and a temp app.jsonl.

    `install()` mutates the process-wide root logger and appends to
    `_LOG_FILE`; without isolation the first test to run would leave handlers
    behind and poison both the rest of the suite and the developer's real log
    file. Handlers/filters/level are captured and restored verbatim.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_filters = root.filters[:]
    saved_level = root.level

    root.handlers.clear()
    root.filters.clear()

    # Redirect the file sink so the test never touches the real app.jsonl.
    monkeypatch.setattr(log_buffer, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(log_buffer, "_LOG_FILE", tmp_path / "app.jsonl")

    yield root

    root.handlers.clear()
    root.filters.clear()
    root.handlers.extend(saved_handlers)
    root.filters.extend(saved_filters)
    root.setLevel(saved_level)


def _stream_handlers(root):
    """The two console handlers only.

    Filtered by stream IDENTITY rather than by type: the app.jsonl
    ``BufferHandler`` and pytest's own capture handler are both
    ``StreamHandler``-shaped, and pytest re-attaches its handler to root around
    each test phase, so a type check alone would pick them up.
    """
    return [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and getattr(h, "stream", None) in (sys.stdout, sys.stderr)
    ]


# ──────────────────────────────────────────────────────────────────────────────
# _MaxLevelFilter — the ceiling primitive
# ──────────────────────────────────────────────────────────────────────────────

class TestMaxLevelFilter:

    def _record(self, level: int) -> logging.LogRecord:
        return logging.LogRecord("t", level, __file__, 1, "m", None, None)

    @pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
    def test_passes_records_below_ceiling(self, level):
        assert log_buffer._MaxLevelFilter(logging.WARNING).filter(self._record(level)) is True

    @pytest.mark.parametrize("level", [logging.WARNING, logging.ERROR, logging.CRITICAL])
    def test_blocks_records_at_or_above_ceiling(self, level):
        assert log_buffer._MaxLevelFilter(logging.WARNING).filter(self._record(level)) is False


# ──────────────────────────────────────────────────────────────────────────────
# Handler wiring
# ──────────────────────────────────────────────────────────────────────────────

class TestConsoleHandlerWiring:

    def test_installs_exactly_one_stdout_and_one_stderr_handler(self, clean_root):
        log_buffer.install()
        streams = [h.stream for h in _stream_handlers(clean_root)]
        assert streams.count(sys.stdout) == 1
        assert streams.count(sys.stderr) == 1

    def test_stdout_handler_carries_the_ceiling_and_stderr_does_not(self, clean_root):
        log_buffer.install()
        by_stream = {h.stream: h for h in _stream_handlers(clean_root)}

        out = by_stream[sys.stdout]
        err = by_stream[sys.stderr]

        assert any(isinstance(f, log_buffer._MaxLevelFilter) for f in out.filters), \
            "stdout handler needs the ceiling filter or warnings would be emitted twice"
        assert not any(isinstance(f, log_buffer._MaxLevelFilter) for f in err.filters)

    def test_thresholds_are_unchanged_by_the_split(self, clean_root):
        """The console stayed at INFO; only the destination changed."""
        log_buffer.install()
        by_stream = {h.stream: h for h in _stream_handlers(clean_root)}
        assert by_stream[sys.stdout].level == logging.INFO
        assert by_stream[sys.stderr].level == logging.WARNING

    def test_both_console_handlers_keep_the_existing_format(self, clean_root):
        log_buffer.install()
        for h in _stream_handlers(clean_root):
            assert h.formatter._fmt == log_buffer.LOG_FORMAT
            assert h.formatter.datefmt == log_buffer.LOG_DATE_FORMAT

    def test_install_is_idempotent(self, clean_root):
        log_buffer.install()
        first = len(clean_root.handlers)
        log_buffer.install()
        assert len(clean_root.handlers) == first, "second install() duplicated handlers"


# ──────────────────────────────────────────────────────────────────────────────
# End-to-end routing — the behaviour operators actually observe
# ──────────────────────────────────────────────────────────────────────────────

class TestRoutingBehaviour:

    @pytest.fixture
    def captured(self, clean_root):
        """Install, then repoint the two console handlers at in-memory buffers."""
        log_buffer.install()
        bufs = {}
        for h in _stream_handlers(clean_root):
            target = "out" if h.stream is sys.stdout else "err"
            bufs[target] = io.StringIO()
            h.setStream(bufs[target])
        return bufs

    @pytest.mark.parametrize("level,expect", [
        ("info", "out"),
        ("warning", "err"),
        ("error", "err"),
        ("critical", "err"),
    ])
    def test_record_lands_on_the_expected_stream_only(self, captured, level, expect):
        other = "err" if expect == "out" else "out"
        getattr(logging.getLogger("app.routing_test"), level)("MARKER-%s", level)

        assert "MARKER" in captured[expect].getvalue()
        assert "MARKER" not in captured[other].getvalue(), \
            f"{level} leaked onto {other} — double emit or wrong route"

    def test_warning_is_emitted_once_not_twice(self, captured):
        logging.getLogger("app.routing_test").warning("ONCE")
        assert captured["err"].getvalue().count("ONCE") == 1

    def test_debug_reaches_neither_console_stream(self, captured):
        """Console threshold is INFO; DEBUG stays in app.jsonl only."""
        logging.getLogger("app.routing_test").debug("DEBUG-MARKER")
        assert "DEBUG-MARKER" not in captured["out"].getvalue()
        assert "DEBUG-MARKER" not in captured["err"].getvalue()


# ──────────────────────────────────────────────────────────────────────────────
# Uvicorn alignment
# ──────────────────────────────────────────────────────────────────────────────

class TestUvicornAlignment:

    @pytest.fixture(autouse=True)
    def _restore_uvicorn_loggers(self):
        names = ("uvicorn", "uvicorn.error", "uvicorn.access")
        saved = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).propagate)
                 for n in names}
        yield
        for n, (handlers, propagate) in saved.items():
            lg = logging.getLogger(n)
            lg.handlers.clear()
            lg.handlers.extend(handlers)
            lg.propagate = propagate

    def test_clears_handlers_and_enables_propagation(self):
        """Uvicorn pins uvicorn.error→stderr / uvicorn.access→stdout with
        propagate=False. Alignment hands those records to root so they obey the
        same severity rule as application logs."""
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            lg.addHandler(logging.StreamHandler(io.StringIO()))
            lg.propagate = False

        log_buffer.align_uvicorn_loggers()

        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            assert lg.handlers == []
            assert lg.propagate is True

    def test_is_idempotent(self):
        log_buffer.align_uvicorn_loggers()
        log_buffer.align_uvicorn_loggers()
        assert logging.getLogger("uvicorn.access").handlers == []
        assert logging.getLogger("uvicorn.access").propagate is True
