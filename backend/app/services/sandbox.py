# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Sandboxed code execution via Docker-in-Docker (Slice 14).

Given a dict of files (relative path → content), a command, and a base
image, writes the files to a tempdir, mounts it at `/workspace` inside a
fresh container, runs the command, captures stdout/stderr, and returns a
`SandboxResult`. The container is always cleaned up.

Hard isolation defaults:
  - `network_disabled=True`  (no outbound network; plan §7.4)
  - `mem_limit` enforced     (OOM → `killed_for_oom=True`)
  - `nano_cpus` enforced     (CPU cap)
  - hard `timeout`           (via `container.wait(timeout=...)`; kill on expiry)

Not wired into any caller yet — Slice 15 (self-correction loop) is the first.

Path safety: `repo_files` keys must be relative and contain no `..`. Any
violation raises `ValueError` before any Docker activity.
"""
from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


WORKSPACE_MOUNT = "/workspace"


@dataclass
class SandboxResult:
    """Outcome of a single sandboxed execution."""
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    killed_for_timeout: bool = False
    killed_for_oom: bool = False
    image: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers — unit-tested without Docker
# ──────────────────────────────────────────────────────────────────────────────

def _validate_paths(repo_files: dict[str, str]) -> None:
    """Reject absolute paths and any segment equal to `..`. Raises ValueError."""
    for rel_path in repo_files.keys():
        if not rel_path or not isinstance(rel_path, str):
            raise ValueError(f"Unsafe path (empty or non-string): {rel_path!r}")
        p = Path(rel_path)
        if p.is_absolute():
            raise ValueError(f"Unsafe absolute path: {rel_path!r}")
        if any(part == ".." for part in p.parts):
            raise ValueError(f"Unsafe path contains '..': {rel_path!r}")


def _build_container_kwargs(
    *,
    image: str,
    command: str | list[str],
    workdir_host: str,
    memory_limit: str,
    cpu_limit: float,
    network_disabled: bool,
) -> dict[str, Any]:
    """Pure builder for `docker.containers.run(**kwargs)`. Unit-testable."""
    return {
        "image":            image,
        "command":          command,
        "volumes":          {workdir_host: {"bind": WORKSPACE_MOUNT, "mode": "rw"}},
        "working_dir":      WORKSPACE_MOUNT,
        "network_disabled": network_disabled,
        "mem_limit":        memory_limit,
        "nano_cpus":        int(cpu_limit * 1_000_000_000),
        "detach":           True,
        "stdout":           True,
        "stderr":           True,
    }


def _materialise_files(repo_files: dict[str, str], workdir: Path) -> None:
    """Write all files to workdir. Paths must already be validated."""
    for rel_path, content in repo_files.items():
        full = workdir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content or "", encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# Docker availability
# ──────────────────────────────────────────────────────────────────────────────

def is_docker_available() -> bool:
    """Fast reachability probe. Returns False on any failure (SDK absent,
    daemon down, permission denied). Never raises."""
    try:
        import docker  # noqa: WPS433 (local import keeps module importable without SDK)
        client = docker.from_env()
        client.ping()
        return True
    except Exception as e:
        logger.debug("is_docker_available: %s", e)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Main entry
# ──────────────────────────────────────────────────────────────────────────────

def run_in_sandbox(
    repo_files: dict[str, str],
    command: str | list[str],
    *,
    image: str | None = None,
    timeout: int | None = None,
    memory: str | None = None,
    cpus: float | None = None,
    network_disabled: bool = True,
) -> SandboxResult:
    """Run `command` in a fresh container with `repo_files` mounted at /workspace.

    All limits default to `settings.sandbox_*` but may be overridden per call.

    Never raises. Any Docker exception is mapped into a `SandboxResult` with
    `exit_code=-1` and the error on `stderr`, so callers can treat sandbox
    failures the same as command failures.
    """
    image   = image   or settings.sandbox_image_java
    timeout = timeout or settings.sandbox_timeout_seconds
    memory  = memory  or settings.sandbox_memory_limit
    cpus    = cpus    if cpus is not None else settings.sandbox_cpu_limit

    _validate_paths(repo_files)

    # Late SDK import so a missing `docker` package doesn't break module import.
    try:
        import docker
        from docker.errors import APIError, ImageNotFound
    except ImportError as e:
        logger.error("docker SDK not installed: %s", e)
        return SandboxResult(
            exit_code=-1, stdout="", stderr=f"docker SDK not installed: {e}",
            duration_seconds=0.0, image=image,
        )

    try:
        client = docker.from_env()
    except Exception as e:
        logger.error("docker.from_env() failed: %s", e)
        return SandboxResult(
            exit_code=-1, stdout="", stderr=f"docker client init failed: {e}",
            duration_seconds=0.0, image=image,
        )

    with tempfile.TemporaryDirectory(prefix="sandbox-") as workdir_str:
        workdir = Path(workdir_str)
        _materialise_files(repo_files, workdir)

        kwargs = _build_container_kwargs(
            image=image, command=command, workdir_host=str(workdir),
            memory_limit=memory, cpu_limit=cpus, network_disabled=network_disabled,
        )

        container = None
        start = time.time()
        try:
            try:
                container = client.containers.run(**kwargs)
            except ImageNotFound:
                return SandboxResult(
                    exit_code=-1, stdout="",
                    stderr=f"image not found: {image}",
                    duration_seconds=time.time() - start, image=image,
                )
            except APIError as e:
                return SandboxResult(
                    exit_code=-1, stdout="",
                    stderr=f"docker API error: {e}",
                    duration_seconds=time.time() - start, image=image,
                )

            # Block until completion or timeout.
            try:
                wait_result = container.wait(timeout=timeout)
            except Exception as e:
                # `container.wait` raises on timeout (requests.ReadTimeout et al.)
                duration = time.time() - start
                try:
                    container.kill()
                except Exception:
                    pass
                stdout, stderr = _collect_logs(container)
                return SandboxResult(
                    exit_code=-1, stdout=stdout,
                    stderr=stderr + f"\ntimeout after {timeout}s: {e}",
                    duration_seconds=duration, killed_for_timeout=True,
                    image=image,
                )

            duration = time.time() - start
            exit_code = wait_result.get("StatusCode", -1) if isinstance(wait_result, dict) else -1
            stdout, stderr = _collect_logs(container)

            killed_for_oom = False
            try:
                state = (container.attrs or {}).get("State") or {}
                # Reload attrs to get fresh state (attrs is cached on first access).
                container.reload()
                state = (container.attrs or {}).get("State") or {}
                killed_for_oom = bool(state.get("OOMKilled", False))
            except Exception:
                pass

            return SandboxResult(
                exit_code=exit_code, stdout=stdout, stderr=stderr,
                duration_seconds=duration, killed_for_oom=killed_for_oom,
                image=image,
            )
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception as e:
                    logger.debug("container.remove failed: %s", e)


def _collect_logs(container) -> tuple[str, str]:
    """Read stdout/stderr streams from a stopped container. Never raises."""
    try:
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
    except Exception:
        stdout = ""
    try:
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
    except Exception:
        stderr = ""
    return stdout, stderr
