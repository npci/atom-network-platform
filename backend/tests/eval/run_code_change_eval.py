# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code-change eval runner (Slice 25).

Scores pre-generated agent outputs against the `code_change_gold.jsonl`
gold set and reports metrics. Compares to baseline if present; exits
non-zero on regression.

Why pre-generated outputs instead of live-agent invocation?
  The code-change agent is a streaming LLM call that requires a fully-
  populated Code RAG index, real LLM credentials, and several seconds
  per case. Running it inside `make eval-code-change` would make eval
  expensive and flaky. Instead:

    1. Reviewer runs the agent on each gold case (manually or via a
       separate harness), saves the raw `<<FILE:...>>`-marker-delimited
       output as `<case_id>.txt` in an outputs directory.
    2. This runner parses those outputs and scores them — deterministic,
       cheap, CI-friendly.

Gold-set schema (one JSON object per line in `code_change_gold.jsonl`):
    {
      "id": "cc001-add-retry",
      "description": "...",
      "tech_spec": "...",
      "brd": "...",
      "expected_files_modified": ["NetworkSwitchService.java"],
      "expected_files_new": ["NewClass.java"],
      "expected_contains": {
        "NetworkSwitchService.java": ["@Retryable", "backoff"]
      },
      "forbidden_files": ["application.yml"],
      "notes": "free-text"
    }

Run (scoring an outputs directory):
    make eval-code-change OUTPUTS=/tmp/cc_outputs
  or:
    cd backend && python -m tests.eval.run_code_change_eval --outputs /tmp/cc_outputs

Run (empty outputs — baseline-refresh utility only):
    cd backend && python -m tests.eval.run_code_change_eval --refresh-baseline
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

