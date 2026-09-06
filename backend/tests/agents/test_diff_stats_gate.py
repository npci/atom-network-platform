# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 13 — diff-stats gate (pure, no-LLM).

Locks the flag-generation behaviour for all four check categories:
  - unexpected_new_file (create action, path not in plan)
  - file_outside_plan    (any other action, path not in plan)
  - action_mismatch      (path in plan but action differs)
  - large_deletion       (new content < (1 - max_shrink) × original size)
  - missing_planned_file (plan has file the editor didn't touch)
"""
from __future__ import annotations

from app.agents import diff_stats_gate as gate


# Canonical plan used across tests
_PLAN = {
    "files": [
        {
            "path":   "src/ratelimit/TieredRateLimiter.java",
            "action": "create",
            "intent": "New subclass of RateLimiter",
        },
        {
            "path":   "src/retry/PaymentRetryController.java",
            "action": "modify",
            "intent": "Inject TieredRateLimiter; 429 on reject",
        },
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# Happy path — clean diff
# ──────────────────────────────────────────────────────────────────────────────

def test_clean_diff_no_flags():
    files_changed = [
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "create",
         "content": "public class TieredRateLimiter extends RateLimiter { }"},
        {"path": "src/retry/PaymentRetryController.java", "action": "modify",
         "content": "public class PaymentRetryController { /* modified */ }"},
    ]
    originals = {
        "src/retry/PaymentRetryController.java": "public class PaymentRetryController { /* original */ }",
    }
    flags = gate.check_diff_stats(files_changed, _PLAN, original_files=originals)
    assert flags == []


# ──────────────────────────────────────────────────────────────────────────────
# Check 1 & 2 — file outside plan / unexpected new file
# ──────────────────────────────────────────────────────────────────────────────

def test_file_outside_plan_flagged():
    files_changed = [
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "create", "content": "x"},
        {"path": "src/retry/PaymentRetryController.java", "action": "modify", "content": "x"},
        {"path": "src/unrelated/SomeOther.java", "action": "modify", "content": "x"},   # not in plan
    ]
    flags = gate.check_diff_stats(files_changed, _PLAN)
    kinds = [f["kind"] for f in flags]
    assert "file_outside_plan" in kinds
    off_plan = [f for f in flags if f["kind"] == "file_outside_plan"]
    assert off_plan[0]["path"] == "src/unrelated/SomeOther.java"
    assert off_plan[0]["severity"] == "high"


def test_unexpected_new_file_flagged():
    files_changed = [
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "create", "content": "x"},
        {"path": "src/retry/PaymentRetryController.java", "action": "modify", "content": "x"},
        {"path": "src/bonus/ExtraFactory.java", "action": "create", "content": "x"},   # unexpected create
    ]
    flags = gate.check_diff_stats(files_changed, _PLAN)
    kinds = [f["kind"] for f in flags]
    assert "unexpected_new_file" in kinds
    assert any(f["path"] == "src/bonus/ExtraFactory.java" for f in flags)


def test_action_mismatch_flagged():
    files_changed = [
        # Plan says "create" but editor modified
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "modify", "content": "x"},
        {"path": "src/retry/PaymentRetryController.java", "action": "modify", "content": "x"},
    ]
    flags = gate.check_diff_stats(files_changed, _PLAN)
    kinds = [f["kind"] for f in flags]
    assert "action_mismatch" in kinds
    mismatch = next(f for f in flags if f["kind"] == "action_mismatch")
    assert mismatch["severity"] == "medium"


# ──────────────────────────────────────────────────────────────────────────────
# Check 3 — large deletion
# ──────────────────────────────────────────────────────────────────────────────

def test_large_deletion_flagged():
    """New file body < 70% of original → flag."""
    files_changed = [
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "create", "content": "x"},
        # Editor kept only a fraction of the original payment controller
        {"path": "src/retry/PaymentRetryController.java", "action": "modify",
         "content": "/* stub */"},
    ]
    originals = {
        "src/retry/PaymentRetryController.java": "/* " + ("original " * 200) + "*/",
    }
    flags = gate.check_diff_stats(files_changed, _PLAN, original_files=originals)
    kinds = [f["kind"] for f in flags]
    assert "large_deletion" in kinds
    large = next(f for f in flags if f["kind"] == "large_deletion")
    assert "reduction" in large["message"]


def test_small_deletion_not_flagged():
    """New content ~90% of original → no large-deletion flag."""
    original = "A" * 1000
    new = "A" * 900  # 10% shrink, below the 30% threshold
    files_changed = [
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "create", "content": "x"},
        {"path": "src/retry/PaymentRetryController.java", "action": "modify", "content": new},
    ]
    originals = {"src/retry/PaymentRetryController.java": original}
    flags = gate.check_diff_stats(files_changed, _PLAN, original_files=originals)
    assert not any(f["kind"] == "large_deletion" for f in flags)


def test_large_deletion_skipped_without_originals():
    """No original_files supplied → no large_deletion flag even on empty new content."""
    files_changed = [
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "create", "content": "x"},
        {"path": "src/retry/PaymentRetryController.java", "action": "modify", "content": ""},
    ]
    flags = gate.check_diff_stats(files_changed, _PLAN)  # no originals
    assert not any(f["kind"] == "large_deletion" for f in flags)


def test_large_deletion_threshold_is_configurable():
    files_changed = [
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "create", "content": "x"},
        {"path": "src/retry/PaymentRetryController.java", "action": "modify", "content": "A" * 850},
    ]
    originals = {"src/retry/PaymentRetryController.java": "A" * 1000}
    # 15% shrink — under default threshold but over a strict 0.10
    flags_default = gate.check_diff_stats(files_changed, _PLAN, original_files=originals)
    flags_strict  = gate.check_diff_stats(files_changed, _PLAN, original_files=originals, max_shrink=0.10)
    assert not any(f["kind"] == "large_deletion" for f in flags_default)
    assert     any(f["kind"] == "large_deletion" for f in flags_strict)


# ──────────────────────────────────────────────────────────────────────────────
# Check 4 — missing planned file
# ──────────────────────────────────────────────────────────────────────────────

def test_missing_planned_file_flagged():
    """Plan has 2 files; editor only touches 1 → low-severity flag for the other."""
    files_changed = [
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "create", "content": "x"},
        # PaymentRetryController not touched
    ]
    flags = gate.check_diff_stats(files_changed, _PLAN)
    missing = [f for f in flags if f["kind"] == "missing_planned_file"]
    assert len(missing) == 1
    assert missing[0]["path"] == "src/retry/PaymentRetryController.java"
    assert missing[0]["severity"] == "low"


# ──────────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────────

def test_empty_diff_with_plan_flags_all_planned_missing():
    flags = gate.check_diff_stats([], _PLAN)
    assert all(f["kind"] == "missing_planned_file" for f in flags)
    assert len(flags) == len(_PLAN["files"])


def test_empty_plan_treats_all_touched_as_outside_plan():
    files_changed = [
        {"path": "anywhere/x.java", "action": "modify", "content": "x"},
    ]
    flags = gate.check_diff_stats(files_changed, {"files": []})
    assert any(f["kind"] == "file_outside_plan" for f in flags)


def test_malformed_plan_tolerated():
    """Non-dict or non-list plan doesn't crash; treats as empty plan."""
    files_changed = [{"path": "x.java", "action": "create", "content": "x"}]
    # Non-dict plan
    flags = gate.check_diff_stats(files_changed, "not a dict")
    assert any(f["kind"] in ("file_outside_plan", "unexpected_new_file") for f in flags)
    # Plan with non-list files
    flags = gate.check_diff_stats(files_changed, {"files": "not a list"})
    assert any(f["kind"] in ("file_outside_plan", "unexpected_new_file") for f in flags)


def test_malformed_entries_skipped():
    """Non-dict entries in files_changed are silently skipped, not crashed."""
    files_changed = [
        "not a dict",
        None,
        {"path": "", "action": "create"},   # empty path
        {"path": "src/ratelimit/TieredRateLimiter.java", "action": "create", "content": "x"},
        {"path": "src/retry/PaymentRetryController.java", "action": "modify", "content": "x"},
    ]
    flags = gate.check_diff_stats(files_changed, _PLAN)
    assert flags == []  # the two valid entries match the plan exactly


def test_path_normalisation():
    files_changed = [
        {"path": "/src/ratelimit/TieredRateLimiter.java/", "action": "create", "content": "x"},
        {"path": "src/retry/PaymentRetryController.java", "action": "modify", "content": "x"},
    ]
    # Leading/trailing slashes shouldn't turn valid paths into "outside plan"
    flags = gate.check_diff_stats(files_changed, _PLAN)
    assert not any(f["kind"] == "file_outside_plan" for f in flags)
