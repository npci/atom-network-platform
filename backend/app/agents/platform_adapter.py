# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Platform-aware command execution (THE BOOK §18.2).

The single execution primitive the agentic runtime drives — one cross-OS
``run_command`` that runs **allowlisted argv** (never a shell string), with
per-OS tool resolution and operational containment. This corrects the legacy
``/bin/bash -c`` path (``local_runner.py``, left untouched for the legacy build).

**Containment, not a sandbox (§6/§17):** clean env, argv[0] allowlist, timeout,
and — on POSIX only — ``RLIMIT_AS``/``RLIMIT_CPU``. The network is *not* isolated
and a hostile repo payload is out of scope (deferred). Windows containment is
weaker (no ``setrlimit``); documented honestly.

The git-guard hook (§22) wraps this whenever ``argv[0]`` is git; that lands in
S12. Here we provide the mechanism + the allowlist choke point.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings

# argv[0] allowlist — the containment boundary (a security gate, not a tuning
# knob, so it lives in code). Matched on the basename minus a .exe/.cmd/.bat
# suffix, so Windows variants (mvn.cmd, git.exe) and ./mvnw all resolve here.
ALLOWED_ARGV0: frozenset[str] = frozenset({
    "git", "mvn", "mvnw", "javac", "java",   # build + vcs
    # READ-ONLY inspection — zero mutation/exec/network risk, repo-scoped via cwd.
    # The agent legitimately reaches for these to diagnose (e.g. grep GENERATED /
    # gitignored sources the git-grep tool can't see). The dedicated read_file/grep/
    # glob tools remain the primary path; these just remove a needless wall.
    "tail", "head", "cat", "ls", "grep", "wc", "sort",
})
# Deliberately STILL excluded: anything that mutates, executes, or escapes — find
# (-exec/-delete), sed/awk (-i / program exec), xargs, rm/mv/cp, chmod, curl/wget,
# bash/sh. Mutation goes through the edit-ladder tools (under the git-guard).

# Env vars passed through to children — everything else (incl. secrets like
# GITLAB_TOKEN, *_API_KEY) is dropped. mvn/git need PATH + HOME (~/.m2, ~/.git*)
# + JAVA_HOME; the rest are locale/build niceties.
_ENV_PASSTHROUGH: frozenset[str] = frozenset({
    "PATH", "HOME", "JAVA_HOME", "M2_HOME", "MAVEN_OPTS",
    "LANG", "LC_ALL", "TMPDIR", "SystemRoot", "USERPROFILE",
})


def _log_command_diag(argv, cwd, result) -> None:
    """Tee one command (argv, cwd, exit, duration) to the diagnostics command
    log. Best-effort — a logging hiccup must never break command execution, so
    every failure (incl. a missing diag module) is swallowed."""
    try:
        from app.core import diag
        diag.log_command(
            argv, str(cwd),
            exit_code=result.exit_code, duration_ms=result.duration_ms,
            timed_out=result.timed_out, stdout=result.stdout, stderr=result.stderr,
        )
    except Exception:
        pass


class CommandNotAllowed(ValueError):
    """argv[0] is not on the allowlist — refused before execution."""


@dataclass
class CommandResult:
    """Result of one command. ``stdout``/``stderr`` are **uncapped** — callers
    must tail + redact before persisting (§21); the coding-log sink
    (``agentic_events.emit_event``) does the redaction, and the tail cap is
    applied where command output is logged (S6+)."""
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _argv0_key(argv0: str) -> str:
    """Normalise argv[0] to its allowlist key: basename without exe/cmd suffix."""
    name = Path(argv0).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


