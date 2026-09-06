# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""RAG retrieval facade.

`retrieve()` is the public API used throughout the agents. It delegates to
hybrid_search (dense + BM25 + RRF) and, when `USE_QUERY_UNDERSTANDING` is on
(Slice 5), runs a multi-pass enriched retrieval: original query + HyDE
hypothetical answer + sub-questions, fused via RRF across passes.

`build_context()` formats retrieved chunks for pasting into an LLM prompt.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document_chunk import DocCategory
from app.rag.hybrid_search import RRF_K, _dedup_by_parent_symbol, hybrid_retrieve

logger = logging.getLogger(__name__)


def retrieve(
    query: str,
    db: Session,
    top_k: int = 6,
    categories: list[DocCategory] | None = None,
    min_score: float = 0.0,   # kept for API compatibility — no longer a hard gate
    use_reranker: bool | None = None,
) -> list[dict]:
    """Return top_k most relevant chunks via hybrid search (+ optional multi-pass).

    Args:
        query:      Natural-language search string.
        db:         SQLAlchemy session.
        top_k:      Maximum number of chunks to return.
        categories: Optional filter — only search within these doc categories.
        min_score:  (Kept for backwards compat) Legacy cosine-similarity gate.
                    Hybrid scoring is RRF (different scale); we now apply a
                    token-overlap post-filter inside hybrid_retrieve instead.
                    If you need a hard score floor, pass >0 here.

    Returns:
        List of dicts: id, source_file, doc_category, content, chunk_index,
        score (RRF-fused), and — when available — dense_score / bm25_score /
        dense_rank / bm25_rank for debugging.
    """
    # Slice 6 — When the reranker is on, over-sample the retrieval pool so the
    # cross-encoder has enough candidates to reorder. The reranker truncates
    # back to `top_k` at the end. A caller can force it on for one call (e.g. the
    # code-gen agent) without flipping the global default; None → configured setting.
    rerank_on = settings.use_reranker if use_reranker is None else use_reranker
    retrieval_k = top_k * 5 if rerank_on else top_k

    if settings.use_query_understanding:
        results = _multi_pass_retrieve(query, db, top_k=retrieval_k, categories=categories)
    else:
        results = hybrid_retrieve(query, db, top_k=retrieval_k, categories=categories)

    # Sub-slice 20b — When the graph-retrieval flag is on, run graph_retrieve
    # as an extra pass and RRF-fuse it with the primary ranking. Fail-open:
    # any exception or empty graph result is a no-op (primary ranking wins).
    if settings.use_graph_retrieval:
        try:
            from app.rag import graph_retriever
            graph_results = graph_retriever.graph_retrieve(
                query, db, top_k=retrieval_k,
            )
        except Exception as e:
            logger.warning("graph_retrieve failed, skipping graph fusion: %s", e)
            graph_results = []
        if graph_results:
            logger.info(
                "Fusing graph pass (%d chunks) with primary (%d chunks)",
                len(graph_results), len(results),
            )
            results = _rrf_fuse_passes([results, graph_results], top_k=retrieval_k)

    # Phase 2.5 — when MMR is on, ask the reranker / slicer for a wider
    # pool than `top_k` so the diversifier has room to operate. The cut to
    # `top_k` happens inside `_diversify_with_mmr`.
    use_mmr = bool(getattr(settings, "use_mmr_diversification", False))
    pre_mmr_k = top_k * 2 if use_mmr else top_k

    if rerank_on and len(results) > 1:
        # Lazy import so PyTorch is only loaded when reranking is actually requested.
        from app.rag import reranker
        results = reranker.rerank(query, results, top_k=pre_mmr_k)
    else:
        results = results[:pre_mmr_k]

    if use_mmr and len(results) > 1:
        results = _diversify_with_mmr(query, results, db, top_k=top_k)
    else:
        results = results[:top_k]

    if min_score > 0:
        results = [r for r in results if r["score"] >= min_score]

    # Phase 7.3 — augment the final top-K with 1-hop graph neighbours.
    # The neighbours are appended to `results` with `ctx_only=True` so
    # downstream context-builders can render them in a "Referenced
    # symbols" section without letting them displace primary matches.
    # Off by default — operators opt in once eval shows the lift.
    if (
        bool(getattr(settings, "use_graph_one_hop_expansion", False))
        and results
    ):
        try:
            extras = _expand_top_k_neighbors(results, db)
            if extras:
                results = list(results) + extras
        except Exception as e:
            logger.warning("graph 1-hop expansion failed (%s) — keeping primary results", e)

    return results


