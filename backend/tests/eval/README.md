# Retrieval Eval Harness

**Slice 2 of `platform-enhancement-backlog.md`.** Measures recall@5, recall@10, MRR, and citation-coverage over a hand-curated gold set of PO-style queries. Used to capture a baseline and gate future changes on regression.

---

## Running

```bash
cd backend
make eval-retrieval        # from project root, or:
.venv/bin/python tests/eval/run_retrieval_eval.py
```

Prerequisites (same as running the platform):
- Postgres + pgvector (dev stack: `docker compose up -d postgres`)
- Ollama reachable with `nomic-embed-text` pulled
- Knowledge base ingested (first-time setup)

Output:
- One-line summary on stdout: `recall@5=0.67 recall@10=0.67 mrr=0.50 citation_coverage=1.0 ...`
- Timestamped report at `backend/tests/eval/eval_reports/retrieval_<ts>.json`
- On first successful run: `backend/tests/eval/baseline.json` is created. Commit this.

## Regression gate

```bash
cd backend && .venv/bin/pytest tests/eval/test_retrieval_regression.py
```

Fails the test if any of `{recall@5, recall@10, mrr, citation_coverage}` drops by more than **2 percentage points** from `baseline.json`. Tune via `EVAL_REGRESSION_THRESHOLD=0.01` env var.

Skips (rather than fails) when:
- `baseline.json` is absent (no comparison target yet)
- The DB or Ollama is unreachable (so CI without the stack can still run `pytest` without false failures)

---

## Gold-set schema (`retrieval_gold.jsonl`)

One JSON object per line. Blank/comment lines allowed.

```json
{
  "id": "r001",
  "query": "How does a network transaction flow end-to-end?",
  "expected": [
    {
      "source_file_matches": "UPI_Complete_Guide",
      "content_contains_any": ["PSP processes request", "the Authority routes"]
    }
  ],
  "category_hint": "payment_initiation",
  "notes": "Section 4 of the Complete Guide. Seed case."
}
```

### Why not expected_chunk_ids?

Chunks are UUIDs assigned at ingest time. If you re-ingest, re-embed, or change chunk size, all UUIDs churn and the gold set breaks. We instead match by `source_file` substring + content phrases — the semantic intent of "the correct chunk was retrieved" is preserved across ingestion changes.

### Match rules

A retrieved chunk **matches** an expected spec when *all* of:
- `source_file_matches` (case-insensitive substring) is in the chunk's `source_file`, AND
- at least one phrase from `content_contains_any` is in the chunk's `content`, AND
- all phrases in `content_contains_all` are in the chunk's `content`.

Match is resolved per-spec; a case may have multiple expected specs (all must match somewhere in the top-10 for `recall@10 = 1.0` on that case).

---

## Curation protocol

1. **Write the query first**, phrased as a PM/PO would ask ("How does X work?", "What are the constraints on Y?").
2. **Spot-check against current retrieval** — run the query via `hybrid_retrieve()` and pick 1–2 chunks that truly answer it. If zero chunks answer it, the query is out-of-scope for the current corpus — skip.
3. **Write the expected spec** using `source_file_matches` (unique path fragment) + `content_contains_any` (a phrase distinctive to that chunk, not a phrase likely to appear elsewhere).
4. **Add notes** — why this case matters, what section it tests, any gotchas.

Cases should span the 10 the network taxonomy buckets: `payment_initiation`, `mandate_recurring`, `authentication_security`, `lite_offline`, `limit_enhancement`, `dispute_grievance`, `kyc_verification`, `cross_border`, `credit_products`, `value_added_service`.

---

> ⚠️ **The corpus this gold set targets is no longer in the repository.**
> `knowledge_base/upi_product_docs/UPI_Complete_Guide.md` was removed in the
> 2026-08 exposure remediation — its provenance is unverified and it may contain
> The Authority/RBI circular text (see `docs/genericization/00-open-questions.md`, OQ-3).
> `retrieval_gold.jsonl` still matches on `source_file_matches: "UPI_Complete_Guide"`,
> so **the retrieval eval will return zero hits until an operator supplies a corpus
> and re-ingests it**. The gold set is deliberately retained rather than deleted:
> the cases are still valid, and re-pointing them at a replacement corpus is much
> less work than re-authoring them. `knowledge_base/**` is gitignored, so the
> corpus is operator-supplied by design from here on.

## Scale guidance

The backlog originally targeted **30 cases**. The corpus was **one file**
(`upi_product_docs/UPI_Complete_Guide.md`, 77 lines → ~10 chunks) — 30 cases would
have been highly redundant on it. Realistic targets:

| Corpus size | Recommended gold set size |
|---|---|
| 1 file (~10 chunks) | 5–10 cases (current) |
| ~100 docs (~1k chunks) | 30 cases |
| ~1000 docs (~10k chunks) | 50–100 cases, regenerate every ~3 months |

Revisit the gold set size when the knowledge base grows.

---

## Files

| File | Role |
|---|---|
| `retrieval_gold.jsonl` | Gold-set data (edit to add/remove cases) |
| `run_retrieval_eval.py` | CLI eval runner |
| `test_retrieval_regression.py` | pytest regression gate |
| `baseline.json` | Frozen baseline metrics (committed; regenerated only on intentional accuracy improvement) |
| `eval_reports/` | Per-run timestamped reports (not committed — gitignore candidate when git is initialized) |
| `README.md` | This file |