class PlatformAdapter:
    """Per-OS executor. One instance per worker; stateless beyond the platform."""

    def __init__(self) -> None:
        self.system = platform.system()  # "Darwin" | "Linux" | "Windows"

    @property
    def is_windows(self) -> bool:
        return self.system == "Windows"

    def resolve(self, argv0: str, cwd: Path) -> str | None:
        """Resolve argv[0] to an executable path. Repo-local wrappers
        (``mvnw``/``./mvnw``) resolve against ``cwd``; everything else via PATH."""
        key = _argv0_key(argv0)
        if key == "mvnw":
            for cand in ("mvnw.cmd" if self.is_windows else "mvnw", "mvnw"):
                p = cwd / cand
                if p.exists():
                    return str(p)
            return None
        return shutil.which(argv0)

    def _clean_env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k in _ENV_PASSTHROUGH}

    _BUILD_KEYS = frozenset({"mvn", "mvnw", "java", "javac"})

    def _build_tmpdir(self) -> str:
        """A guaranteed-existing, writable temp dir for the build JVM. jansi (Maven's
        ANSI-colour native lib) extracts ``libjansi.so`` + a ``.lck`` lock into
        ``java.io.tmpdir``; when that dir is missing the native load fails with
        ``…/libjansi.so.lck (No such file or directory)``, which on some boxes aborts
        the build. Pin the temp dir to one we own and CREATE, so the load can't fail
        on a missing/unwritable dir."""
        d = Path(os.path.expanduser("~")) / ".a2a" / "buildtmp"
        try:
            d.mkdir(parents=True, exist_ok=True)
            return str(d)
        except OSError:
            # Home not writable → a PRIVATE mkdtemp (unique name, mode 0700) rather than a
            # predictable /tmp/a2a-buildtmp that a co-tenant on a shared host could
            # pre-create as a symlink. mkdtemp can't land on an attacker-planted path.
            try:
                return tempfile.mkdtemp(prefix="a2a-buildtmp-")
            except OSError:
                return tempfile.gettempdir()

    def _augment_build_env(self, env: dict[str, str], key: str) -> dict[str, str]:
        """For build commands only: pin a writable temp dir (jansi fix) so a missing
        ``java.io.tmpdir`` can't fail the build, and disable ANSI colour so jansi
        needn't extract its native lib at all."""
        if key not in self._BUILD_KEYS:
            return env
        env = dict(env)
        tmp = self._build_tmpdir()
        env["TMPDIR"] = tmp
        # -Djava.io.tmpdir + -Djansi.tmpdir → jansi extracts into a dir that exists.
        # -Dstyle.color=never (Maven) + -Djansi.passthrough=true → no native colour
        # lib needed in the first place, so the .lck extraction can't fail the build.
        extra = (f"-Djava.io.tmpdir={tmp} -Djansi.tmpdir={tmp} "
                 f"-Dstyle.color=never -Djansi.passthrough=true")
        # Cap the build-JVM heap so it fits under the container/VM memory ceiling.
        # Default heap is ≈¼ of host RAM; under the full stack that OOM-kills the
        # worker mid-verify (SIGKILL). Honour an operator-set -Xmx in MAVEN_OPTS;
        # otherwise inject the configured cap (and bound metaspace + fail the build
        # cleanly on a JVM OOM instead of letting the kernel kill the process).
        cap_mb = getattr(settings, "agentic_maven_heap_mb", 0) or 0
        cur = env.get("MAVEN_OPTS", "")
        if cap_mb > 0 and "-Xmx" not in cur:
            extra += (f" -Xmx{cap_mb}m -XX:MaxMetaspaceSize=256m "
                      f"-XX:+ExitOnOutOfMemoryError")
        env["MAVEN_OPTS"] = (cur.strip() + " " + extra).strip()
        return env

    def _preexec(self):
        """POSIX rlimits — returns None on Windows (no setrlimit)."""
        if self.is_windows:
            return None
        cap_mb = settings.agentic_rlimit_as_mb
        if not cap_mb:
            return None
        import resource

        def _apply():
            limit = cap_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

        return _apply

    def run_command(
        self,
        repo_dir: str | Path,
        argv: list[str],
        timeout_s: int | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run ``argv`` in ``repo_dir`` with containment. Never invokes a shell.

        Raises :class:`CommandNotAllowed` if ``argv[0]`` is off the allowlist or
        cannot be resolved. A timeout yields ``timed_out=True`` (a §9 gate
        failure) with whatever partial output was captured. ``env_overrides`` (e.g.
        ``{"JAVA_HOME": …}``) switches the build JDK for THIS command — its bin is
        prepended to PATH so a spawned ``java`` matches the override."""
        if not argv:
            raise CommandNotAllowed("empty argv")
        key = _argv0_key(argv[0])
        if key not in ALLOWED_ARGV0:
            raise CommandNotAllowed(f"argv[0] {argv[0]!r} not allowed")
        # argv[0] MUST be a bare command name, resolved via PATH to the trusted
        # system binary. A path-bearing argv[0] (e.g. "/tmp/evil/git", "./git")
        # whose basename matches the allowlist would otherwise run an arbitrary
        # binary — shutil.which returns a path-bearing arg unchanged. The Maven
        # wrapper `mvnw` is the one deliberate exception: it is resolved relative
        # to the repo, and §6 already accepts that the wrapper — like any repo
        # build script — is repo-controlled code (prompt-injection deferred).
        if key != "mvnw" and ("/" in argv[0] or "\\" in argv[0]):
            raise CommandNotAllowed(f"argv[0] must be a bare command name, got {argv[0]!r}")
        # Git-guard hook (§22): every git op — LLM-issued or runtime-issued — passes
        # the per-run remote-write policy. No-op when no agentic run is in scope.
        if key == "git":
            from app.agents import git_guard
            git_guard.enforce(argv)

        # Path containment for file-reading commands: reject absolute paths
        # that resolve outside the working directory (e.g. `cat /etc/shadow`).
        _FILE_READERS = {"cat", "head", "tail", "grep", "ls"}
        if key in _FILE_READERS:
            cwd_resolved = Path(repo_dir).resolve()
            for arg in argv[1:]:
                if arg.startswith("-"):
                    continue
                if arg.startswith("/") or arg.startswith("\\"):
                    resolved = Path(arg).resolve()
                    if cwd_resolved not in resolved.parents and resolved != cwd_resolved:
                        raise CommandNotAllowed(
                            f"path {arg!r} is outside the working directory — "
                            "use repo-relative paths"
                        )

        cwd = Path(repo_dir)
        resolved = self.resolve(argv[0], cwd)
        if not resolved:
            raise CommandNotAllowed(f"could not resolve executable for {argv[0]!r}")

        timeout = timeout_s or settings.agentic_command_timeout_s
        real_argv = [resolved, *argv[1:]]
        env = self._augment_build_env(self._clean_env(), key)
        if env_overrides:
            env.update(env_overrides)
            jh = env_overrides.get("JAVA_HOME")
            if jh:                            # make the switched JDK's java win on PATH
                env["PATH"] = str(Path(jh) / "bin") + os.pathsep + env.get("PATH", "")
        kwargs: dict = dict(
            cwd=str(cwd),
            env=env,
            capture_output=True,
            # Decode as UTF-8 but NEVER raise on a stray non-UTF-8 byte in tool output
            # (git diff of a Latin-1/binary-ish file, mvn logs, etc.). A strict-decode crash
            # here used to escape the tool layer and FAIL the whole run; replace keeps the
            # agent in the loop so it can self-heal. (encoding+errors implies text mode.)
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if self.is_windows:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["preexec_fn"] = self._preexec()

        start = time.monotonic()
        try:
            proc = subprocess.run(real_argv, **kwargs)
            result = CommandResult(
                argv=argv, exit_code=proc.returncode,
                stdout=proc.stdout or "", stderr=proc.stderr or "",
                timed_out=False, duration_ms=int((time.monotonic() - start) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                argv=argv, exit_code=-1,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
                timed_out=True, duration_ms=int((time.monotonic() - start) * 1000),
            )
        _log_command_diag(argv, cwd, result)
        return result


# Module-level singleton — the worker resolves the platform once.
adapter = PlatformAdapter()