# ── Slice 5 — Multi-pass retrieval orchestration ──────────────────────────────

def _multi_pass_retrieve(
    query: str,
    db: Session,
    top_k: int,
    categories: list[DocCategory] | None,
) -> list[dict]:
    """Enrich the query, run hybrid_retrieve for each variant, fuse via RRF.

    Fail-open: if enrichment produces no variants, falls back to single pass.
    If a single variant's retrieval throws, that pass is skipped (log) — other
    variants still contribute.

    Phase 2.1 — when `settings.use_parallel_multi_pass` is on, the per-variant
    `hybrid_retrieve` calls fan out via a ThreadPoolExecutor. Each worker
    allocates its own SQLAlchemy session so the SessionLocal handed to this
    function isn't shared across threads (SQLAlchemy 2.0 Session is not
    thread-safe). Wall-time for N variants drops from N*T to ~T.
    """
    # Lazy import to keep startup footprint minimal when the feature is off.
    from app.rag import query_understanding

    enriched = query_understanding.enrich_sync(query)
    variants = enriched.variants_for_retrieval()
    if len(variants) <= 1:
        logger.debug("Multi-pass: only the raw query (enrichment yielded nothing extra)")
        return hybrid_retrieve(query, db, top_k=top_k, categories=categories)

    logger.info(
        "Multi-pass retrieve: %d variants (raw + %d enriched) top_k=%d",
        len(variants), len(variants) - 1, top_k,
    )

    # Over-sample per variant so RRF has enough material to fuse.
    per_pass_k = max(top_k * 2, 10)

    if bool(getattr(settings, "use_parallel_multi_pass", True)):
        rankings = _run_variants_parallel(variants, per_pass_k, categories)
    else:
        rankings = _run_variants_serial(variants, db, per_pass_k, categories)

    if not rankings:
        # All variants threw. Final fallback: try the raw query directly.
        logger.warning("All multi-pass variants failed, falling back to raw query")
        return hybrid_retrieve(query, db, top_k=top_k, categories=categories)

    return _rrf_fuse_passes(rankings, top_k=top_k)


def _run_variants_serial(
    variants: list[str],
    db: Session,
    per_pass_k: int,
    categories: list[DocCategory] | None,
) -> list[list[dict]]:
    """Legacy sequential variant loop. Each variant reuses the caller's
    session — safe because there's only one thread at a time."""
    rankings: list[list[dict]] = []
    for i, variant in enumerate(variants):
        try:
            ranked = hybrid_retrieve(variant, db, top_k=per_pass_k, categories=categories)
        except Exception as e:
            logger.warning(
                "Multi-pass variant %d (%r) failed, skipping: %s",
                i, variant[:60], e,
            )
            continue
        rankings.append(ranked)
    return rankings


def _run_variants_parallel(
    variants: list[str],
    per_pass_k: int,
    categories: list[DocCategory] | None,
) -> list[list[dict]]:
    """Phase 2.1 — parallel variant execution.

    Each task pulls its own SessionLocal() so SQLAlchemy 2.0 Session
    thread-safety holds. Results are merged in the original variant order
    (RRF fusion is order-sensitive only via rank, not via input position,
    but keeping order stable preserves identical fused output regardless
    of completion timing).

    Per-variant exceptions are swallowed — that variant is dropped from the
    output (matching the serial loop's contract).
    """
    try:
        from app.core.database import SessionLocal
    except Exception as e:
        logger.warning(
            "Phase 2.1: SessionLocal unavailable (%s), falling back to serial loop", e,
        )
        return []  # caller will see empty rankings → final raw-query fallback

    try:
        max_workers = int(getattr(settings, "multi_pass_max_workers", 4) or 4)
    except Exception:
        max_workers = 4
    max_workers = max(1, min(max_workers, len(variants)))

    def _one(variant: str) -> list[dict]:
        # Each task owns its own session — SQLAlchemy 2.0 Session is NOT
        # thread-safe. Always close it, success or fail.
        local_db = SessionLocal()
        try:
            return hybrid_retrieve(
                variant, local_db, top_k=per_pass_k, categories=categories,
            )
        finally:
            try:
                local_db.close()
            except Exception:
                pass

    rankings: list[list[dict] | None] = [None] * len(variants)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rag-mp") as pool:
        future_to_idx = {pool.submit(_one, v): i for i, v in enumerate(variants)}
        for fut in as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                rankings[i] = fut.result()
            except Exception as e:
                logger.warning(
                    "Multi-pass variant %d (%r) failed in parallel exec: %s",
                    i, variants[i][:60], e,
                )
                rankings[i] = None

    return [r for r in rankings if r is not None]


