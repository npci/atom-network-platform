# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pure scoring functions for the code-change evaluation (Slice 25).

All functions here are pure: no DB, no LLM, no filesystem. They take
in-memory dicts/sets and return numbers. This means the eval scoring
itself can be regression-tested in milliseconds — a property the
live-agent runner can never have.

Conventions:
  - A generator produces a mapping `{relative_path: file_content}`.
  - A gold case specifies `expected_files_modified` + `expected_files_new`
    + `expected_contains: dict[path → list[substring]]` + `forbidden_files`.
  - Path comparison is done by **basename match**: `NetworkSwitchService.java`
    matches `src/main/java/com/network/core/service/NetworkSwitchService.java`.
    This survives project-layout refactors and keeps gold-set authoring
    cheap (reviewers don't need to guess the full path).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


# ──────────────────────────────────────────────────────────────────────────────
# Path normalisation
# ──────────────────────────────────────────────────────────────────────────────

def _basename(path: str) -> str:
    """Return the bare filename portion of a path. Normalises separators.

    `src/main/java/com/network/Foo.java` → `Foo.java`
    `Foo.java`                       → `Foo.java`
    """
    if not path:
        return ""
    # Handle both / and \ separators.
    return os.path.basename(path.replace("\\", "/"))


def _index_by_basename(paths: list[str] | set[str]) -> dict[str, str]:
    """Return {basename: original_path} — first wins on duplicates."""
    out: dict[str, str] = {}
    for p in paths:
        bn = _basename(p)
        if bn and bn not in out:
            out[bn] = p
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Core metrics
# ──────────────────────────────────────────────────────────────────────────────

def compute_file_precision(expected_basenames: set[str], generated_basenames: set[str]) -> float:
    """Precision = correctly-generated files / total generated files.

    Vacuously 1.0 when nothing was generated (the generator touched zero
    files — no false positives).
    """
    if not generated_basenames:
        return 1.0
    hits = expected_basenames & generated_basenames
    return len(hits) / len(generated_basenames)


def compute_file_recall(expected_basenames: set[str], generated_basenames: set[str]) -> float:
    """Recall = correctly-generated files / total expected files.

    Vacuously 1.0 when nothing was expected.
    """
    if not expected_basenames:
        return 1.0
    hits = expected_basenames & generated_basenames
    return len(hits) / len(expected_basenames)


def compute_f1(precision: float, recall: float) -> float:
    """Harmonic mean. Returns 0.0 when both are zero."""
    if precision <= 0 and recall <= 0:
        return 0.0
    if precision + recall == 0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def compute_contains_score(
    generated_files: dict[str, str],
    expected_contains: dict[str, list[str]],
) -> tuple[float, list[dict]]:
    """Score how many (file, substring) expectations were met.

    Returns `(fraction_hit, misses)` where misses is a list of
    `{"file": basename, "missing_substrings": [...]}` for reporting.

    File lookup is by basename; the generator's full path is matched
    against the expected file's basename.
    """
    if not expected_contains:
        return 1.0, []

    # Index the generator's output by basename for fast lookup.
    gen_by_basename: dict[str, str] = {}
    for path, content in generated_files.items():
        bn = _basename(path)
        # Later entries don't overwrite — first occurrence wins (stable).
        gen_by_basename.setdefault(bn, content)

    total_checks = 0
    hits = 0
    misses: list[dict] = []

    for expected_path, substrings in expected_contains.items():
        if not substrings:
            continue
        bn = _basename(expected_path)
        content = gen_by_basename.get(bn)
        missing_for_file: list[str] = []
        for sub in substrings:
            total_checks += 1
            if content is not None and sub in content:
                hits += 1
            else:
                missing_for_file.append(sub)
        if missing_for_file:
            misses.append({"file": bn, "missing_substrings": missing_for_file})

    fraction = (hits / total_checks) if total_checks > 0 else 1.0
    return fraction, misses


def compute_forbidden_violations(
    generated_files: dict[str, str],
    forbidden_files: list[str],
) -> list[str]:
    """Return basenames of forbidden files that appeared in the generated set."""
    if not forbidden_files:
        return []
    forbidden_set = {_basename(p) for p in forbidden_files if p}
    generated_basenames = {_basename(p) for p in generated_files.keys()}
    return sorted(forbidden_set & generated_basenames)


# ──────────────────────────────────────────────────────────────────────────────
# Per-case scoring
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CaseScore:
    case_id: str
    parse_success: bool
    file_precision: float
    file_recall: float
    file_f1: float
    contains_score: float
    forbidden_violations: list[str] = field(default_factory=list)
    contains_misses: list[dict] = field(default_factory=list)
    expected_basenames: set[str] = field(default_factory=set)
    generated_basenames: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "case_id":              self.case_id,
            "parse_success":        self.parse_success,
            "file_precision":       round(self.file_precision, 4),
            "file_recall":          round(self.file_recall, 4),
            "file_f1":              round(self.file_f1, 4),
            "contains_score":       round(self.contains_score, 4),
            "forbidden_violations": self.forbidden_violations,
            "contains_misses":      self.contains_misses,
            "expected_basenames":   sorted(self.expected_basenames),
            "generated_basenames":  sorted(self.generated_basenames),
        }


