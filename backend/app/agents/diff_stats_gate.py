# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Diff-stats gate (Slice 13).

Pure — no LLM, no I/O. Given a list of generated file changes and a CodePlan,
raises structural flags for:

  1. **file_outside_plan** — the editor touched a path that isn't in the plan.
  2. **unexpected_new_file** — a file with `action=create` but no `create`
     entry for that path in the plan.
  3. **large_deletion** — new content size < `(1 - max_shrink) × original_size`
     (default threshold: 30% shrink). Requires `original_files` to be supplied.
  4. **missing_planned_file** — the plan includes a file the editor didn't
     touch (nice-to-have check; classified as low-severity).

Returns a list of flag dicts `{severity, kind, path, message}` — no exceptions,
no printing. Caller decides whether to block, warn, or log.

Wiring: called post-generation in `code_change.py`'s iteration loop after
the editor produces files. Not wired yet — standalone module for this slice.
"""
from __future__ import annotations

DEFAULT_MAX_SHRINK = 0.30   # flag when new size < 70% of original


def _normalise_path(p: str) -> str:
    """Tolerate minor path-normalisation differences (trailing slashes, etc.)."""
    return (p or "").strip().strip("/").strip()


def _plan_file_index(plan: dict) -> dict[str, dict]:
    """Path → plan file entry. Safe on malformed plans (returns empty dict)."""
    if not isinstance(plan, dict):
        return {}
    files = plan.get("files") or []
    if not isinstance(files, list):
        return {}
    index: dict[str, dict] = {}
    for f in files:
        if not isinstance(f, dict):
            continue
        path = _normalise_path(f.get("path"))
        if path:
            index[path] = f
    return index


def check_diff_stats(
    files_changed: list[dict],
    plan: dict,
    *,
    original_files: dict[str, str] | None = None,
    max_shrink: float = DEFAULT_MAX_SHRINK,
) -> list[dict]:
    """Run structural checks over a generated diff against a CodePlan.

    Args:
        files_changed: Editor output as [{path, content, action}]. `action`
            is "create" or "modify". `content` is the post-change file body.
        plan: CodePlan dict (Slice 12 shape). Must contain `files: list`.
        original_files: Optional {path: content} for files that existed
            before the edit. Required to detect large deletions.
        max_shrink: Size-reduction threshold (default 0.30 = 30%). A new
            content size below `(1 - max_shrink) * original_size` flags.

    Returns:
        List of flags `{severity, kind, path, message}`. Empty list means no
        structural concerns — does NOT mean the code is good (that's what
        the reviewers are for).
    """
    flags: list[dict] = []

    plan_index = _plan_file_index(plan)

    # Files the editor touched, keyed by normalised path.
    touched: dict[str, dict] = {}
    for entry in files_changed or []:
        if not isinstance(entry, dict):
            continue
        path = _normalise_path(entry.get("path"))
        if not path:
            continue
        touched[path] = entry

    # Check 1 & 2: every touched file must be in the plan; if action=create
    # but plan has a different action (or no entry), flag unexpected_new_file.
    for path, entry in touched.items():
        action = (entry.get("action") or "").strip().lower()
        plan_entry = plan_index.get(path)
        if plan_entry is None:
            if action == "create":
                flags.append({
                    "severity": "high",
                    "kind":     "unexpected_new_file",
                    "path":     path,
                    "message":  f"Editor created {path!r} but no `create` entry exists for it in the plan.",
                })
            else:
                flags.append({
                    "severity": "high",
                    "kind":     "file_outside_plan",
                    "path":     path,
                    "message":  f"Editor touched {path!r} ({action or 'unknown action'}) but the plan does not reference it.",
                })
            continue

        plan_action = (plan_entry.get("action") or "").strip().lower()
        if action and plan_action and action != plan_action:
            flags.append({
                "severity": "medium",
                "kind":     "action_mismatch",
                "path":     path,
                "message":  f"Editor action {action!r} for {path!r} does not match plan action {plan_action!r}.",
            })

    # Check 3: large deletions. Require original_files.
    if original_files:
        for path, entry in touched.items():
            new_content = entry.get("content") or ""
            if not isinstance(new_content, str):
                continue
            orig_content = original_files.get(path)
            if not isinstance(orig_content, str) or not orig_content:
                continue  # no baseline to compare
            orig_size = len(orig_content)
            new_size = len(new_content)
            if orig_size == 0:
                continue
            shrink = 1.0 - (new_size / orig_size)
            if shrink > max_shrink:
                flags.append({
                    "severity": "high",
                    "kind":     "large_deletion",
                    "path":     path,
                    "message":  (
                        f"New content of {path!r} is {new_size} chars, "
                        f"down from {orig_size} — a {shrink:.0%} reduction "
                        f"(threshold {max_shrink:.0%})."
                    ),
                })

    # Check 4: planned files the editor didn't deliver (low-severity).
    for path in plan_index.keys():
        if path not in touched:
            flags.append({
                "severity": "low",
                "kind":     "missing_planned_file",
                "path":     path,
                "message":  f"Plan included {path!r} but the editor did not produce it.",
            })

    return flags
