# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""System-dependency preflight (THE BOOK §18.1).

Runs once per run (a sub-step of ``workspace_ready``) and **fails the run early**
— a §9 hard-gate failure, never a silent pass — when a *required* tool is
missing. Optional tools (e.g. ``jdtls`` for the §8 ``lsp_*`` structural tier)
degrade to a warning and fall back to symbol-graph + AST + grep.

The verdict is data, not prose: ``blocking_missing`` non-empty ⇒ the run cannot
verify, so it must not start a phase that would later claim "verified" without a
toolchain.
"""
from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.agents.platform_adapter import adapter
from app.core.config import settings

# The toolchain (git/mvn/java/javac presence + versions, disk, GITLAB_TOKEN) is
# effectively static for a process. The `/agentic/preflight` endpoint is polled
# by the UI every ~minute, and each uncached call re-spawned 4 `--version`
# subprocesses — wasteful and it spammed the command log. Cache the report for a
# short TTL; the per-run preflight passes use_cache=False when it needs a fresh
# read (e.g. right before driving a run).
_REPORT_CACHE: dict[tuple, tuple[float, "ToolchainReport"]] = {}
_REPORT_TTL_S = 120

# git is the ONLY hard requirement (without it we can't even clone). mvn/java/javac
# are needed only by the LOCAL verification backend — when they're absent the run
# degrades to the deferred verifier (§9) instead of failing, so they are OPTIONAL
# here and reported as warnings, never blocking.
_REQUIRED_TOOLS = ("git",)
_BUILD_TOOLS = ("mvn", "java", "javac")
_DEFAULT_REQUIRED_TOOLS = _REQUIRED_TOOLS + _BUILD_TOOLS
_MIN_DISK_FREE_GB = 5


@dataclass
class ToolInfo:
    name: str
    found: bool
    version: str | None = None
    ok: bool = False


@dataclass
class ToolchainReport:
    platform: str
    tools: dict[str, ToolInfo] = field(default_factory=dict)
    jdk_majors: list[int] = field(default_factory=list)
    disk_free_gb: float | None = None
    gitlab_token_present: bool = False
    warnings: list[str] = field(default_factory=list)
    blocking_missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking_missing

    @property
    def build_ready(self) -> bool:
        """True iff the LOCAL verification toolchain (mvn + javac) is present."""
        return all(self.tools.get(t) is not None and self.tools[t].found for t in _BUILD_TOOLS)


def _version_of(tool: str, flag: str) -> str | None:
    """Run ``tool flag`` and return the first non-empty output line (version
    banners go to stdout or stderr depending on the tool)."""
    exe = adapter.resolve(tool, Path("."))
    if not exe:
        return None
    try:
        res = adapter.run_command(".", [tool, flag], timeout_s=30)
    except Exception:
        return None
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _java_major(version_line: str | None) -> int | None:
    """Parse a JDK major from a `java -version` banner. Handles legacy 1.8."""
    if not version_line:
        return None
    m = re.search(r'version "?(\d+)(?:\.(\d+))?', version_line)
    if not m:
        return None
    first = int(m.group(1))
    return int(m.group(2)) if first == 1 and m.group(2) else first


def build_toolchain_report(required_tools: tuple[str, ...] = _DEFAULT_REQUIRED_TOOLS,
                           *, use_cache: bool = True) -> ToolchainReport:
    if use_cache:
        hit = _REPORT_CACHE.get(required_tools)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    report = ToolchainReport(platform=adapter.system)

    for tool in required_tools:
        flag = "-version" if tool in ("java", "javac") else "--version" if tool == "git" else "-v"
        version = _version_of(tool, flag)
        found = version is not None
        info = ToolInfo(name=tool, found=found, version=version, ok=found)
        report.tools[tool] = info
        if not found:
            if tool in _REQUIRED_TOOLS:
                report.blocking_missing.append(tool)        # hard: can't proceed
            else:
                report.warnings.append(                      # soft: local verify unavailable → defer to CI
                    f"{tool} not found — local verification unavailable; will defer to CI")

    java_info = report.tools.get("java")
    if java_info and java_info.found:
        major = _java_major(java_info.version)
        if major is not None:
            report.jdk_majors.append(major)

    # Optional: jdtls — degrade, never block (§8/§18.1).
    if shutil.which("jdtls") is None:
        report.warnings.append("jdtls not found — lsp_* tools fall back to symbol-graph + AST + grep")

    # Disk under (an existing ancestor of) the workspace root.
    probe = Path(settings.agentic_workspace_root)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free_gb = shutil.disk_usage(str(probe)).free / (1024 ** 3)
        report.disk_free_gb = round(free_gb, 1)
        if free_gb < _MIN_DISK_FREE_GB:
            report.warnings.append(f"low disk: {free_gb:.1f} GB free under {settings.agentic_workspace_root}")
    except OSError:
        report.warnings.append("could not stat workspace disk")

    report.gitlab_token_present = bool(settings.gitlab_token)
    if not report.gitlab_token_present:
        report.blocking_missing.append("GITLAB_TOKEN")

    _REPORT_CACHE[required_tools] = (time.monotonic() + _REPORT_TTL_S, report)
    return report
