# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Diagnostics logging — ONE findable, fail-open home for code-gen + build logs.

Why this exists: the code-gen pipeline already logs richly (LLM turns with
timing/tokens, every tool call with its own ms, phase transitions) — but it
runs in the **Celery worker**, which never installed file logging, so all of
that only ever reached the worker's stdout. And the legacy Phase-B build wrote
its log to the DB + a 300-char-truncated app log, while the internal mvn/git
commands (which folder, exit code, duration) were logged nowhere. This module
collects all of it under one writable directory.

Sinks (all under the resolved root):

    codegen.log            normal: the agentic run story — `app.agentic` /
                           `app.agents` loggers (LLM turns, tool calls, phase
                           transitions, all already timestamped + timed).
    codegen.debug.log      verbose mirror — ONLY when DIAG_VERBOSE=1, so the
                           normal file stays small.
    commands.log           every system command run via the platform adapter:
                           argv, cwd (which folder), exit code, duration.
    build/<id>-<ts>.log    full, untruncated build+deploy output, one file
                           per build, with a command/host header + exit footer.
    llm_calls.jsonl        one JSON row per LLM call (written by observability).

Root resolution — first WRITABLE wins (probe-tested), in order:

    1. settings.diag_log_dir            operator override (DIAG_LOG_DIR)
    2. <running-code dir>/logs/diagnostics
                                        == /app/logs/diagnostics in the
                                        container, bind-mounted to ./logs on the
                                        host. This is the "log next to the
                                        running code, no permission surprises"
                                        default the user asked for.
    3. ~/.cm-diag                     per-user fallback (home is writable even
                                        when /app is read-only).
    4. <tempdir>/cm-diag             last resort.

