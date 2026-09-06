# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Review-loop convergence helpers (the fix for the gap-count drifting UP instead of down).

Two pure helpers back the convergence fix:
1. `_review_feedback_errors` — the WHOLE reviewer verdict (must-fix items + advisory notes) is
   handed back to the code agent, not just the blockers. Handing back blockers-only left the
   non-blocking findings unfixed, so they re-surfaced and were re-counted every round.
2. `_untouched_review_files` — a deterministic FLOOR: the agent can't declare "done" while a
   reviewer-flagged file was never touched. Stops the 1-3-edits-then-stop dribble.
"""
from app.agents import agentic_orchestrator as O


# ── _untouched_review_files ────────────────────────────────────────────────────
def test_untouched_flags_files_never_touched():
    flagged = ["transaction-processor/.../V2__odd_hour_config.sql", "FinController.java"]
    touched = ["src/main/java/com/example/tpu/controller/FinController.java"]
    # V2 SQL was flagged but never touched → reported; FinController matched by basename → cleared.
    assert O._untouched_review_files(flagged, touched) == ["v2__odd_hour_config.sql"]


def test_untouched_matches_across_path_prefix_differences():
    # Reviewer names a full repo path; the disk change-set is repo-relative. Basename match clears it.
    flagged = ["repoA/src/main/resources/db/migration/V2__x.sql"]
    touched = ["src/main/resources/db/migration/V2__x.sql"]
    assert O._untouched_review_files(flagged, touched) == []


def test_untouched_empty_when_all_addressed():
    flagged = ["A.java", "B.java"]
    touched = ["pkg/A.java", "other/B.java"]
    assert O._untouched_review_files(flagged, touched) == []


def test_untouched_ignores_blank_files_and_dedupes():
    # Behavioural findings carry no file (""), and the same file can be flagged twice.
    flagged = ["", "Foo.java", "x/Foo.java", None]
    assert O._untouched_review_files(flagged, []) == ["foo.java"]


# ── _review_feedback_errors ────────────────────────────────────────────────────
def _item(file, why, sev="blocker", cat="correctness", fix=None):
    return {"file": file, "line": 12, "category": cat, "severity": sev, "why": why, "suggested_fix": fix}


def test_feedback_includes_blockers_and_advisory_notes():
    items = [_item("A.java", "missing guard", fix="add the guard")]
    notes = [_item("B.java", "minor style", sev="info", cat="convention")]
    errs = O._review_feedback_errors(items, notes)
    assert len(errs) == 2
    # Must-fix first, advisory tagged so the agent (and prompt) can tell them apart.
    assert errs[0].startswith("A.java:12 [correctness] missing guard → fix: add the guard")
    assert errs[1].startswith("[advisory] B.java:12 [convention] minor style")


def test_feedback_blockers_precede_advisory():
    errs = O._review_feedback_errors([_item("A.java", "boom")], [_item("B.java", "nit", sev="warning")])
    assert "[advisory]" not in errs[0] and errs[1].startswith("[advisory]")


def test_feedback_handles_empty_and_missing_fields():
    assert O._review_feedback_errors([], []) == []
    # A finding with no file/fix degrades gracefully to '?'.
    errs = O._review_feedback_errors([{"why": "x", "category": "security"}], [])
    assert errs == ["?:? [security] x"]
