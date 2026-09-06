# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ProcessExecutor — the sanctioned interface for OS process execution.

Closes S5 (`ARCHITECTURE_REVIEW_ACTIONS.md` — "Wrap direct OS process
execution... in an approved interface with authorization, command
allowlisting, no shell-injection path, telemetry, audit and bounded
execution time") and implements ADR-0003
(docs/adr/ADR-0003-controlled-process-execution.md) and
security_architecture_skills.md §12.2 ("Direct OS process execution from
application business flows is prohibited... MUST be routed through a
tightly controlled, approved, auditable service interface").

Call-site migration status (per ADR-0003's own "Negative" consequence —
"a real refactor across every existing subprocess call site... should be
done incrementally, file by file, verified against the existing Maven
verification test suite after each migration"):

- **Migrated in this pass** (fixed argv, no shell semantics needed —
  clean fit for this interface): `backend/app/agents/jdk_discovery.py`
  (`update-java-alternatives`/`update-alternatives` enumeration),
  `backend/app/agents/delta_grounding.py` (`git grep` evidence lookup).
  Both verified against their existing test suites after migration
  (`test_jdk_discovery.py`, `test_delta_grounding.py` — all tests pass,
  including a real-git-repo integration test for the latter).
- **Deliberately NOT migrated** (a real behavior-preserving migration
  would require this interface to grow shell-command support, which is
  a larger design decision than a call-site swap):
  `backend/app/services/local_runner.py` (needs `bash -c` shell
  semantics for multi-command build scripts — forcing it through this
  interface's no-shell-interpolation design would either break its
  functionality or require weakening the interface's core security
  guarantee for every OTHER caller);
  `backend/app/agents/governance_sandbox.py` (has its own more
  specialized containment — custom rlimits, named-container timeout
  handling, Docker orchestration with bind-mount isolation — that this
  interface does not yet replicate; migrating it here would be a
  downgrade, not a hardening, until this interface grows equivalent
  sandbox-specific capabilities);
  `backend/app/agents/lsp_client.py` (long-lived, bidirectional-pipe
  `Popen` process for JSON-RPC over stdin/stdout — a fundamentally
  different lifecycle than this interface's request/response
  `run()` model);
  `backend/app/agents/platform_adapter.py` (Windows-specific
  `creationflags` handling this interface does not yet expose).
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Extend deliberately, never wildcard — adding a command here is itself a
# security-relevant change and should go through code review, per ADR-0003's
# migration plan step 3. update-java-alternatives/update-alternatives added
# for jdk_discovery.py's migration (S5 call-site migration, read-only
# `--list`/`-l` JDK enumeration, never mutates the system).
_DEFAULT_ALLOWLIST = frozenset({
    "mvn", "mvn.cmd", "git", "java", "javac",
    "update-java-alternatives", "update-alternatives",
})


def _allowlist_from_settings() -> frozenset[str]:
    """Reads `settings.process_executor_allowlist` (comma-separated
    command names). Falls back to `_DEFAULT_ALLOWLIST` if settings isn't
    importable (unit tests that stub this module out) or the setting is
    empty/unset — this keeps the hardcoded default as the ultimate
    fail-safe, per security_architecture_skills.md §4.3 ('fail fast
    instead of starting insecurely' — an EMPTY effective allowlist would
    be a silent 'nothing is allowed to run' footgun, not a security
    improvement, so an empty config value is treated as 'use the
    default,' not 'allow nothing')."""
    try:
        from app.core.config import settings
        raw = getattr(settings, "process_executor_allowlist", "") or ""
        names = frozenset(n.strip() for n in raw.split(",") if n.strip())
        return names or _DEFAULT_ALLOWLIST
    except Exception:  # noqa: BLE001 — settings unavailable in isolated unit tests
        return _DEFAULT_ALLOWLIST


def _default_timeout_s() -> float:
    """Reads `settings.process_executor_default_timeout_s`, used by
    `ProcessExecutionRequest.default_timeout()` (see below) for call
    sites that want the operator-configured default rather than naming
    their own timeout inline. Falls back to 600.0 (10 minutes) if
    settings isn't importable."""
    try:
        from app.core.config import settings
        return float(getattr(settings, "process_executor_default_timeout_s", 600.0) or 600.0)
    except Exception:  # noqa: BLE001
        return 600.0


class ProcessNotAllowedError(Exception):
    """Raised when the requested command is not in the allowlist."""


class ProcessTimeoutError(Exception):
    """Raised when the process did not complete within `timeout_s`."""


class MissingTimeoutError(Exception):
    """Raised when a caller omits `timeout_s` — fail-fast on a missing
    safety parameter, consistent with core/config.py's validators."""


@dataclass(frozen=True)
class ProcessExecutionRequest:
    command: str                    # allowlisted binary name, e.g. "mvn"
    args: list[str]                 # NEVER shell-interpolated
    cwd: Path
    timeout_s: float
    env_overrides: dict[str, str] = field(default_factory=dict)
    run_id: str | None = None       # for audit correlation
    actor: str = "system"           # who/what triggered this (run_id, admin user, etc.)

    @classmethod
    def with_default_timeout(cls, *, command: str, args: list[str], cwd: Path,
                             env_overrides: dict[str, str] | None = None,
                             run_id: str | None = None, actor: str = "system"):
        """Convenience constructor for call sites that want the
        operator-configured `settings.process_executor_default_timeout_s`
        rather than naming their own timeout inline — reads the setting
        fresh on each call (not cached), so an operator's config change
        takes effect on the next invocation without a process restart."""
        return cls(
            command=command, args=args, cwd=cwd, timeout_s=_default_timeout_s(),
            env_overrides=env_overrides or {}, run_id=run_id, actor=actor,
        )


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False


class ProcessExecutor:
    """The ONLY sanctioned way to spawn an OS process from application
    business flows. Enforces: (1) command allowlist, (2) no shell=True
    ever, (3) bounded execution time, (4) audit log entry per
    invocation, (5) telemetry (duration, exit code, truncated
    stdout/stderr) emitted via the existing observability chokepoint.

    Interaction with governance_sandbox.py: where the Docker-backed
    sandbox is available, THAT remains the preferred, stricter control
    (per ADR-0003's "Interaction with existing governance sandbox"
    section) — ProcessExecutor is the fallback control for paths that do
    NOT yet go through the sandbox (host/local runners), and for the
    sandbox's own subprocess fallback path itself
    (`governance_sandbox.py`'s "subprocess backend").
    """

    def __init__(self, allowlist: frozenset[str] | None = None,
                 audit_sink=None):
        # Explicit `allowlist=` always wins (test harnesses, a caller that
        # needs a NARROWER allowlist than the operator-wide default). With
        # no explicit override, read `settings.process_executor_allowlist`
        # (comma-separated) so an operator can extend the allowlist via
        # config without a code change — falling back to the hardcoded
        # `_DEFAULT_ALLOWLIST` only if settings is unavailable (e.g. a
        # test importing this module without the full app config loaded).
        if allowlist is not None:
            self._allowlist = allowlist
        else:
            self._allowlist = _allowlist_from_settings()
        # audit_sink: callable(dict) -> None, defaults to structured
        # logging; a caller can pass a DB-writing sink to persist to
        # `process_execution_audit` (see ADR-0003 migration step 4) once
        # that table exists.
        self._audit_sink = audit_sink or self._log_audit_event

    @staticmethod
    def _log_audit_event(event: dict) -> None:
        logger.info(
            "PROCESS_EXECUTION_AUDIT command=%s args_digest=%s cwd=%s "
            "run_id=%s actor=%s exit_code=%s duration_ms=%.1f timed_out=%s",
            event["command"], event["args_digest"], event["cwd"],
            event["run_id"], event["actor"], event.get("exit_code"),
            event.get("duration_ms", 0.0), event.get("timed_out", False),
        )

    @staticmethod
    def _args_digest(args: list[str]) -> str:
        """A short, non-reversible-enough-for-casual-leakage digest of the
        args, NOT the full args themselves — per ADR-0003 migration step 4
        ("args digest — NOT full args if they could carry secrets").
        Uses shlex.join for a stable, shell-safe-looking representation,
        then truncates hard; this is for audit correlation, not secrecy
        guarantees, so a simple truncated join is sufficient (no args in
        this platform's known call sites carry raw secret VALUES — secrets
        are passed via env_overrides, which is never logged here)."""
        joined = shlex.join(args)
        return joined[:200] + ("...(truncated)" if len(joined) > 200 else "")

    async def run(self, req: ProcessExecutionRequest) -> ProcessResult:
        if req.command not in self._allowlist:
            logger.error(
                "SECURITY_EVENT event=process_not_allowed command=%s run_id=%s actor=%s",
                req.command, req.run_id, req.actor,
            )
            raise ProcessNotAllowedError(
                f"{req.command!r} is not in the process allowlist "
                f"({sorted(self._allowlist)}); extend ProcessExecutor's "
                f"allowlist deliberately if this is a legitimate new tool, "
                f"per ADR-0003's code-review requirement for allowlist changes."
            )
        if not req.timeout_s or req.timeout_s <= 0:
            raise MissingTimeoutError(
                f"ProcessExecutionRequest for {req.command!r} has no "
                f"positive timeout_s — every process invocation MUST "
                f"supply a bounded execution time (security_architecture_"
                f"skills.md §12.2)."
            )

        run_id = req.run_id or str(uuid.uuid4())
        args_digest = self._args_digest(req.args)
        start = time.monotonic()
        timed_out = False
        stdout_text = ""
        stderr_text = ""
        exit_code = -1

        try:
            proc = await asyncio.create_subprocess_exec(
                req.command, *req.args,
                cwd=str(req.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # NEVER shell=True — args passed as a list, never a
                # joined/interpolated string, structurally preventing
                # shell-injection regardless of what `args` contains.
                env=_merged_env(req.env_overrides),
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=req.timeout_s,
                )
                exit_code = proc.returncode if proc.returncode is not None else -1
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                await proc.wait()
                stdout_b, stderr_b = b"", b""
                exit_code = -1
            stdout_text = stdout_b.decode("utf-8", errors="replace")[:50_000]
            stderr_text = stderr_b.decode("utf-8", errors="replace")[:50_000]
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            self._audit_sink({
                "command": req.command,
                "args_digest": args_digest,
                "cwd": str(req.cwd),
                "run_id": run_id,
                "actor": req.actor,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "timed_out": timed_out,
            })

        if timed_out:
            raise ProcessTimeoutError(
                f"{req.command} timed out after {req.timeout_s}s (run_id={run_id})"
            )
        return ProcessResult(
            exit_code=exit_code, stdout=stdout_text, stderr=stderr_text,
            duration_ms=duration_ms, timed_out=timed_out,
        )


def _merged_env(overrides: dict[str, str]) -> dict[str, str]:
    import os
    env = dict(os.environ)
    env.update(overrides)
    return env


def run_sync(req: ProcessExecutionRequest) -> ProcessResult:
    """Synchronous convenience wrapper around `ProcessExecutor.run()`, for
    call sites that are not `async def` (e.g. `jdk_discovery.py`,
    `delta_grounding.py` — read-only, short-lived tool invocations called
    from otherwise-synchronous code paths). Internally still goes through
    the SAME allowlist/timeout/audit-logging logic as the async path; this
    is a convenience shim, not a second implementation.

    Uses `asyncio.run()`, which requires no event loop already running on
    the calling thread — correct for these call sites (plain functions
    called from synchronous contexts), but NOT safe to call from inside an
    already-running event loop (use `await default_executor.run(...)`
    directly there instead)."""
    return asyncio.run(default_executor.run(req))


# Module-level default instance — call sites migrating from a direct
# `subprocess`/`asyncio.create_subprocess_exec` call should import and use
# this, e.g.:
#
#   from app.core.process_executor import default_executor, ProcessExecutionRequest
#   result = await default_executor.run(ProcessExecutionRequest(
#       command="mvn", args=["-B", "verify"], cwd=workspace_dir,
#       timeout_s=600, run_id=run.id, actor=f"agentic_run:{run.id}",
#   ))
default_executor = ProcessExecutor()
