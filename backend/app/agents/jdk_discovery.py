# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Installed-JDK discovery + selection (Java-version awareness, §18.1).

The agentic verify gate builds offline with `mvn`. When a module targets a newer
Java than the *active* JDK (e.g. the repo is on Java 25 but the box's default is
17), the compile aborts with `invalid target release: N` — an ENVIRONMENT problem,
not a code defect. The fix is to BUILD WITH THE RIGHT JDK: find an installed JDK
whose major matches what the module needs and point `JAVA_HOME` at it for that
build (the switch). Only when no matching JDK is installed do we need to install
one (a privileged, human-approved step handled elsewhere).

Pure + side-effect-free (filesystem reads only) so it stays unit-testable: feed it
a fake set of JDK homes (dirs with a `release` file) and assert the selection.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("app.agentic")

# Standard JDK install roots across the platforms we run on. SDKMAN first — it's the
# non-root way `agappuser` can hold several JDKs side by side on the UAT box.
_SDKMAN = Path(os.path.expanduser("~")) / ".sdkman" / "candidates" / "java"
_JVM_DIRS = (
    str(_SDKMAN),
    "/usr/lib/jvm",                          # Debian/Ubuntu, RHEL
    "/usr/java",                             # Oracle RPM
    "/opt/java", "/opt/jdk", "/opt/jdks",
    "/Library/Java/JavaVirtualMachines",     # macOS
)
_VERSION_RE = re.compile(r'JAVA_VERSION="?(?:1\.)?(\d+)')


def _home_norm(p: Path) -> Path:
    """macOS bundles keep the JDK under Contents/Home — normalise to it."""
    mac = p / "Contents" / "Home"
    return mac if mac.is_dir() else p


def jdk_major(home: Path) -> int | None:
    """Read a JDK's major from its ``release`` file (no process spawn). Handles the
    legacy ``1.8`` scheme (→ 8). Returns None if it isn't a readable JDK home."""
    rel = home / "release"
    if not rel.is_file():
        return None
    try:
        for line in rel.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _VERSION_RE.match(line.strip())
            if m:
                return int(m.group(1))
    except OSError:
        return None
    return None


def _parse_update_alternatives(text: str) -> list[str]:
    """`update-alternatives --list java` → JDK homes (strip the trailing /bin/java)."""
    homes = []
    for line in text.splitlines():
        p = line.strip()
        if p.endswith("/bin/java"):
            homes.append(p[: -len("/bin/java")])
    return homes


def _parse_update_java_alternatives(text: str) -> list[str]:
    """`update-java-alternatives -l` → lines 'name priority /path/to/home'."""
    homes = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1].startswith("/"):
            homes.append(parts[-1])
    return homes


def _alternatives_homes() -> list[str]:
    """JDK homes the box registered with the system ALTERNATIVES mechanism — the
    canonical 'what's installed' list the user asked us to choose from. Read-only
    (`--list`/`-l`); never mutates the global alternative (we switch per-build via
    JAVA_HOME, which needs no root). Best-effort: absent tools → empty.

    Routed through core.process_executor.ProcessExecutor (S5 call-site
    migration, closing ARCHITECTURE_REVIEW_ACTIONS.md S5 for this file) —
    same fixed argv, same behavior, now with the allowlist/timeout/audit-
    logging chokepoint every OS process this platform spawns should share.
    """
    from app.core.process_executor import (
        ProcessExecutionRequest, ProcessNotAllowedError, ProcessTimeoutError,
        run_sync,
    )
    from pathlib import Path as _Path

    homes: list[str] = []
    for command, args, parser in (
        ("update-java-alternatives", ["-l"], _parse_update_java_alternatives),
        ("update-alternatives", ["--list", "java"], _parse_update_alternatives),
    ):
        try:
            result = run_sync(ProcessExecutionRequest(
                command=command, args=args, cwd=_Path.cwd(), timeout_s=10,
                actor="jdk_discovery",
            ))
            if result.exit_code == 0:
                homes += parser(result.stdout or "")
        except (ProcessNotAllowedError, ProcessTimeoutError, FileNotFoundError):
            continue
        except Exception:  # noqa: BLE001 — tool absent (non-Debian) / no PATH; skip
            continue
    return homes


def discover_jdks(extra_roots: tuple[str, ...] = ()) -> dict[int, str]:
    """Map ``major -> JAVA_HOME`` for every JDK the box exposes — via the system
    ALTERNATIVES list, the standard install roots, and the active JAVA_HOME. First
    one wins per major; never raises."""
    found: dict[int, str] = {}
    candidates: list[Path] = []
    for env_var in ("JAVA_HOME", "JDK_HOME"):
        v = os.environ.get(env_var)
        if v:
            candidates.append(Path(v))
    candidates += [Path(h) for h in _alternatives_homes()]
    for base in (*_JVM_DIRS, *extra_roots):
        b = Path(base)
        if not b.is_dir():
            continue
        try:
            for child in sorted(b.iterdir()):
                if child.is_dir() and child.name != "current":
                    candidates.append(child)
        except OSError:
            continue
    for home in candidates:
        h = _home_norm(home)
        major = jdk_major(h)
        if major is not None and major not in found:
            found[major] = str(h)
    return found


def select_jdk_home(required_major: int | None, *, jdks: dict[int, str] | None = None) -> str | None:
    """The JAVA_HOME to build with for a module that needs ``required_major`` — or
    None when nothing is required or no installed JDK matches (→ install needed)."""
    if not required_major:
        return None
    return (jdks if jdks is not None else discover_jdks()).get(required_major)
