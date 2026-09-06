# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Regression gate for retrieval metrics.

This test:
  1. Loads `baseline.json` (captured on first `make eval-retrieval` run).
  2. Invokes `run_retrieval_eval.run()` to compute current metrics.
  3. Fails if any metric drops by more than `EVAL_REGRESSION_THRESHOLD`
     (default 0.02 = 2 percentage points) from baseline.

Skips gracefully when:
  - `baseline.json` is absent (no baseline to compare against yet)
  - The DB / Ollama stack is unreachable (retrieval raises → all infra_errors)
  - The embedding model is missing, which does NOT raise: `embed_query` fails
    open to a zero vector, so every case "scores" a real 0.0 and the run looks
    like a total regression rather than an outage (see the counter check below)
  - The knowledge base is empty, which also scores a clean 0.0 everywhere

**This test cannot currently gate anything in this repo.** `retrieval_gold.jsonl`
asserts against `UPI_Complete_Guide`, deleted on purpose (commit 647b246) because
the genericisation audit flagged it as possibly-verbatim the Authority circular text
(OQ-3, "the single most consequential unverified item in the audit"). Restoring
that corpus to turn this green would reintroduce precisely what a legal review
removed. Making this a real gate again means writing NEW gold cases against a
corpus that legitimately ships — the OQ-3 resolution says adopters bring their
own corpus, so this is deployment-side, not repo-side.

The eval is also runnable directly (`python tests/eval/run_retrieval_eval.py`)
for ad-hoc use; this test exists so CI can gate merges on the same numbers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.eval import run_retrieval_eval as runner


BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"
THRESHOLD = float(os.environ.get("EVAL_REGRESSION_THRESHOLD", "0.02"))
METRICS_TO_GATE = ("recall_at_5", "recall_at_10", "mrr", "citation_coverage")


@pytest.mark.eval
def test_retrieval_no_regression_vs_baseline():
    if not BASELINE_PATH.exists():
        pytest.skip(
            "No baseline.json yet — run `make eval-retrieval` once to capture it."
        )

    baseline = json.loads(BASELINE_PATH.read_text())

    # An EMPTY corpus scores 0.0 on every metric with zero infra errors — which
    # is indistinguishable from "retrieval broke catastrophically" unless we say
    # so here. This is the normal state of a fresh clone, and of this repo:
    # `retrieval_gold.jsonl` asserts against `UPI_Complete_Guide`, which was
    # DELETED on purpose (commit 647b246) because the genericisation audit
    # flagged it as possibly-verbatim the Authority circular text (OQ-3). Re-ingesting it
    # to make this test green would reintroduce exactly what a legal review
    # removed, so the gold cases — not the corpus — are what needs replacing.
    from app.core.database import SessionLocal
    from sqlalchemy import text as _sql

    # The count itself can raise, and used to take the test down with it: in CI
    # Postgres is up but never migrated, so `document_chunks` does not exist and
    # this query throws ProgrammingError OUTSIDE any handler. The test then
    # FAILED where every other unavailable-infra path deliberately skips —
    # reporting a retrieval regression when the truth was "there is no schema".
    # An unreachable DB lands here too.
    try:
        with SessionLocal() as _s:
            corpus = _s.execute(_sql("SELECT count(*) FROM document_chunks")).scalar() or 0
    except Exception as e:
        pytest.skip(f"knowledge-base schema unavailable ({type(e).__name__}) — nothing to evaluate")
    if corpus == 0:
        pytest.skip(
            "knowledge base is empty (0 document_chunks) — retrieval metrics would "
            "read 0.0 for every case and look like a total regression. Ingest a "
            "corpus first; note the shipped gold cases target a document that was "
            "deliberately removed (see this test's comments)."
        )

    from app.rag import embeddings as _emb
    before = _emb.embed_failure_stats()["zero_vector_total"]

    try:
        report = runner.run(write_baseline=False)
    except Exception as e:
        pytest.skip(f"Eval infra unavailable (DB/Ollama?): {e}")

    if report["metrics"]["cases"] == 0:
        pytest.skip(
            "No cases scored (likely infra errors on every case — DB/Ollama down). "
            "See the written report for details."
        )

    # A missing embedding model (e.g. `nomic-embed-text` never pulled) returns
    # HTTP 404, which embed_query converts into a zero vector rather than an
    # exception. Dense search then matches nothing and every metric reads 0.0 —
    # indistinguishable from a catastrophic regression unless we check here.
    zero_vecs = _emb.embed_failure_stats()["zero_vector_total"] - before
    if zero_vecs:
        pytest.skip(
            f"Embedding backend degraded — {zero_vecs} query embed(s) fell back to a "
            "zero vector, so retrieval metrics are meaningless. Pull the embedding "
            "model (see EMBEDDING_MODEL) and re-run."
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
        "Retrieval metrics regressed beyond threshold:\n  "
        + "\n  ".join(regressions)
        + f"\n(threshold: {THRESHOLD:+.4f} per-metric; override via EVAL_REGRESSION_THRESHOLD)"
    )
