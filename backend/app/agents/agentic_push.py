# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared short branch + multi-repo push actions (THE BOOK §12).

The push phase performs the ONLY remote mutation — a ``git push`` of the new,
app-created branch (enforced by the git-guard, §22). This module builds the
deterministic, testable pieces: the title-derived shared branch name and the
add/modify/**delete** GitLab commit actions from a ChangeSet. The live GitLab
calls reuse the existing ``git_integrator`` primitives (branch off the approved
base SHA, create MR) — extended here only to carry deletes + base-SHA validation.
"""
from __future__ import annotations

import re

_SLUG_CAP = 40


def _branch_prefix() -> str:
    """Configurable namespace prefix for agent branches (default ``atom``).
    Case is preserved; only git-illegal chars
    are sanitized and any wrapping slashes are trimmed so the join stays ``<prefix>/…``."""
    from app.core.config import settings
    raw = (getattr(settings, "agentic_branch_prefix", None) or "atom").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._/-]+", "-", raw).strip("/-")
    return cleaned or "atom"


def branch_name(title: str | None, suffix: str | None = None) -> str:
    """Title-derived shared branch — ``<prefix>/xsd-<slug>`` (§12), prefix from
    ``settings.agentic_branch_prefix`` (default ``atom``) so agent branches
    are distinguishable from human ones. Slug sanitized to ``[a-z0-9-]``, length-capped;
    a collision suffix is appended atomically for ALL repos (preserving the shared-branch
    invariant), never per-repo."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "change").lower()).strip("-")[:_SLUG_CAP]
    base = f"{_branch_prefix()}/xsd-{slug or 'change'}"
    return f"{base}-{suffix}" if suffix else base


def gitlab_actions(change_set, repo_id: str | None = None) -> list[dict]:
    """ChangeSet FileOps → GitLab commit ``actions`` (§12), optionally for one
    repo. ``add→create``, ``modify→update``, ``delete→delete`` (the legacy push
    built only create/update — delete is new)."""
    actions: list[dict] = []
    for op in change_set.operations:
        if repo_id is not None and op.repo_id != repo_id:
            continue
        if op.op == "add":
            actions.append({"action": "create", "file_path": op.path, "content": op.content or ""})
        elif op.op == "modify":
            actions.append({"action": "update", "file_path": op.path, "content": op.content or ""})
        elif op.op == "delete":
            actions.append({"action": "delete", "file_path": op.path})
    return actions
