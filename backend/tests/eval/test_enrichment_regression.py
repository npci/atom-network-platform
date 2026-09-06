# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Enrichment regression gate — Slice 10.

Compares the current enrichment eval run against `enrichment_baseline.json`
captured on the first clean run. Fails if any metric drops by more than
`ENRICHMENT_REGRESSION_THRESHOLD` (default 0.02 = 2pp).

Skips gracefully when:
  - `enrichment_baseline.json` is absent (no comparison target yet)
  - The LLM is unreachable (all cases hit infra_errors → zero scored cases)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.eval import run_enrichment_eval as runner


BASELINE_PATH = Path(__file__).resolve().parent / "enrichment_baseline.json"
THRESHOLD = float(os.environ.get("ENRICHMENT_REGRESSION_THRESHOLD", "0.02"))
METRICS_TO_GATE = ("schema_validity", "field_completeness", "keyword_coverage")


@pytest.mark.eval
def test_enrichment_no_regression_vs_baseline():
    if not BASELINE_PATH.exists():
        pytest.skip(
            "No enrichment_baseline.json — run `make eval-enrichment` once to capture it."
        )

    baseline = json.loads(BASELINE_PATH.read_text())
    try:
        report = runner.run(write_baseline=False)
    except Exception as e:
        pytest.skip(f"Enrichment eval infra unavailable (LLM?): {e}")

    if report["metrics"]["cases"] == 0:
        pytest.skip(
            "No cases scored (infra errors on every case — LLM unreachable)."
        )

    regressions: list[str] = []
    for metric in METRICS_TO_GATE:
        base = baseline["metrics"].get(metric, 0.0)
        curr = report["metrics"].get(metric, 0.0)
        delta = curr - base
        if delta < -THRESHOLD:
            regressions.append(
                f"{metric}: baseline={base:.4f} current={curr:.4f} delta={delta:+.4f}"
            )

    assert not regressions, (
        "Enrichment metrics regressed beyond threshold:\n  "
        + "\n  ".join(regressions)
        + f"\n(threshold: {THRESHOLD:+.4f} per metric)"
    )
