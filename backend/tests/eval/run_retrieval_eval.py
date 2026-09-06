# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Retrieval eval runner — Slice 2 of docs/platform-enhancement-backlog.md.

Loads a gold set (`retrieval_gold.jsonl`), runs each case through the live
`hybrid_retrieve` pipeline, and computes four metrics:
  - recall@5, recall@10  — fraction of expected specs matched within top-k
  - MRR                  — mean reciprocal rank of first matching chunk
  - citation_coverage    — fraction of cases with ≥1 match in top-10

Metrics are written to `eval_reports/retrieval_<timestamp>.json`. On the first
run (when `baseline.json` is absent) the current metrics are also written to
`baseline.json`. Subsequent runs read `baseline.json` for regression comparison
(the comparison itself lives in `test_retrieval_regression.py`).

Gold-set schema (one JSON object per line in `retrieval_gold.jsonl`):
    {
      "id": "r001",
      "query": "How does X work?",
      "expected": [
        {
          "source_file_matches": "substring-or-pattern",
          "content_contains_any": ["phrase A", "phrase B"]
        },
        ...
      ],
      "category_hint": "payment_initiation",
      "notes": "free-text"
    }

Design note: expected chunks are keyed by source_file pattern + content phrases
(NOT by UUID) so the gold set survives re-ingestion, re-embedding, and
chunk-size changes without needing regeneration.

Run:
    cd backend && python tests/eval/run_retrieval_eval.py
or via Makefile:
    make eval-retrieval
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

HERE = Path(__file__).resolve().parent
GOLD_PATH = HERE / "retrieval_gold.jsonl"
BASELINE_PATH = HERE / "baseline.json"
REPORTS_DIR = HERE / "eval_reports"


# ── Gold-set loader ──────────────────────────────────────────────────────────

def load_gold(path: Path = GOLD_PATH) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Gold set not found at {path}")
    cases = []
    for line_no, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"Malformed JSON at line {line_no}: {e}") from e
        if "id" not in case or "query" not in case or "expected" not in case:
            raise ValueError(f"Case at line {line_no} missing required keys")
        cases.append(case)
    return cases


# ── Match scoring ────────────────────────────────────────────────────────────

def _chunk_matches_spec(chunk: dict, spec: dict) -> bool:
    """Return True iff chunk satisfies an expected-spec."""
    source = (chunk.get("source_file") or "").lower()
    content = (chunk.get("content") or "").lower()

    source_pat = spec.get("source_file_matches", "").lower()
    if source_pat and source_pat not in source:
        return False

    required_any = spec.get("content_contains_any", [])
    if required_any:
        if not any(phrase.lower() in content for phrase in required_any):
            return False

    required_all = spec.get("content_contains_all", [])
    if required_all:
        if not all(phrase.lower() in content for phrase in required_all):
            return False

    return True


def _first_match_rank(retrieved: list[dict], spec: dict) -> int | None:
    """1-indexed rank of first chunk in retrieved list matching spec, or None."""
    for i, chunk in enumerate(retrieved, start=1):
        if _chunk_matches_spec(chunk, spec):
            return i
    return None


# ── Case scoring ─────────────────────────────────────────────────────────────

def score_case(case: dict, retrieved: list[dict]) -> dict:
    """Per-case metrics given a retrieved top-k list."""
    expected = case["expected"]
    if not expected:
        return {"id": case["id"], "recall_at_5": 0.0, "recall_at_10": 0.0,
                "reciprocal_rank": 0.0, "covered": False,
                "match_ranks": []}

    ranks: list[int | None] = [_first_match_rank(retrieved, spec) for spec in expected]

    matched_at_5 = sum(1 for r in ranks if r is not None and r <= 5)
    matched_at_10 = sum(1 for r in ranks if r is not None and r <= 10)

    # MRR uses the BEST (lowest) rank across expected specs for this case.
    valid_ranks = [r for r in ranks if r is not None]
    best_rank = min(valid_ranks) if valid_ranks else None
    reciprocal = 1.0 / best_rank if best_rank else 0.0

    return {
        "id": case["id"],
        "recall_at_5": matched_at_5 / len(expected),
        "recall_at_10": matched_at_10 / len(expected),
        "reciprocal_rank": reciprocal,
        "covered": any(r is not None and r <= 10 for r in ranks),
        "match_ranks": ranks,
    }


# ── Dataset-level aggregate ──────────────────────────────────────────────────

def aggregate(case_results: list[dict]) -> dict:
    n = len(case_results)
    if n == 0:
        return {"cases": 0, "recall_at_5": 0.0, "recall_at_10": 0.0,
                "mrr": 0.0, "citation_coverage": 0.0}
    return {
        "cases": n,
        "recall_at_5":       round(sum(r["recall_at_5"] for r in case_results) / n, 4),
        "recall_at_10":      round(sum(r["recall_at_10"] for r in case_results) / n, 4),
        "mrr":               round(sum(r["reciprocal_rank"] for r in case_results) / n, 4),
        "citation_coverage": round(sum(1 for r in case_results if r["covered"]) / n, 4),
    }


# ── Live retrieval (imported lazily; requires DB + Ollama) ───────────────────

def _run_retrieve(query: str, top_k: int):
    """Call hybrid_retrieve with a fresh DB session. Raises on infra failure."""
    from app.core.database import SessionLocal  # lazy import
    from app.rag.retrieval import retrieve

    db = SessionLocal()
    try:
        return retrieve(query, db, top_k=top_k)
    finally:
        db.close()


# ── Main entry ───────────────────────────────────────────────────────────────

def run(gold_path: Path = GOLD_PATH, top_k: int = 10, write_baseline: bool = True) -> dict:
    cases = load_gold(gold_path)
    logger.info("Loaded %d gold cases from %s", len(cases), gold_path)

    case_results: list[dict] = []
    infra_errors: list[dict] = []
    for case in cases:
        try:
            retrieved = _run_retrieve(case["query"], top_k=top_k)
        except Exception as e:
            logger.error("Retrieval failed for case %s: %s", case["id"], e)
            infra_errors.append({"id": case["id"], "error": str(e)})
            continue
        result = score_case(case, retrieved)
        case_results.append(result)
        logger.info("  %s: recall@5=%.2f recall@10=%.2f RR=%.2f covered=%s",
                    case["id"], result["recall_at_5"], result["recall_at_10"],
                    result["reciprocal_rank"], result["covered"])

    agg = aggregate(case_results)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "top_k": top_k,
        "gold_set_path": str(gold_path.name),
        "metrics": agg,
        "per_case": case_results,
        "infra_errors": infra_errors,
    }

    # Write timestamped report
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"retrieval_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2))
    logger.info("Wrote %s", report_path)

    # Emit a compact one-line summary to stdout for Makefile convenience
    print(
        f"recall@5={agg['recall_at_5']} "
        f"recall@10={agg['recall_at_10']} "
        f"mrr={agg['mrr']} "
        f"citation_coverage={agg['citation_coverage']} "
        f"(cases={agg['cases']}, infra_errors={len(infra_errors)})"
    )

    # First-run: write baseline
    if write_baseline and not BASELINE_PATH.exists() and not infra_errors:
        BASELINE_PATH.write_text(json.dumps({"metrics": agg, "captured_at": report["timestamp"]}, indent=2))
        logger.info("Wrote first baseline to %s", BASELINE_PATH)

    return report


if __name__ == "__main__":
    try:
        report = run()
    except Exception as e:
        logger.error("Eval run failed: %s", e)
        sys.exit(1)
    # Non-zero exit if infra errors prevented any case from running
    if report["metrics"]["cases"] == 0:
        sys.exit(2)
