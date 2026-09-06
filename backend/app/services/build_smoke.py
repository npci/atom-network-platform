# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase B build+deploy smoke test — run `build_and_deploy.sh` with no change request.

The real Build step lives inside a change at Phase B BUILD with the governance
gate passed, so there is no way to answer "is the build host wired up correctly?"
without walking a change all the way there. This module answers it directly.

It deliberately reuses `build_runner`'s own internals — `_build_command` composes
the identical invocation, `_LogParser` slices the log the identical way, and the
success rule below matches `build_runner.py:780`. A smoke test that used its own
parallel implementation could pass while the real thing fails.

Two levels:

  preflight — a probe script: who am I, is build_and_deploy.sh there, are
              git/mvn/java on PATH. Seconds. Safe to run any time.
  full      — the real clone + build + deploy. Many minutes.

Consumed by `scripts/smoke_phase_b_build.py` (CLI) and `api/admin_build_smoke.py`
(Admin → Build Host page).
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import time
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.error_taxonomy import client_safe_detail
from app.models.base import generate_uuid, utcnow
from app.services.build_runner import _LogParser, _build_command
from app.services.host_runner import stream_remote_command
from app.services.local_runner import stream_local_command

logger = logging.getLogger(__name__)

# A long Maven build is easily tens of thousands of lines. Keep a bounded tail
# in memory for the UI; the CLI and the on-disk build log keep everything.
_MAX_LINES = 20000


def resolve_config() -> dict:
    """Current runner wiring plus whether it can actually work, without running it."""
    mode = (settings.phase_b_runner_mode or "ssh").strip().lower()
    script = settings.phase_b_build_script
    cfg = {
        "mode": mode,
        "build_script": script,
        "host": settings.phase_b_host if mode == "ssh" else "localhost",
        "host_user": settings.phase_b_host_user if mode == "ssh" else None,
        "host_key": settings.phase_b_host_key if mode == "ssh" else None,
        "connect_timeout": settings.phase_b_host_connect_timeout if mode == "ssh" else None,
        "known_hosts": settings.build_host_known_hosts or None,
    }
    cfg["ready"], cfg["blocker"] = _readiness(mode, script)
    return cfg


def _readiness(mode: str, script: str) -> tuple[bool, Optional[str]]:
    if mode in ("mock", "build"):
        return False, (
            f"PHASE_B_RUNNER_MODE={mode} never invokes {script} — 'mock' returns a canned "
            f"log and 'build' clones+builds locally with a faked deploy. Set the mode to "
            f"'local' or 'ssh' to exercise the real script."
        )
    if mode not in ("ssh", "local"):
        return False, f"Unknown PHASE_B_RUNNER_MODE={mode!r} (expected ssh, local, build or mock)."
    if mode == "ssh":
        key = Path(settings.phase_b_host_key)
        if not key.exists():
            return False, f"No SSH key at {key} — nothing is mounted there."
        # Compose binds a /dev/null tombstone when PHASE_B_HOST_KEY_FILE is
        # unset; that lands as a 0-byte character device, not the literal
        # string "/dev/null", so the inode is what has to be checked.
        if not key.is_file() or key.stat().st_size == 0:
            return False, (
                f"{key} is not a usable key "
                f"({'empty file' if key.is_file() else 'not a regular file'}) — "
                f"PHASE_B_HOST_KEY_FILE is unset, so the /dev/null tombstone got mounted."
            )
    if mode == "local" and not Path(script).is_file():
        return False, (
            f"{script} does not exist on this backend's filesystem. PHASE_B_RUNNER_MODE=local "
            f"runs the script as a subprocess of the backend process, so in Docker it must be "
            f"bind-mounted in — a copy on the Docker host is not reachable. Either mount it, "
            f"or use PHASE_B_RUNNER_MODE=ssh."
        )
    return True, None


def preflight_command(script: str) -> str:
    """Probe the target the way the build would find it — same shell, same user."""
    q = shlex.quote(script)
    return "; ".join([
        'echo "[smoke] whoami=$(whoami) host=$(hostname) pwd=$(pwd)"',
        'echo "[smoke] HOME=$HOME"',
        'for t in git mvn java; do '
        'if command -v $t >/dev/null 2>&1; then echo "[smoke] tool $t -> $(command -v $t)"; '
        'else echo "[smoke] MISSING tool $t"; fi; done',
        f'if [ -f {q} ]; then echo "[smoke] script FOUND {q}"; '
        f'else echo "[smoke] script MISSING {q}"; exit 2; fi',
        f'[ -r {q} ] && echo "[smoke] script readable" || echo "[smoke] script NOT readable"',
        f'echo "[smoke] shebang: $(head -1 {q})"',
        f'echo "[smoke] script dir:"; ls -l "$(dirname {q})" | head -20',
    ])


def build_command(core_branch: str, app_branch: str) -> str:
    """The exact invocation the real Build step uses."""
    return _build_command(core_branch, app_branch)


