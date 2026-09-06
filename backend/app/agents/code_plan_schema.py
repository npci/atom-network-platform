# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CodePlan schema + validator (Slice 12).

Shape per plan §7.3 but flattened — one `files` list across repos (repo is a
per-file attribute) plus an optional `tests` list. Kept simple for v0; we can
layer repo-grouping on top later if callers need it.

    {
      "files": [                          # REQUIRED; ≥1 entry
        {
          "path": "src/...",              # REQUIRED str
          "action": "create" | "modify",  # REQUIRED enum
          "intent": "...",                # REQUIRED str, ≥12 chars
          "repo": "common-infra",         # optional str
          "signatures_to_add": ["..."],   # optional list[str]
          "callers_impacted": ["..."]     # optional list[str]
        }
      ],
      "tests": [                          # optional; may be [] or omitted
        {
          "path": "test/...",             # REQUIRED str
          "action": "create" | "modify",  # REQUIRED
          "cases": ["case_a", "case_b"]   # REQUIRED list[str], ≥1 entry
        }
      ],
      "notes": "..."                      # optional free-text
    }

The validator is pure and returns a coverage report (same pattern as
enrichment_schema / citation_validator — no LLM, no I/O).
"""
from __future__ import annotations

from typing import Any

ALLOWED_FILE_ACTIONS = ("create", "modify")
MIN_INTENT_CHARS = 12

# Schema-level keys
REQUIRED_PLAN_KEYS = ("files",)
OPTIONAL_PLAN_KEYS = ("tests", "notes")
ALL_PLAN_KEYS = REQUIRED_PLAN_KEYS + OPTIONAL_PLAN_KEYS

# Per-file keys
REQUIRED_FILE_KEYS  = ("path", "action", "intent")
OPTIONAL_FILE_KEYS  = ("repo", "signatures_to_add", "callers_impacted")
ALL_FILE_KEYS       = REQUIRED_FILE_KEYS + OPTIONAL_FILE_KEYS

# Per-test keys
REQUIRED_TEST_KEYS = ("path", "action", "cases")
OPTIONAL_TEST_KEYS = ()
ALL_TEST_KEYS      = REQUIRED_TEST_KEYS + OPTIONAL_TEST_KEYS


def _str_nonempty(value: Any, min_chars: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= min_chars


def _validate_file(entry: Any, *, index: int) -> list[str]:
    issues: list[str] = []
    if not isinstance(entry, dict):
        return [f"files[{index}]: not a dict"]

    for key in REQUIRED_FILE_KEYS:
        if key not in entry:
            issues.append(f"files[{index}]: missing required key `{key}`")

    if not _str_nonempty(entry.get("path")):
        issues.append(f"files[{index}]: `path` must be a non-empty string")

    action = entry.get("action")
    if action not in ALLOWED_FILE_ACTIONS:
        issues.append(
            f"files[{index}]: `action` must be one of {ALLOWED_FILE_ACTIONS}, got {action!r}"
        )

    if not _str_nonempty(entry.get("intent"), min_chars=MIN_INTENT_CHARS):
        issues.append(
            f"files[{index}]: `intent` must be a non-empty string ≥{MIN_INTENT_CHARS} chars"
        )

    # Optional fields — just type-check when present
    if "signatures_to_add" in entry and not isinstance(entry["signatures_to_add"], list):
        issues.append(f"files[{index}]: `signatures_to_add` must be a list")
    if "callers_impacted" in entry and not isinstance(entry["callers_impacted"], list):
        issues.append(f"files[{index}]: `callers_impacted` must be a list")
    if "repo" in entry and not _str_nonempty(entry["repo"]):
        issues.append(f"files[{index}]: `repo` must be a non-empty string when present")

    return issues


def _validate_test(entry: Any, *, index: int) -> list[str]:
    issues: list[str] = []
    if not isinstance(entry, dict):
        return [f"tests[{index}]: not a dict"]

    for key in REQUIRED_TEST_KEYS:
        if key not in entry:
            issues.append(f"tests[{index}]: missing required key `{key}`")

    if not _str_nonempty(entry.get("path")):
        issues.append(f"tests[{index}]: `path` must be a non-empty string")

    action = entry.get("action")
    if action not in ALLOWED_FILE_ACTIONS:
        issues.append(
            f"tests[{index}]: `action` must be one of {ALLOWED_FILE_ACTIONS}, got {action!r}"
        )

    cases = entry.get("cases")
    if not isinstance(cases, list) or not cases:
        issues.append(f"tests[{index}]: `cases` must be a non-empty list of strings")
    elif not all(_str_nonempty(c) for c in cases):
        issues.append(f"tests[{index}]: every `cases` entry must be a non-empty string")

    return issues


def validate(plan: dict) -> dict:
    """Validate a CodePlan dict. Returns a coverage report.

    Report shape:
      schema_valid      (bool)   no issues at top, file, or test level
      file_count        (int)
      test_count        (int)
      issues            (list[str])
      missing_required  (list[str])  top-level keys missing
    """
    if not isinstance(plan, dict):
        return {
            "schema_valid":     False,
            "file_count":       0,
            "test_count":       0,
            "issues":           ["plan: not a dict"],
            "missing_required": list(REQUIRED_PLAN_KEYS),
        }

    issues: list[str] = []

    missing_required = [k for k in REQUIRED_PLAN_KEYS if k not in plan]
    for k in missing_required:
        issues.append(f"plan: missing required key `{k}`")

    # files
    files = plan.get("files")
    file_count = 0
    if not isinstance(files, list):
        if files is not None:
            issues.append("plan: `files` must be a list")
    elif not files:
        issues.append("plan: `files` must contain at least one entry")
    else:
        file_count = len(files)
        for i, f in enumerate(files):
            issues.extend(_validate_file(f, index=i))

    # tests (optional)
    tests = plan.get("tests")
    test_count = 0
    if tests is not None:
        if not isinstance(tests, list):
            issues.append("plan: `tests` must be a list when present")
        else:
            test_count = len(tests)
            for i, t in enumerate(tests):
                issues.extend(_validate_test(t, index=i))

    return {
        "schema_valid":     not issues,
        "file_count":       file_count,
        "test_count":       test_count,
        "issues":           issues,
        "missing_required": missing_required,
    }
