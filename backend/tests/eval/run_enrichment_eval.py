# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Enrichment eval runner — Slice 10 of docs/platform-enhancement-backlog.md.

Loads `enrichment_gold.jsonl`, invokes `enrichment.generate_enriched_story()`
per case, and computes three metrics via the pure validator:
  - schema_validity       — fraction of cases where all REQUIRED fields populated
  - field_completeness    — mean populated-fields-over-10 across cases
  - keyword_coverage      — mean required-keyword hit-rate across cases

Writes a timestamped report to `eval_reports/enrichment_<ts>.json` and, on
the first clean run, the summary metrics into `enrichment_baseline.json`
(mirrors the retrieval eval pattern).

Run:
    make eval-enrichment
or:
    cd backend && python -m tests.eval.run_enrichment_eval
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

HERE = Path(__file__).resolve().parent
GOLD_PATH = HERE / "enrichment_gold.jsonl"
BASELINE_PATH = HERE / "enrichment_baseline.json"
REPORTS_DIR = HERE / "eval_reports"


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
        if "id" not in case or "po_prompt" not in case:
            raise ValueError(f"Case at line {line_no} missing required keys")
        cases.append(case)
    return cases


def aggregate(case_results: list[dict]) -> dict:
    n = len(case_results)
    if n == 0:
        return {"cases": 0, "schema_validity": 0.0, "field_completeness": 0.0,
                "keyword_coverage": 0.0}
    schema_ok_count = sum(1 for r in case_results if r["report"]["schema_valid"])
    completeness_sum = sum(r["report"]["field_completeness"] for r in case_results)
    kw_sum = sum(r["report"]["keyword_coverage"] for r in case_results)
    return {
        "cases":              n,
        "schema_validity":    round(schema_ok_count / n, 4),
        "field_completeness": round(completeness_sum / n, 4),
        "keyword_coverage":   round(kw_sum / n, 4),
    }


async def _run_async(gold_path: Path, write_baseline: bool) -> dict:
    from app.agents.enrichment import generate_enriched_story
    from app.agents.enrichment_schema import validate

    cases = load_gold(gold_path)
    logger.info("Loaded %d enrichment gold cases from %s", len(cases), gold_path)

    case_results: list[dict] = []
    infra_errors: list[dict] = []

    for case in cases:
        try:
            story = await generate_enriched_story(case["po_prompt"])
        except Exception as e:
            logger.error("Generator failed for %s: %s", case["id"], e)
            infra_errors.append({"id": case["id"], "error": str(e)})
            continue

        report = validate(story, required_keywords=case.get("required_keywords"))
        case_results.append({
            "id": case["id"],
            "report": report,
            "story_field_count": len(story),
        })
        logger.info(
            "  %s: schema_valid=%s completeness=%.2f kw_coverage=%.2f",
            case["id"], report["schema_valid"],
            report["field_completeness"], report["keyword_coverage"],
        )

    agg = aggregate(case_results)
    final = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "gold_set":     gold_path.name,
        "metrics":      agg,
        "per_case":     case_results,
        "infra_errors": infra_errors,
    }

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"enrichment_{stamp}.json"
    report_path.write_text(json.dumps(final, indent=2))
    logger.info("Wrote %s", report_path)

    print(
        f"schema_validity={agg['schema_validity']} "
        f"field_completeness={agg['field_completeness']} "
        f"keyword_coverage={agg['keyword_coverage']} "
        f"(cases={agg['cases']}, infra_errors={len(infra_errors)})"
    )

    if write_baseline and not BASELINE_PATH.exists() and not infra_errors and agg["cases"] > 0:
        BASELINE_PATH.write_text(json.dumps({
            "metrics": agg, "captured_at": final["timestamp"],
        }, indent=2))
        logger.info("Wrote first enrichment baseline to %s", BASELINE_PATH)

    return final


def run(gold_path: Path = GOLD_PATH, write_baseline: bool = True) -> dict:
    return asyncio.run(_run_async(gold_path, write_baseline))


if __name__ == "__main__":
    try:
        report = run()
    except Exception as e:
        logger.error("Enrichment eval failed: %s", e)
        sys.exit(1)
    if report["metrics"]["cases"] == 0:
        sys.exit(2)