def open_stream(mode: str, command: str):
    if mode == "local":
        return stream_local_command(command)
    return stream_remote_command(
        host=settings.phase_b_host,
        user=settings.phase_b_host_user,
        private_key_path=settings.phase_b_host_key,
        command=command,
        connect_timeout=settings.phase_b_host_connect_timeout,
    )


class SmokeRun:
    """One smoke execution — live state, pollable while it streams."""

    def __init__(self, kind: str, mode: str, command: str) -> None:
        self.id = generate_uuid()
        self.kind = kind                  # "preflight" | "full"
        self.mode = mode
        self.command = command
        self.status = "running"           # running|success|failure|timeout|error
        self.started_at = utcnow()
        self.finished_at = None
        self.exit_code: Optional[int] = None
        self.lines: list[str] = []
        self.total_lines = 0
        self.truncated = False
        self.section = "build"
        self.section_seconds = {"build": 0.0, "deploy": 0.0, "startup": 0.0}
        self.artifacts: list[dict] = []
        self.services: list[dict] = []
        self.first_artifact: Optional[str] = None
        self.saw_build_success = False
        self.saw_build_failure = False
        self.log_path: Optional[str] = None
        self.note: Optional[str] = None
        self._monotonic_start = time.monotonic()

    @property
    def elapsed(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return time.monotonic() - self._monotonic_start

    def append(self, line: str) -> None:
        self.total_lines += 1
        self.lines.append(line)
        if len(self.lines) > _MAX_LINES:
            del self.lines[:len(self.lines) - _MAX_LINES]
            self.truncated = True

    def to_dict(self, since: int = 0) -> dict:
        """Serialise for the UI. *since* is an index into the retained tail."""
        start = max(0, since)
        return {
            "id": self.id,
            "kind": self.kind,
            "mode": self.mode,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_seconds": round(self.elapsed, 1),
            "section": self.section,
            "section_seconds": {k: round(v, 1) for k, v in self.section_seconds.items()},
            "lines": self.lines[start:],
            "next_index": len(self.lines),
            "total_lines": self.total_lines,
            "truncated": self.truncated,
            "artifacts": self.artifacts,
            "services": self.services,
            "first_artifact": self.first_artifact,
            "saw_build_success": self.saw_build_success,
            "saw_build_failure": self.saw_build_failure,
            "log_path": self.log_path,
            "note": self.note,
        }


# In-memory registry. Single-process uvicorn; a restart loses history, which is
# fine — the on-disk build log is the durable copy.
_RUNS: dict[str, SmokeRun] = {}
_RUN_ORDER: list[str] = []
_KEEP_RUNS = 20


def get_run(run_id: str) -> Optional[SmokeRun]:
    return _RUNS.get(run_id)


def recent_runs(limit: int = 10) -> list[SmokeRun]:
    return [_RUNS[r] for r in reversed(_RUN_ORDER[-limit:]) if r in _RUNS]


def _register(run: SmokeRun) -> None:
    _RUNS[run.id] = run
    _RUN_ORDER.append(run.id)
    while len(_RUN_ORDER) > _KEEP_RUNS:
        _RUNS.pop(_RUN_ORDER.pop(0), None)


async def _pump(stream, queue: asyncio.Queue) -> None:
    """Drain *stream* into *queue*, then post a None sentinel.

    Why a queue rather than iterating directly: callers need to time out on
    *silence* (to emit a heartbeat) without cancelling the generator, which
    wrapping `__anext__()` in `wait_for` would do on every quiet period.
    """
    try:
        async for event in stream:
            await queue.put(event)
    except Exception as e:  # noqa: BLE001 — surfaced as a log line, not a traceback
        # SCR #6: this string lands in `run.lines`, which `to_dict()` returns
        # from GET /api/admin/build-smoke/run/{run_id}. The exception comes
        # from asyncssh/subprocess streaming, so it can name the SSH host, the
        # service account and the private-key path.
        logger.warning("build smoke stream error: %s", e)
        await queue.put(("stderr", f"[smoke] stream error: {client_safe_detail(e)}"))
        await queue.put(("exit", -1))
    finally:
        await queue.put(None)


def hhmmss(seconds: float) -> str:
    s = int(max(0.0, seconds))
    if s >= 3600:
        return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


async def execute(
    run: SmokeRun,
    *,
    parse: bool,
    heartbeat: float = 30.0,
    timeout: float = 7200.0,
    blog=None,
    echo=None,
) -> SmokeRun:
    """Stream *run*'s command to completion, filling the run in place.

    *echo* is an optional callable for the CLI's live stdout; the UI polls
    ``run.lines`` instead. *blog* is an optional diag build-log writer.
    """
    parser = _LogParser()
    exit_code = -1
    timed_out = False
    started = time.monotonic()
    last_line_at = started
    section_started = started

    queue: asyncio.Queue = asyncio.Queue()
    stream = open_stream(run.mode, run.command)
    pump = asyncio.create_task(_pump(stream, queue))

    def emit(text: str) -> None:
        run.append(text)
        if blog is not None:
            blog.write(text)
        if echo is not None:
            echo(text)

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                now = time.monotonic()
                emit(f"[{hhmmss(now - started)}] ... alive ({parser.section if parse else 'running'}), "
                     f"no output for {hhmmss(now - last_line_at)}, {run.total_lines} lines so far")
                if timeout and (now - started) >= timeout:
                    timed_out = True
                    break
                continue

            if item is None:
                break

            kind, payload = item
            now = time.monotonic()

            if kind == "exit":
                exit_code = int(payload) if isinstance(payload, int) else -1
                continue

            last_line_at = now
            line = str(payload)

            if parse:
                before = parser.section
                parser.feed(line)
                if parser.section != before:
                    run.section_seconds[before] += now - section_started
                    section_started = now
                    run.section = parser.section
                    emit(f"[{hhmmss(now - started)}] ══ section: {before} → {parser.section} ══")

            emit(f"[{hhmmss(now - started)}] {'!' if kind == 'stderr' else '|'} {line}")

            # Keep structured results live so the UI can show them mid-run.
            if parse:
                run.artifacts = list(parser.artifacts)
                run.services = list(parser.services)
                run.first_artifact = parser.first_artifact_path
                run.saw_build_success = parser.saw_build_success
                run.saw_build_failure = parser.saw_build_failure

            if timeout and (now - started) >= timeout:
                timed_out = True
                break
    finally:
        # Order matters on the timeout path: cancel, WAIT for the cancellation
        # to land, then close the generator — otherwise the loop can be left
        # holding a live subprocess transport.
        if not pump.done():
            pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        try:
            await stream.aclose()
        except Exception:  # noqa: BLE001 — teardown only
            pass

    if parse:
        run.section_seconds[parser.section] += time.monotonic() - section_started

    run.exit_code = exit_code
    run.finished_at = utcnow()

    if timed_out:
        run.status = "timeout"
        run.note = (
            "Stopped watching after the timeout. The script is almost certainly still "
            "running on the target — the real Build step behaves the same way, a client "
            "disconnect never cancels it."
        )
    elif run.kind == "preflight":
        run.status = "success" if exit_code == 0 else "failure"
    else:
        # Identical rule to the real runner (build_runner.py:780).
        run.status = "success" if (exit_code == 0 and not parser.saw_build_failure) else "failure"
        if run.status == "success" and not parser.deploy and not parser.startup:
            run.note = (
                "Exit 0, but no deploy/startup sections were carved out — the step passes, "
                "but the Build panel will show one undifferentiated log. The script's stage "
                "banners don't match the cues in build_runner.py:60-88."
            )

    logger.info(
        "Build smoke %s finished: kind=%s mode=%s status=%s exit=%s elapsed=%.1fs",
        run.id, run.kind, run.mode, run.status, exit_code, run.elapsed,
    )
    return run


async def start_background_run(core_branch: str, app_branch: str,
                               *, heartbeat: float = 30.0,
                               timeout: float = 7200.0) -> SmokeRun:
    """Kick off a full build+deploy smoke run; return immediately with the run."""
    from app.core import diag

    cfg = resolve_config()
    run = SmokeRun("full", cfg["mode"], build_command(core_branch, app_branch))
    blog = diag.open_build_log("smoke", utcnow().strftime("%Y%m%d-%H%M%S"))
    run.log_path = str(blog.path) if blog.path else None
    blog.write(f"=== build smoke (admin) — mode={run.mode} command={run.command} "
               f"started={run.started_at.isoformat()} ===")
    _register(run)

    async def _drive():
        # SCR finding #10 — `with blog:` (rather than a manual `.close()` in
        # `finally`) guarantees the log file handle is released on every exit
        # path, including one that raises out of the `finally` block's own
        # `blog.write(...)` call, which the previous manual-close version did
        # not cover.
        with blog:
            try:
                await execute(run, parse=True, heartbeat=heartbeat, timeout=timeout, blog=blog)
            except Exception as e:  # noqa: BLE001 — a crashed run must still report
                logger.exception("Build smoke run %s crashed", run.id)
                run.status = "error"
                # SCR #6: `note` is serialised by to_dict() and returned by
                # GET /api/admin/build-smoke/run/{run_id}. Full detail is on
                # the logger.exception line immediately above.
                run.note = f"Smoke run crashed: {client_safe_detail(e)}"
                run.finished_at = utcnow()
            finally:
                blog.write(f"--- status={run.status} exit_code={run.exit_code} "
                           f"elapsed={run.elapsed:.1f}s ---")

    asyncio.create_task(_drive())
    return run


async def run_preflight(*, timeout: float = 120.0) -> SmokeRun:
    """Run the fast probe to completion and return the finished run."""
    cfg = resolve_config()
    run = SmokeRun("preflight", cfg["mode"], preflight_command(settings.phase_b_build_script))
    _register(run)
    await execute(run, parse=False, heartbeat=15.0, timeout=timeout)
    return run
