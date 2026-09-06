# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the pure code-change scoring module (Slice 25).

These tests run under regular pytest (no `eval` marker — they're pure).
They validate that the scoring logic itself is correct, independent of
whether any agent output has been produced.
"""
from __future__ import annotations

import pytest

from tests.eval.code_change_metrics import (
    AggregateScore,
    CaseScore,
    _basename,
    aggregate,
    check_regression,
    compute_contains_score,
    compute_f1,
    compute_file_precision,
    compute_file_recall,
    compute_forbidden_violations,
    score_case,
)


# ──────────────────────────────────────────────────────────────────────────────
# _basename
# ──────────────────────────────────────────────────────────────────────────────

class TestBasename:

    def test_forward_slash_path(self):
        assert _basename("src/main/java/Foo.java") == "Foo.java"

    def test_bare_filename(self):
        assert _basename("Foo.java") == "Foo.java"

    def test_backslash_path_normalised(self):
        assert _basename("src\\main\\Foo.java") == "Foo.java"

    def test_empty(self):
        assert _basename("") == ""

    def test_trailing_slash(self):
        # os.path.basename returns "" for trailing slash; acceptable edge.
        assert _basename("src/main/java/") == ""


# ──────────────────────────────────────────────────────────────────────────────
# Precision / recall / F1
# ──────────────────────────────────────────────────────────────────────────────

class TestPrecisionRecallF1:

    def test_perfect_overlap(self):
        expected = {"A.java", "B.java"}
        generated = {"A.java", "B.java"}
        assert compute_file_precision(expected, generated) == 1.0
        assert compute_file_recall(expected, generated) == 1.0

    def test_zero_overlap(self):
        assert compute_file_precision({"A.java"}, {"X.java"}) == 0.0
        assert compute_file_recall({"A.java"}, {"X.java"}) == 0.0

    def test_over_generation_drops_precision(self):
        expected = {"A.java"}
        generated = {"A.java", "B.java", "C.java"}
        assert compute_file_precision(expected, generated) == pytest.approx(1/3)
        assert compute_file_recall(expected, generated) == 1.0

    def test_missing_expected_drops_recall(self):
        expected = {"A.java", "B.java", "C.java"}
        generated = {"A.java"}
        assert compute_file_precision(expected, generated) == 1.0
        assert compute_file_recall(expected, generated) == pytest.approx(1/3)

    def test_empty_generated_gives_vacuous_precision(self):
        assert compute_file_precision({"A.java"}, set()) == 1.0

    def test_empty_expected_gives_vacuous_recall(self):
        assert compute_file_recall(set(), {"A.java"}) == 1.0

    def test_f1_harmonic_mean(self):
        assert compute_f1(1.0, 1.0) == 1.0
        assert compute_f1(0.0, 0.0) == 0.0
        assert compute_f1(1.0, 0.5) == pytest.approx(2/3)

    def test_f1_handles_zero_precision_zero_recall(self):
        assert compute_f1(0.0, 0.5) == 0.0
        assert compute_f1(0.5, 0.0) == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# contains_score
# ──────────────────────────────────────────────────────────────────────────────

class TestContainsScore:

    def test_all_substrings_present(self):
        files = {"src/Foo.java": "class Foo { @Retryable void bar() {} }"}
        expected = {"Foo.java": ["@Retryable", "void bar"]}
        score, misses = compute_contains_score(files, expected)
        assert score == 1.0
        assert misses == []

    def test_partial_presence(self):
        files = {"Foo.java": "class Foo { @Retryable }"}
        expected = {"Foo.java": ["@Retryable", "missingOne", "missingTwo"]}
        score, misses = compute_contains_score(files, expected)
        assert score == pytest.approx(1/3)
        assert len(misses) == 1
        assert misses[0]["file"] == "Foo.java"
        assert set(misses[0]["missing_substrings"]) == {"missingOne", "missingTwo"}

    def test_missing_file_counts_all_substrings_as_misses(self):
        files = {"Bar.java": "anything"}
        expected = {"Foo.java": ["a", "b"]}
        score, misses = compute_contains_score(files, expected)
        assert score == 0.0
        assert misses[0]["missing_substrings"] == ["a", "b"]

    def test_basename_match_across_path_differences(self):
        # Generator emits a full path; gold references just the basename.
        files = {"src/main/java/com/network/Foo.java": "@Retryable"}
        expected = {"Foo.java": ["@Retryable"]}
        score, misses = compute_contains_score(files, expected)
        assert score == 1.0
        assert misses == []

    def test_empty_expectations_scores_vacuously(self):
        score, misses = compute_contains_score({"x.java": "body"}, {})
        assert score == 1.0
        assert misses == []

    def test_empty_substring_list_skipped(self):
        score, _ = compute_contains_score({"Foo.java": ""}, {"Foo.java": []})
        assert score == 1.0


# ──────────────────────────────────────────────────────────────────────────────
# forbidden_violations
# ──────────────────────────────────────────────────────────────────────────────

class TestForbiddenViolations:

    def test_no_forbidden_no_violations(self):
        assert compute_forbidden_violations({"Foo.java": "x"}, []) == []

    def test_forbidden_not_touched(self):
        assert compute_forbidden_violations({"Foo.java": "x"}, ["application.yml"]) == []

    def test_forbidden_touched(self):
        out = compute_forbidden_violations(
            {"application.yml": "server.port=8080"}, ["application.yml"],
        )
        assert out == ["application.yml"]

    def test_basename_match_for_forbidden(self):
        out = compute_forbidden_violations(
            {"src/main/resources/application.yml": "x"}, ["application.yml"],
        )
        assert out == ["application.yml"]

    def test_sorted_output(self):
        out = compute_forbidden_violations(
            {"z.txt": "", "a.txt": ""}, ["z.txt", "a.txt"],
        )
        assert out == ["a.txt", "z.txt"]


# ──────────────────────────────────────────────────────────────────────────────
# score_case
# ──────────────────────────────────────────────────────────────────────────────

class TestScoreCase:

    def test_happy_path_all_hits(self):
        case = {
            "id": "cc001",
            "expected_files_modified": ["src/Foo.java"],
            "expected_files_new": ["src/Bar.java"],
            "expected_contains": {"Foo.java": ["@Retryable"]},
            "forbidden_files": ["application.yml"],
        }
        generated = {
            "Foo.java": "class Foo { @Retryable void x() {} }",
            "Bar.java": "class Bar {}",
        }
        score = score_case(case, generated)
        assert score.case_id == "cc001"
        assert score.parse_success is True
        assert score.file_precision == 1.0
        assert score.file_recall == 1.0
        assert score.file_f1 == 1.0
        assert score.contains_score == 1.0
        assert score.forbidden_violations == []

    def test_parse_failure_zeroes_everything(self):
        case = {
            "id": "cc-fail",
            "expected_files_modified": ["Foo.java"],
        }
        score = score_case(case, {}, parse_success=False)
        assert score.parse_success is False
        assert score.file_precision == 0.0
        assert score.file_recall == 0.0
        assert score.file_f1 == 0.0
        assert score.contains_score == 0.0

    def test_forbidden_violation_reported(self):
        case = {
            "id": "cc-forbid",
            "forbidden_files": ["application.yml"],
        }
        generated = {"application.yml": "x", "Foo.java": "y"}
        score = score_case(case, generated)
        assert score.forbidden_violations == ["application.yml"]

    def test_over_generation(self):
        case = {
            "id": "cc-over",
            "expected_files_modified": ["Foo.java"],
        }
        generated = {"Foo.java": "", "Bar.java": "", "Baz.java": ""}
        score = score_case(case, generated)
        assert score.file_precision == pytest.approx(1/3)
        assert score.file_recall == 1.0
        assert score.file_f1 == pytest.approx(0.5)

    def test_case_dict_serialisation(self):
        case = {"id": "cc1", "expected_files_modified": ["A.java"]}
        score = score_case(case, {"A.java": ""})
        d = score.to_dict()
        assert d["case_id"] == "cc1"
        assert d["file_f1"] == 1.0
        assert d["expected_basenames"] == ["A.java"]
        assert d["generated_basenames"] == ["A.java"]


# ──────────────────────────────────────────────────────────────────────────────
# aggregate
# ──────────────────────────────────────────────────────────────────────────────

class TestAggregate:

    def test_empty_input_zeroed(self):
        agg = aggregate([])
        assert agg.n_cases == 0
        assert agg.macro_f1 == 0.0
        assert agg.parse_success_rate == 0.0

    def test_macro_average(self):
        scores = [
            CaseScore(
                case_id="a", parse_success=True,
                file_precision=1.0, file_recall=1.0, file_f1=1.0,
                contains_score=1.0,
            ),
            CaseScore(
                case_id="b", parse_success=True,
                file_precision=0.5, file_recall=0.5, file_f1=0.5,
                contains_score=0.0,
            ),
            CaseScore(
                case_id="c", parse_success=False,
                file_precision=0.0, file_recall=0.0, file_f1=0.0,
                contains_score=0.0,
            ),
        ]
        agg = aggregate(scores)
        assert agg.n_cases == 3
        assert agg.parse_success_rate == pytest.approx(2/3)
        assert agg.macro_f1 == pytest.approx(0.5)
        assert agg.macro_contains_score == pytest.approx(1/3)

    def test_forbidden_violation_counts_summed(self):
        scores = [
            CaseScore(case_id="a", parse_success=True, file_precision=1, file_recall=1, file_f1=1,
                      contains_score=1, forbidden_violations=["x.yml"]),
            CaseScore(case_id="b", parse_success=True, file_precision=1, file_recall=1, file_f1=1,
                      contains_score=1, forbidden_violations=["y.yml", "z.yml"]),
        ]
        agg = aggregate(scores)
        assert agg.total_forbidden_violations == 3


# ──────────────────────────────────────────────────────────────────────────────
# check_regression
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckRegression:

    def _agg(self, **kw) -> AggregateScore:
        defaults = dict(
            n_cases=5, parse_success_rate=1.0,
            macro_precision=0.8, macro_recall=0.8, macro_f1=0.8,
            macro_contains_score=0.8, total_forbidden_violations=0,
        )
        defaults.update(kw)
        return AggregateScore(**defaults)

    def test_no_baseline_change_passes(self):
        current = self._agg(macro_f1=0.8, macro_contains_score=0.8)
        baseline = current.to_dict()
        result = check_regression(current, baseline)
        assert result.passed is True
        assert result.reasons == []

    def test_f1_within_tolerance_passes(self):
        current = self._agg(macro_f1=0.79)    # 1pp below baseline
        baseline = self._agg(macro_f1=0.80).to_dict()
        result = check_regression(current, baseline, tolerance_pp=2.0)
        assert result.passed is True

    def test_f1_beyond_tolerance_fails(self):
        current = self._agg(macro_f1=0.75)    # 5pp below baseline
        baseline = self._agg(macro_f1=0.80).to_dict()
        result = check_regression(current, baseline, tolerance_pp=2.0)
        assert result.passed is False
        assert any("macro_f1" in r for r in result.reasons)

    def test_contains_regression_flagged(self):
        current = self._agg(macro_contains_score=0.50)
        baseline = self._agg(macro_contains_score=0.80).to_dict()
        result = check_regression(current, baseline)
        assert result.passed is False
        assert any("contains" in r for r in result.reasons)

    def test_forbidden_violations_strict_zero_tolerance(self):
        """Forbidden violations going up is always a fail — no tolerance."""
        current = self._agg(total_forbidden_violations=1)
        baseline = self._agg(total_forbidden_violations=0).to_dict()
        result = check_regression(current, baseline, tolerance_pp=99.0)   # huge tol
        assert result.passed is False
        assert any("forbidden" in r for r in result.reasons)

    def test_improvement_passes(self):
        """Higher-than-baseline scores pass cleanly."""
        current = self._agg(macro_f1=0.95, macro_contains_score=0.95)
        baseline = self._agg(macro_f1=0.80, macro_contains_score=0.80).to_dict()
        result = check_regression(current, baseline)
        assert result.passed is True

    def test_multiple_regressions_all_reported(self):
        current = self._agg(macro_f1=0.5, macro_contains_score=0.5,
                             total_forbidden_violations=2)
        baseline = self._agg(macro_f1=0.9, macro_contains_score=0.9,
                              total_forbidden_violations=0).to_dict()
        result = check_regression(current, baseline)
        assert result.passed is False
        assert len(result.reasons) == 3     # f1 + contains + forbidden