def score_case(
    case: dict,
    generated_files: dict[str, str],
    *,
    parse_success: bool = True,
) -> CaseScore:
    """Score a single gold case against a generator output.

    `case` is the dict loaded from `code_change_gold.jsonl`.
    `generated_files` is `{path: content}` — the result of parsing the
    agent's raw output via `parse_files_from_output`.
    `parse_success` reflects whether the agent emitted parseable output at
    all (if False, all file-level metrics will score 0 for this case).
    """
    expected_paths = list(case.get("expected_files_modified") or []) + list(
        case.get("expected_files_new") or []
    )
    expected_basenames = {_basename(p) for p in expected_paths if p}
    generated_basenames = {_basename(p) for p in generated_files.keys() if p}

    if not parse_success:
        # Generator failed to emit parseable output. Everything scores 0.
        return CaseScore(
            case_id=case.get("id", "?"),
            parse_success=False,
            file_precision=0.0,
            file_recall=0.0,
            file_f1=0.0,
            contains_score=0.0,
            forbidden_violations=[],
            contains_misses=[],
            expected_basenames=expected_basenames,
            generated_basenames=set(),
        )

    precision = compute_file_precision(expected_basenames, generated_basenames)
    recall    = compute_file_recall(expected_basenames, generated_basenames)
    f1        = compute_f1(precision, recall)
    contains_score, contains_misses = compute_contains_score(
        generated_files, case.get("expected_contains") or {},
    )
    forbidden = compute_forbidden_violations(
        generated_files, case.get("forbidden_files") or [],
    )

    return CaseScore(
        case_id=case.get("id", "?"),
        parse_success=True,
        file_precision=precision,
        file_recall=recall,
        file_f1=f1,
        contains_score=contains_score,
        forbidden_violations=forbidden,
        contains_misses=contains_misses,
        expected_basenames=expected_basenames,
        generated_basenames=generated_basenames,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AggregateScore:
    n_cases: int
    parse_success_rate: float
    macro_precision: float
    macro_recall: float
    macro_f1: float
    macro_contains_score: float
    total_forbidden_violations: int

    def to_dict(self) -> dict:
        return {
            "n_cases":                    self.n_cases,
            "parse_success_rate":         round(self.parse_success_rate, 4),
            "macro_precision":            round(self.macro_precision, 4),
            "macro_recall":               round(self.macro_recall, 4),
            "macro_f1":                   round(self.macro_f1, 4),
            "macro_contains_score":       round(self.macro_contains_score, 4),
            "total_forbidden_violations": self.total_forbidden_violations,
        }


def aggregate(case_scores: list[CaseScore]) -> AggregateScore:
    """Macro-average across cases. Empty input yields all-zero scores."""
    n = len(case_scores)
    if n == 0:
        return AggregateScore(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)

    parse_successes = sum(1 for cs in case_scores if cs.parse_success)
    return AggregateScore(
        n_cases=n,
        parse_success_rate=parse_successes / n,
        macro_precision=sum(cs.file_precision for cs in case_scores) / n,
        macro_recall=sum(cs.file_recall for cs in case_scores) / n,
        macro_f1=sum(cs.file_f1 for cs in case_scores) / n,
        macro_contains_score=sum(cs.contains_score for cs in case_scores) / n,
        total_forbidden_violations=sum(len(cs.forbidden_violations) for cs in case_scores),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Baseline comparison (used by the runner for CI gating)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RegressionCheck:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def check_regression(
    current: AggregateScore,
    baseline: dict,
    *,
    tolerance_pp: float = 2.0,
) -> RegressionCheck:
    """Compare current aggregate against a baseline snapshot.

    Regression criteria (any one fails the check):
      - macro_f1 dropped by more than tolerance_pp (default 2.0 percentage points)
      - macro_contains_score dropped by more than tolerance_pp
      - total_forbidden_violations went up (should stay at 0)

    parse_success_rate + precision/recall are reported but not hard-gated
    here since f1 + contains cover them. Reviewers can raise tolerance
    to 0 to gate on every dimension.

    `baseline` is expected to be the dict emitted by `AggregateScore.to_dict()`.
    """
    reasons: list[str] = []
    tol = tolerance_pp / 100.0

    baseline_f1 = float(baseline.get("macro_f1", 0.0))
    if current.macro_f1 + tol < baseline_f1:
        reasons.append(
            f"macro_f1 regressed: {current.macro_f1:.4f} < baseline {baseline_f1:.4f} "
            f"(tolerance {tolerance_pp}pp)"
        )

    baseline_contains = float(baseline.get("macro_contains_score", 0.0))
    if current.macro_contains_score + tol < baseline_contains:
        reasons.append(
            f"macro_contains_score regressed: {current.macro_contains_score:.4f} < "
            f"baseline {baseline_contains:.4f} (tolerance {tolerance_pp}pp)"
        )

    baseline_forbidden = int(baseline.get("total_forbidden_violations", 0))
    if current.total_forbidden_violations > baseline_forbidden:
        reasons.append(
            f"forbidden_violations went up: {current.total_forbidden_violations} > "
            f"baseline {baseline_forbidden}"
        )

    return RegressionCheck(passed=not reasons, reasons=reasons)