If even temp is unwritable, every helper degrades to a silent no-op — logging
must NEVER break a build or a run.
"""
from __future__ import annotations

import logging
import re
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.log_buffer import RedactionFilter

logger = logging.getLogger("app.diag")

# Per-file rotation ceilings. Normal files small (most diagnostics are a few
# lines per turn); the debug + command files a bit larger since they're chattier.
_CODEGEN_MAX_BYTES = 25 * 1024 * 1024   # 25 MB
_CODEGEN_BACKUPS = 5                     # → ~150 MB ceiling for codegen.log
_COMMAND_MAX_BYTES = 25 * 1024 * 1024
_COMMAND_BACKUPS = 5
_BUILD_KEEP = 60                         # newest N per-build files retained

# /app/app/core/diag.py → parents[2] == /app  (the running-code dir)
_CODE_ROOT = Path(__file__).resolve().parents[2]

_FMT = logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
                         "%Y-%m-%d %H:%M:%S")

_resolved_root: Optional[Path] = None
_resolve_attempted = False
_installed = False

# Dedicated, NON-propagating logger for system commands so the (chatty) git/mvn
# command stream lands only in commands.log, not doubled into the main app log.
_cmd_logger = logging.getLogger("app.command")
_cmd_logger.propagate = False


# ── root resolution ──────────────────────────────────────────────────────────

def _candidates() -> list[str]:
    return [
        settings.diag_log_dir,
        str(_CODE_ROOT / "logs" / "diagnostics"),
        str(Path.home() / ".cm-diag"),
        str(Path(tempfile.gettempdir()) / "cm-diag"),
    ]


def diag_dir() -> Optional[Path]:
    """First writable diagnostics root, cached. Returns None only if every
    candidate (down to the OS temp dir) is unwritable — then all helpers no-op."""
    global _resolved_root, _resolve_attempted
    if _resolve_attempted:
        return _resolved_root
    _resolve_attempted = True
    for cand in _candidates():
        if not cand:
            continue
        try:
            d = Path(cand)
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"      # prove it's writable, not just present
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue
        _resolved_root = d
        return d
    return None


# ── handler wiring ───────────────────────────────────────────────────────────

def _make_handler(filename: str, level: int, marker: str,
                  max_bytes: int, backups: int) -> Optional[RotatingFileHandler]:
    d = diag_dir()
    if d is None:
        return None
    try:
        h = RotatingFileHandler(str(d / filename), maxBytes=max_bytes,
                                backupCount=backups, encoding="utf-8")
    except OSError:
        return None
    h.setLevel(level)
    h.setFormatter(_FMT)
    h.addFilter(RedactionFilter())   # defense-in-depth: scrub secrets per record
    h._diag_marker = marker          # type: ignore[attr-defined]  (idempotency tag)
    return h


def _attach(log: logging.Logger, handler: Optional[RotatingFileHandler]) -> None:
    if handler is None:
        return
    marker = getattr(handler, "_diag_marker", None)
    for existing in log.handlers:
        if getattr(existing, "_diag_marker", None) == marker:
            return  # already attached (re-import / worker fork)
    log.addHandler(handler)


def install() -> None:
    """Attach the dedicated rotating file handlers. Idempotent and fail-open;
    safe to call from both the FastAPI process and the Celery worker."""
    global _installed
    if _installed:
        return
    _installed = True

    d = diag_dir()
    if d is None:
        logger.warning("diagnostics dir not writable anywhere — diag file logs disabled")
        return

    verbose = bool(getattr(settings, "diag_verbose", False))

    # codegen.log ← the agentic pipeline + per-agent module loggers. These
    # loggers ALREADY emit the LLM-turn / tool-call / phase lines we want; we
    # just give them a dedicated, findable file. Propagation is left intact so
    # the same lines still reach the main app log / console. ONE handler
    # instance shared across both loggers (not one per logger) so the two don't
    # race each other on rotation of the same file.
    codegen_h = _make_handler("codegen.log", logging.INFO, "diag-codegen",
                              _CODEGEN_MAX_BYTES, _CODEGEN_BACKUPS)
    codegen_dbg_h = (_make_handler("codegen.debug.log", logging.DEBUG,
                                   "diag-codegen-debug",
                                   _CODEGEN_MAX_BYTES, _CODEGEN_BACKUPS)
                     if verbose else None)
    for name in ("app.agentic", "app.agents"):
        lg = logging.getLogger(name)
        if lg.level == logging.NOTSET or lg.level > logging.DEBUG:
            lg.setLevel(logging.DEBUG if verbose else logging.INFO)
        _attach(lg, codegen_h)
        _attach(lg, codegen_dbg_h)

    # commands.log ← dedicated, non-propagating command stream.
    _cmd_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    _attach(_cmd_logger, _make_handler("commands.log", logging.INFO, "diag-commands",
                                       _COMMAND_MAX_BYTES, _COMMAND_BACKUPS))

    logger.info("diagnostics logging → %s (verbose=%s)", d, verbose)


# ── command logging (called from the platform adapter chokepoint) ────────────

def log_command(argv, cwd, *, exit_code: int, duration_ms: int,
                timed_out: bool, stdout: str = "", stderr: str = "") -> None:
    """Record one system command: what ran, in which folder, the result, how
    long it took. Output tails go to DEBUG (commands.debug visible only when
    verbose). Fail-open — never raises into the caller."""
    try:
        argv = list(argv or [])
        # Skip pure version/availability probes (`git --version`, `mvn -v`,
        # `java -version`, `javac -version`) — they're toolchain checks, not the
        # build/work commands this log is for, and otherwise dominate it.
        if len(argv) == 2 and str(argv[1]) in ("--version", "-v", "-version", "version"):
            return
        argv_str = " ".join(str(a) for a in argv)
        status = "TIMEOUT" if timed_out else f"exit={exit_code}"
        _cmd_logger.info("cwd=%s  %s  %dms  ::  %s", cwd, status, duration_ms, argv_str[:600])
        if getattr(settings, "diag_verbose", False):
            out = (stdout or "")[-2000:]
            err = (stderr or "")[-2000:]
            if out.strip() or err.strip():
                tail = out + (("\n--- stderr ---\n" + err) if err.strip() else "")
                _cmd_logger.debug("output tail (%s):\n%s", argv_str[:120], tail)
    except Exception:
        pass


# ── per-build log file ───────────────────────────────────────────────────────

def _redact(text: str) -> str:
    """Reuse the agentic-events redactor when importable; else identity. Lazy so
    this module stays free of the model-import graph."""
    try:
        from app.agents.agentic_events import redact
        return redact(text)
    except Exception:
        return text


class _BuildLog:
    """Thin fail-open line writer for one build's dedicated log file.

    SCR finding #10 (Improper Resource Shutdown or Release) — `open_build_log`
    / `open_verify_log` used to return a raw `open()` file handle wrapped in an
    object whose `.close()` had to be called manually by every caller; nothing
    enforced that. Every caller found in this codebase already wraps its
    lifetime in `try/finally: blog.close()`, so there was no live leak, but the
    API shape itself invited one from a future caller who forgot. Supporting
    the context-manager protocol lets callers write
    `with diag.open_build_log(...) as blog:` instead, which the interpreter
    enforces even if the caller's own code raises before an explicit
    `.close()`. `.close()` remains available and safe to call directly for any
    existing caller that predates this change.
    """

    def __init__(self, fh, path: Path) -> None:
        self._fh = fh
        self.path = path

    def write(self, line: str) -> None:
        try:
            self._fh.write(_redact(line.rstrip("\n")) + "\n")
            self._fh.flush()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self) -> "_BuildLog":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class _NullBuildLog:
    """No-op writer used when no diagnostics dir is writable."""
    path = None

    def write(self, line: str) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "_NullBuildLog":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


def _prune_build_dir(bdir: Path, keep: int) -> None:
    try:
        files = sorted(bdir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def open_build_log(change_id: str, ts: str):
    """Open a dedicated per-build log file `build/<change>-<ts>.log`.

    Returns a writer with `.write(line)` / `.close()` and a `.path` attribute.
    Fail-open: returns a no-op writer if the dir can't be created. Old build
    logs beyond `_BUILD_KEEP` are pruned so the folder can't grow unbounded.
    """
    d = diag_dir()
    if d is None:
        return _NullBuildLog()
    try:
        bdir = d / "build"
        bdir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(change_id))[:48] or "build"
        path = bdir / f"{safe}-{ts}.log"
        fh = open(path, "a", encoding="utf-8")
        _prune_build_dir(bdir, _BUILD_KEEP)
        return _BuildLog(fh, path)
    except OSError:
        return _NullBuildLog()


def open_verify_log(run_id: str):
    """Open the agentic VERIFY build log for a run: `verify/<run_id>.log` (append).

    One file per agentic run, appended across every verify step (mvn install /
    generate-sources per module), so the FULL build output of the verification
    gate is findable — not just the 4 KB head+tail kept on the DB row, nor only
    the per-module Built/Failed summary the UI shows. Same fail-open writer +
    secret redaction as the build log.
    """
    d = diag_dir()
    if d is None:
        return _NullBuildLog()
    try:
        vdir = d / "verify"
        vdir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(run_id))[:48] or "run"
        path = vdir / f"{safe}.log"
        fh = open(path, "a", encoding="utf-8")
        _prune_build_dir(vdir, _BUILD_KEEP)
        return _BuildLog(fh, path)
    except OSError:
        return _NullBuildLog()
