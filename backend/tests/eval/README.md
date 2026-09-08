# Retrieval Eval Harness

Measures recall@5, recall@10, MRR, and citation-coverage over a hand-curated gold set of PO-style queries. Used to capture a baseline and gate future changes on regression.

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
      "source_file_matches": "Network_Complete_Guide",
      "content_contains_any": ["initiating participant", "the authority routes"]
    }
  ],
  "category_hint": "request_initiation",
  "notes": "Which section of your corpus this tests, and any gotchas."
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

Cases should span the taxonomy buckets your domain pack declares
(`feature_taxonomy`), so a gap in one bucket shows up as a gap rather than as a
slightly lower average.

## Gold sets are per-domain

A gold set is domain data. Every query in it, and every keyword it is scored on,
belongs to one industry — so grading a library-lending deployment against a
financial-network gold set measures nothing about that deployment. A gold set is
therefore resolved against the **active domain pack**:

```
<pack dir>/eval/<name>.jsonl     the active pack's own cases, if it ships them
tests/eval/<name>.jsonl          the platform's domain-neutral defaults
```

`tests/eval/gold_paths.py` performs the resolution and logs which set it chose.
The payments cases now live in `app/packs/network/eval/`; the sets in this
directory are deliberately domain-free.

**Write your own.** The neutral cases are structurally valid templates, not a
meaningful benchmark for your deployment — they exist so the harness runs and so
you have a shape to copy. Put yours in your pack's `eval/` directory.

`code_change_gold.jsonl` is the exception and is NOT pack-split: those cases
test a language capability (add a decorator, add a route, write a validator),
not a domain, so the same set is meaningful everywhere.

---

> ⚠️ **No corpus ships with the repository, so retrieval scores 0.0 until you supply one.**
> The retrieval cases were originally written against a single product guide that
> was withdrawn because its provenance could not be established and it may
> reproduce regulator circular text verbatim. `knowledge_base/**` is gitignored,
> so the corpus is operator-supplied by design. Both the neutral cases here and
> the payments cases in `app/packs/network/eval/` therefore return zero hits
> until you ingest a corpus and re-point `source_file_matches` at a document that
> exists in it. A clean 0.0 with no infra errors means "empty corpus", not
> "retrieval broke" — `test_retrieval_regression.py` skips rather than fails for
> exactly this reason.

## Scale guidance

The backlog originally targeted **30 cases**. The corpus was **one file**
(the product guide above, 77 lines → ~10 chunks) — 30 cases would
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