def _rrf_fuse_passes(rankings: list[list[dict]], top_k: int) -> list[dict]:
    """Fuse multiple ranking lists with Reciprocal Rank Fusion.

    Uses the same RRF_K (=60) as intra-pass fusion in hybrid_search for
    consistency. Each chunk's score across passes is summed.

    Preserves the Slice 4 parent_symbol_id dedup so we don't return multiple
    views of the same symbol.
    """
    fused_scores: dict[str, float] = defaultdict(float)
    first_seen: dict[str, dict] = {}

    for ranked in rankings:
        for rank, chunk in enumerate(ranked):
            chunk_id = chunk["id"]
            fused_scores[chunk_id] += 1.0 / (RRF_K + rank + 1)
            first_seen.setdefault(chunk_id, chunk)

    merged: list[dict] = []
    for chunk_id, score in fused_scores.items():
        row = dict(first_seen[chunk_id])
        row["score"] = round(score, 5)
        merged.append(row)

    merged.sort(key=lambda c: c["score"], reverse=True)
    merged = _dedup_by_parent_symbol(merged)
    return merged[:top_k]


def _expand_top_k_neighbors(
    results: list[dict],
    db: Session,
    *,
    max_hops: int = 1,
) -> list[dict]:
    """Phase 7.3 — fetch 1-hop CALLS/INHERITS/IMPLEMENTS neighbours of the
    final top-K and return them as `[ctx-only]` rows so the agent layer
    can decide whether to surface them in context.

    The neighbour rows carry `ctx_only=True` and `neighbor_of=<chunk_id>`
    so context-builders know to render them in a secondary section, and
    the token-budget code (Phase 0.3) can drop them first on overflow.

    Pure read — no mutation of input rows. Returns up to one neighbour
    per primary result by default; the SQL helper handles dedup.
    """
    seeds: list[str] = []
    for r in results:
        sym = (r.get("symbol_name") or "").strip()
        if sym and sym not in seeds:
            seeds.append(sym)
    if not seeds:
        return []

    try:
        from app.kg.sql_graph import expand_neighbors
    except Exception as e:
        logger.debug("graph 1-hop expansion: sql_graph unavailable (%s)", e)
        return []

    # Don't drown the primary results — cap the neighbour set at top_k
    # primary count. Eval can tune this later.
    cap = max(4, min(20, len(results)))
    try:
        rows = expand_neighbors(db, seeds, max_hops=max_hops, max_results=cap)
    except Exception as e:
        logger.warning("expand_neighbors failed: %s", e)
        return []

    primary_ids = {r.get("id") for r in results}
    extras: list[dict] = []
    for row in rows:
        if row.get("id") in primary_ids:
            continue
        extra = dict(row)
        extra["ctx_only"] = True
        extra["score"] = 0.0  # neighbours don't compete with primary RRF/rerank scores
        extras.append(extra)
    if extras:
        logger.debug(
            "Graph 1-hop expansion: %d primary chunks → %d neighbour chunks",
            len(results), len(extras),
        )
    return extras


