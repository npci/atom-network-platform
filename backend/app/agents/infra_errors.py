# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Classify infra/config errors that the USER must fix (not the agent) — THE BOOK §18.

The agentic flow's legitimate stopping point is an INFRA/CONFIG problem the operator must
resolve (a missing credential, an unreachable remote, no disk, a missing toolchain). When
one of those is hit, the run should surface a CLEAR 'fix this, then Retry' message — not a
cryptic stack trace or a silent wedge — and a retry AFTER the fix should just work.

Pure + deterministic (substring catalog, no I/O) so it unit-tests in isolation and can be
reused anywhere a phase fails (push today; clone/build later)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InfraIssue:
    code: str          # stable machine code (UI can branch on it)
    problem: str       # plain-language: what is wrong
    fix: str           # plain-language: what the USER does to fix it
    retryable: bool = True


# (lowercased substrings) -> issue. Order matters: first match wins.
_CATALOG: list[tuple[tuple[str, ...], InfraIssue]] = [
    (("could not read username", "could not read password", "authentication failed",
      "fatal: could not read", "invalid_token", "401 unauthorized", "403 forbidden",
      "permission denied (publickey)", "access denied", "http basic: access denied"),
     InfraIssue("GIT_REMOTE_AUTH",
                "the worker can't authenticate to the Git remote",
                "check the Git access token on the worker has push (write_repository) rights to "
                "this project, and RESTART the worker after changing the token so it reloads, then Retry")),
    (("could not resolve host", "name or service not known", "network is unreachable",
      "connection refused", "connection timed out", "temporary failure in name resolution"),
     InfraIssue("GIT_REMOTE_UNREACHABLE",
                "the Git remote host is unreachable from the worker",
                "check the worker's network and the remote URL, then Retry")),
    (("no space left on device", "disk quota exceeded"),
     InfraIssue("DISK_FULL",
                "the worker host is out of disk space",
                "free disk space on the worker, then Retry")),
    (("mvn: not found", "command not found: mvn", "mvn: command not found",
      "no such file or directory: 'mvn'", "java_home is not set", "jdk not found"),
     InfraIssue("TOOLCHAIN_MISSING",
                "a required build tool (Maven / JDK) is missing on the worker",
                "install the build toolchain on the worker, then Retry")),
]


def classify_infra_error(text: str | None) -> InfraIssue | None:
    """Return the :class:`InfraIssue` for a USER-fixable config/infra error, or None when
    the error is not a recognised config issue (i.e. a genuine code/agent failure that the
    user cannot fix by changing config)."""
    t = (text or "").lower()
    if not t.strip():
        return None
    for needles, issue in _CATALOG:
        if any(n in t for n in needles):
            return issue
    return None