HERE = Path(__file__).resolve().parent
GOLD_PATH = HERE / "code_change_gold.jsonl"
BASELINE_PATH = HERE / "code_change_baseline.json"
REPORTS_DIR = HERE / "eval_reports"
REPORTS_DIR.mkdir(exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Lightweight helpers — reuses the agent's own parser for consistency.
# ──────────────────────────────────────────────────────────────────────────────

def _parse_agent_output(raw: str) -> tuple[dict[str, str], bool]:
    """Parse `<<FILE: path>> ... <<END_FILE>>` markers.

    Returns `({path: content}, parse_success)`. We delegate to the
    agent's own parser for consistency so the eval metric tracks the
    shipped parser's behaviour (including its quirks).
    """
    try:
        from app.agents.code_change import parse_files_from_output
    except Exception as e:  # pragma: no cover — import should never fail
        logger.warning("could not import parse_files_from_output: %s", e)
        return {}, False

    try:
        parsed = parse_files_from_output(raw)
    except Exception as e:
        logger.warning("parser raised on input: %s", e)
        return {}, False

    if not parsed:
        return {}, False

    # `parsed` is `[{"path": "...", "content": "..."}, ...]` — flatten.
    out: dict[str, str] = {}
    for entry in parsed:
        path = entry.get("path")
        content = entry.get("content", "")
        if path:
            out[path] = content

    return out, bool(out)


def _load_gold() -> list[dict]:
    if not GOLD_PATH.exists():
        raise FileNotFoundError(f"gold set not found: {GOLD_PATH}")
    cases: list[dict] = []
    for ln, line in enumerate(GOLD_PATH.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{GOLD_PATH}:{ln} not valid JSON: {e}") from e
    return cases


def _load_outputs(outputs_dir: Path, case_ids: list[str]) -> dict[str, str]:
    """For each case_id, read `<case_id>.txt` if present. Missing files
    yield empty-string (parse will fail → parse_success=False in score)."""
    out: dict[str, str] = {}
    for cid in case_ids:
        p = outputs_dir / f"{cid}.txt"
        if p.exists():
            out[cid] = p.read_text()
        else:
            logger.warning("no output file for case %s at %s — case will score 0", cid, p)
            out[cid] = ""
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def run(
    *,
    outputs_dir: Path | None,
    refresh_baseline: bool = False,
    tolerance_pp: float = 2.0,
) -> int:
    """Entry point. Returns a Unix exit code — 0 success, 1 regression."""
    from tests.eval.code_change_metrics import (
        aggregate, check_regression, score_case,
    )

    cases = _load_gold()
    logger.info("loaded %d gold cases from %s", len(cases), GOLD_PATH)

    case_ids = [c.get("id", f"unnamed-{i}") for i, c in enumerate(cases)]

    if outputs_dir is None:
        outputs: dict[str, str] = {cid: "" for cid in case_ids}
        logger.info("no outputs directory supplied — scoring empty outputs")
    else:
        if not outputs_dir.exists():
            logger.error("outputs directory does not exist: %s", outputs_dir)
            return 2
        outputs = _load_outputs(outputs_dir, case_ids)

    # Score each case
    case_scores = []
    for case in cases:
        cid = case.get("id", "?")
        raw = outputs.get(cid, "")
        parsed_files, parse_ok = _parse_agent_output(raw)
        score = score_case(case, parsed_files, parse_success=parse_ok)
        case_scores.append(score)

        # Per-case one-liner
        logger.info(
            "  %s  parse=%s  P=%.3f R=%.3f F1=%.3f  contains=%.3f  forbidden=%d",
            cid, parse_ok, score.file_precision, score.file_recall,
            score.file_f1, score.contains_score, len(score.forbidden_violations),
        )
        if score.forbidden_violations:
            logger.warning("    FORBIDDEN VIOLATIONS: %s", score.forbidden_violations)
        for miss in score.contains_misses:
            logger.info("    contains-miss: %s missing %s", miss["file"], miss["missing_substrings"])

    agg = aggregate(case_scores)
    logger.info("")
    logger.info("=== aggregate ===")
    logger.info("  n_cases:                    %d", agg.n_cases)
    logger.info("  parse_success_rate:         %.3f", agg.parse_success_rate)
    logger.info("  macro_precision:            %.3f", agg.macro_precision)
    logger.info("  macro_recall:               %.3f", agg.macro_recall)
    logger.info("  macro_f1:                   %.3f", agg.macro_f1)
    logger.info("  macro_contains_score:       %.3f", agg.macro_contains_score)
    logger.info("  total_forbidden_violations: %d", agg.total_forbidden_violations)

    # Persist this run to a timestamped report + optionally refresh the baseline.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORTS_DIR / f"code_change_{ts}.json"
    report_payload = {
        "timestamp_utc":   ts,
        "n_cases":         agg.n_cases,
        "outputs_dir":     str(outputs_dir) if outputs_dir else None,
        "aggregate":       agg.to_dict(),
        "per_case":        [cs.to_dict() for cs in case_scores],
    }
    report_path.write_text(json.dumps(report_payload, indent=2))
    logger.info("wrote per-run report: %s", report_path)

    if refresh_baseline or not BASELINE_PATH.exists():
        BASELINE_PATH.write_text(json.dumps(agg.to_dict(), indent=2))
        logger.info("baseline %s: %s", "refreshed" if refresh_baseline else "created", BASELINE_PATH)
        return 0

    baseline = json.loads(BASELINE_PATH.read_text())
    check = check_regression(agg, baseline, tolerance_pp=tolerance_pp)
    if check.passed:
        logger.info("regression check PASSED (tolerance=%spp)", tolerance_pp)
        return 0

    logger.error("regression check FAILED:")
    for reason in check.reasons:
        logger.error("  - %s", reason)
    return 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Score code-change agent outputs against the gold set.")
    ap.add_argument("--outputs", type=Path, default=None,
                    help="Directory containing <case_id>.txt files. Omit to run with empty outputs.")
    ap.add_argument("--refresh-baseline", action="store_true",
                    help="Overwrite baseline with current run's aggregate (use after intentional improvements).")
    ap.add_argument("--tolerance-pp", type=float, default=2.0,
                    help="Regression tolerance in percentage points (default 2.0).")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    return run(
        outputs_dir=args.outputs,
        refresh_baseline=args.refresh_baseline,
        tolerance_pp=args.tolerance_pp,
    )


if __name__ == "__main__":
    raise SystemExit(main())