def _diversify_with_mmr(
    query: str,
    results: list[dict],
    db: Session,
    *,
    top_k: int,
) -> list[dict]:
    """Phase 2.5 — apply MMR to the (already reranked) candidate set.

    Hydrates chunk embeddings on demand: most retrieval paths don't surface
    `embedding` to keep `_hydrate_chunks` cheap, so we issue one targeted
    SELECT against `document_chunks` for the final candidate set only.

    Numerically safe — on any exception (missing column, dim mismatch,
    embed_query failure, malformed pgvector text) we return
    `results[:top_k]` unchanged. MMR must not turn a correct result into
    a worse one because of an infrastructure failure.
    """
    if not results:
        return []
    try:
        from app.rag.diversify import mmr_select
        from app.rag.embeddings import embed_query

        ids = [r.get("id") for r in results if r.get("id") is not None]
        if not ids:
            return results[:top_k]

        # Hydrate embeddings for the final candidate set in one SQL round-trip.
        # Cast to ::text so pgvector returns the canonical "[a,b,c]" form
        # which `_coerce_embedding` parses without numpy.
        # SAVEPOINT-scoped: this is a fail-open pass (the except below returns the
        # rerank order unchanged), so a query failure must roll back only this
        # statement — otherwise it leaves the caller's session aborted and the
        # next unrelated write fails with InFailedSqlTransaction.
        with db.begin_nested():
            rows = db.execute(
                sql_text(
                    "SELECT id, embedding::text AS emb "
                    "FROM document_chunks "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": ids},
            ).all()
        emb_by_id = {str(r.id): r.emb for r in rows}

        enriched: list[dict] = []
        for r in results:
            row = dict(r)
            rid = r.get("id")
            if rid is not None and str(rid) in emb_by_id:
                row["embedding"] = emb_by_id[str(rid)]
            enriched.append(row)

        query_vec = embed_query(query)
        try:
            lam = float(getattr(settings, "mmr_lambda", 0.5) or 0.5)
        except (TypeError, ValueError):
            lam = 0.5

        diversified = mmr_select(enriched, query_vec, k=top_k, lambda_=lam)

        # Strip the `embedding` field — it's heavy and only needed inside MMR.
        for row in diversified:
            row.pop("embedding", None)

        return diversified
    except Exception as e:
        logger.warning("MMR diversification failed (%s) — using rerank order", e)
        return results[:top_k]


def format_sources(chunks: list[dict]) -> tuple[str, list[dict]]:
    """Slice 9 — Produce a numbered-source context + a parallel source table.

    Returns:
        (context_text, sources)
        context_text: string with each chunk prefixed `[N] [{category} | {file}]\\n{body}`.
        sources: `[{"n": 1, "source_file": "...", "doc_category": "...",
                    "title_breadcrumb": "..."}]` — safe to surface to the UI
                    for hover tooltips or a Sources section.

    The agent prompt instructs the LLM to cite claims inline via `[1]`, `[2]`,
    etc.; `citation_validator` then parses those markers post-generation.
    Unlike `build_context()` this does NOT apply a token budget — callers
    should pair it with a budget check if needed. It also does NOT apply
    compression (citations need stable anchor text).
    """
    parts = []
    sources: list[dict] = []
    for i, chunk in enumerate(chunks, start=1):
        header = f"[{i}] [{chunk.get('doc_category', '?')} | {chunk.get('source_file', '?')}]\n"
        body = (chunk.get("content") or "").strip()
        parts.append(f"{header}{body}\n")
        sources.append({
            "n":                i,
            "source_file":      chunk.get("source_file"),
            "doc_category":     chunk.get("doc_category"),
            "title_breadcrumb": chunk.get("title_breadcrumb"),  # Slice 7 field; may be None
        })
    context_text = "\n---\n".join(parts)
    return context_text, sources


def build_context(
    chunks: list[dict],
    max_tokens: int = 4000,
    *,
    query: str | None = None,
    model: str | None = None,
) -> str:
    """Assemble retrieved chunks into a single context string within a real
    token budget.

    Phase 0.5 — accounting now uses `app.core.tokens.count_tokens` against
    `model` (defaults to `app.core.llm.get_model()`). The legacy chars-per-
    token heuristic is the fallback when no tokeniser is available so
    flag-off behaviour is preserved.

    Slice 8 — when `settings.use_context_compression` is True AND a `query`
    is supplied, each chunk is first passed through a per-chunk LLM filter
    that keeps only sentences directly relevant to the query. Fail-open: any
    failure returns the original chunk unchanged. Callers that don't pass
    `query` get the legacy behavior (full chunk bodies), regardless of flag.
    """
    # Slice 8 — optional compression before budgeting.
    if settings.use_context_compression and query and chunks:
        # Lazy import so this module doesn't force-load the compressor when
        # the flag is off (it pulls in asyncio + the LLM client lazily).
        from app.rag import context_compressor
        chunks = context_compressor.compress_chunks_sync(query, chunks)

    # Phase 0.5 — resolve target model lazily so import-time cycles are
    # avoided. Fall back to char heuristic if the LLM module can't be
    # imported (e.g. in unit tests that stub the env).
    if model is None:
        try:
            from app.core.llm import get_model
            model = get_model()
        except Exception:
            model = None

    try:
        from app.core.tokens import count_tokens
        _count = lambda s: count_tokens(s, model=model)
    except Exception:
        _count = lambda s: max(1, len(s) // 4)

    parts = []
    total_tokens = 0

    for chunk in chunks:
        header = f"[{chunk['doc_category']} | {chunk['source_file']}]\n"
        body = chunk["content"].strip()
        block = f"{header}{body}\n"
        block_tokens = _count(block)
        if total_tokens + block_tokens > max_tokens:
            break
        parts.append(block)
        total_tokens += block_tokens

    return "\n---\n".join(parts)
