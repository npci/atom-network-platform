# Retrieval

> **Verified at:** alembic head `0138_integration_exchange_query`, commit `e116cea`.
> Checked against `backend/app/rag/` (27 modules), in particular
> `hybrid_search.py`, `reranker.py`, `code_ingestion.py`.

Agents are grounded in two corpora: **ingested documents** and **the target
codebase**. Both are searched the same way; they differ in how they are chunked.

## Hybrid search

Neither dense nor sparse retrieval alone is good enough. Dense embeddings find
things worded differently from the query; BM25 finds exact identifiers and error
codes that embeddings blur together. The stack runs **both and fuses them**.

Fusion is **Reciprocal Rank Fusion** with `k = 60` (the value from Cormack et
al., not a tuned guess):

```
score(chunk) = Σ  1 / (60 + rank_in_that_ranking + 1)
```

RRF combines *rankings*, not scores, which is the property that matters here: a
pgvector cosine distance and a BM25 relevance score are not on comparable scales,
so any weighted sum of them needs tuning that would silently rot. Ranks need
none.

After fusion, multiple views of the same symbol are collapsed, so one heavily
chunked function cannot crowd out everything else.

## Reranking

An optional cross-encoder reranks the fused top-k. It is off unless enabled,
because it downloads a model on first use.

**It fails open, deliberately.** If the model cannot load, retrieval returns the
RRF ordering rather than failing the request. Degraded ranking is a worse answer;
a hard failure is no answer. Model initialisation is guarded by a lock so that
concurrent first-use in a multi-worker process loads once.

## Two ingestion paths

**Documents** are chunked hierarchically, so a chunk keeps the heading trail that
gives it meaning. A paragraph retrieved without knowing which section it came
from is frequently misleading.

**Code** is chunked by **tree-sitter**, along syntactic boundaries — a function,
a class — rather than by character count. A function split mid-body retrieves as
two fragments that each look plausible and neither of which compiles.

Code ingestion goes further than chunking:

| Component | What it adds |
|---|---|
| Symbol-graph extractors | Definition and reference edges — per language |
| LSP resolvers | Real symbol resolution, not text matching — per language |
| Doc/code linker | Ties a specification section to the code implementing it |
| Code summariser | Natural-language summaries, so prose queries match code |

Java, Python and TypeScript each have their own extractor and resolver. That
repetition is deliberate: symbol resolution is genuinely language-specific, and a
shared abstraction over three different type systems would be a fiction.

## Around the search

Retrieval is not one call. Queries are rewritten and analysed before searching;
results are diversified so near-duplicates do not fill the window, then
compressed to fit a context budget. Embeddings are cached, since re-embedding
unchanged text on every request is the easy waste in a stack like this.

## What a term count cannot see

Retrieval quality is not visible in any gate in this repository. The evaluation
layer scores generated *output*, not whether the right chunks were retrieved to
produce it. A corpus that has silently failed to ingest produces confident,
ungrounded answers — the failure looks like a model problem and is not one.

The first check when results look wrong is whether the corpus is populated and
the embedding model was actually pulled; the stack returns zero scores for
everything in that state, rather than erroring.

## Related

- Where retrieval sits in the system: [architecture](architecture.md)
- What consumes it: [workflow phases](workflow-phases.md)
- How output is scored afterwards: [evaluation gate](evaluation-gate.md)
