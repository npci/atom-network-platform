# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Hybrid retrieval: pgvector dense search + BM25 sparse search + RRF fusion.

Strategy:
  1. Pull top-N candidates from both the dense (pgvector) and sparse (BM25)
     rankings in parallel.
  2. Fuse the two rankings using Reciprocal Rank Fusion (RRF) with k=60
     (Cormack et al. 2009). No score normalisation required.
  3. Post-filter out chunks with < 2 query-token overlap; if the filter
     removes everything, fall back to unfiltered results.
  4. Return the top-k chunks as dicts, same shape as the old vector-only
     retrieve() function so call-sites don't need to change.

Taxonomy-driven 3-stage context builder also lives here —
see `build_context_with_taxonomy()`. It calls the 3 query streams the
RAG_SYSTEM paper describes:
    - Stage 1: taxonomy analogue queries (similar past features)
    - Stage 2: feature-description semantic search
    - Stage 3 (optional): doc-type style queries (supplied by caller)
and dedups across stages.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.document_chunk import DocCategory
from app.rag import bm25_search
from app.rag.bm25_search import tokenize
from app.rag.embeddings import embed_query, EMBEDDING_DIM

logger = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────

RRF_K = 60                    # Cormack et al. recommendation
DENSE_CANDIDATES = 10         # top-K from vector search before fusion
SPARSE_CANDIDATES = 10        # top-K from BM25 before fusion
DEFAULT_TOP_K = 6             # final result size
MIN_OVERLAP_TOKENS = 2        # post-filter: require this many query tokens in chunk


# ── Low-level helpers ────────────────────────────────────────────────────────

def _dense_search(
    query: str,
    db: Session,
    top_k: int,
    categories: list[DocCategory] | None,
) -> list[dict]:
    """pgvector cosine similarity search — unfiltered by score."""
    query_vec = embed_query(query)
    vec_literal = "[" + ",".join(str(v) for v in query_vec) + "]"

    # Phase 1.1 — HNSW `ef_search` is per-session in pgvector. Set it before
    # the query so the new HNSW index (migration 0028) is actually used with
    # tuneable recall. 80 is a reasonable default: high recall, low overhead.
    # Wrapped fail-soft: if the GUC isn't recognised (older pgvector,
    # non-Postgres dialect, missing index) we silently skip — query still works.
    try:
        db.execute(text("SET LOCAL hnsw.ef_search = 80"))
    except Exception:
        pass

    cat_filter = ""
    params: dict = {"top_k": top_k, "vec_literal": vec_literal}
    if categories:
        placeholders = ", ".join(f":cat{i}" for i in range(len(categories)))
        cat_filter = f"AND doc_category IN ({placeholders})"
        for i, cat in enumerate(categories):
            params[f"cat{i}"] = cat.value if hasattr(cat, "value") else str(cat)

    sql = text(f"""
        SELECT
            id,
            source_file,
            doc_category,
            content,
            chunk_index,
            parent_symbol_id,
            1 - (embedding <=> CAST(:vec_literal AS vector({EMBEDDING_DIM}))) AS score
        FROM document_chunks
        WHERE embedding IS NOT NULL
          AND (deprecated IS NOT TRUE)
        {cat_filter}
        ORDER BY embedding <=> CAST(:vec_literal AS vector({EMBEDDING_DIM}))
        LIMIT :top_k
    """)
    rows = db.execute(sql, params).fetchall()
    # Zero-norm embeddings (embedder output for near-empty chunks) make the
    # cosine distance NaN — a meaningless similarity, and a NaN dense_score
    # is invalid JSON for the JSONB context-cache row downstream. Drop them.
    return [
        {
            "id":                row.id,
            "source_file":       row.source_file,
            "doc_category":      row.doc_category,
            "content":           row.content,
            "chunk_index":       row.chunk_index,
            "parent_symbol_id":  row.parent_symbol_id,
            "dense_score":       round(float(row.score), 4),
        }
        for row in rows
        if math.isfinite(float(row.score))
    ]


def _hydrate_chunks(db: Session, chunk_ids: list[str]) -> dict[str, dict]:
    """Fetch full chunk rows by id in one query. Returns {id: chunk_dict}."""
    if not chunk_ids:
        return {}
    sql = text("""
        SELECT id, source_file, doc_category, content, chunk_index, parent_symbol_id
        FROM document_chunks
        WHERE id = ANY(:ids)
          AND (deprecated IS NOT TRUE)
    """)
    rows = db.execute(sql, {"ids": chunk_ids}).fetchall()
    return {
        row.id: {
            "id":                row.id,
            "source_file":       row.source_file,
            "doc_category":      row.doc_category,
            "content":           row.content,
            "chunk_index":       row.chunk_index,
            "parent_symbol_id":  row.parent_symbol_id,
        }
        for row in rows
    }


def _dedup_by_parent_symbol(chunks: list[dict]) -> list[dict]:
    """Slice 4 — after RRF fusion, collapse multiple views of the same symbol
    (body / signature / nl_summary) to a single representative row — the one
    with the highest fused score. Rows with no `parent_symbol_id` (pre-Slice-4
    data, file chunks, non-code docs) always pass through.

    Input is assumed to already be sorted by descending `score` so the first
    occurrence per parent_symbol_id is the best-scoring one.
    """
    seen_parents: set[str] = set()
    deduped: list[dict] = []
    for chunk in chunks:
        pid = chunk.get("parent_symbol_id")
        if pid:
            if pid in seen_parents:
                continue
            seen_parents.add(pid)
        deduped.append(chunk)
    return deduped


def _overlap_filter(chunks: list[dict], query: str, min_overlap: int) -> list[dict]:
    """Keep chunks sharing at least *min_overlap* tokens with the query.

    Falls back to all chunks if the filter removes everything (common for
    short queries or very specific terms).
    """
    q_tokens = set(tokenize(query))
    if len(q_tokens) < min_overlap:
        # Query itself has too few tokens — filter would be too aggressive
        return chunks

    filtered: list[dict] = []
    for c in chunks:
        c_tokens = set(tokenize(c["content"]))
        if len(q_tokens & c_tokens) >= min_overlap:
            filtered.append(c)

    return filtered if filtered else chunks


# ── Hybrid retrieve (main entry point) ───────────────────────────────────────

def hybrid_retrieve(
    query: str,
    db: Session,
    top_k: int = DEFAULT_TOP_K,
    categories: list[DocCategory] | None = None,
    *,
    apply_overlap_filter: bool = True,
) -> list[dict]:
    """Run dense + sparse search in parallel, fuse with RRF, return top_k."""
    if not query or not query.strip():
        return []

    # Lazy-rebuild BM25 if a celery ingestion bumped the Redis generation counter.
    try:
        bm25_search.ensure_fresh(db)
    except Exception as e:
        logger.debug("bm25 ensure_fresh check failed (continuing): %s", e)

    # 1. Dense
    dense_results = _dense_search(query, db, DENSE_CANDIDATES, categories)

    # 2. Sparse (BM25 over the entire corpus — category filter applied post-fuse)
    # Pass `db` so the tsvector backend can run its Postgres query;
    # the legacy rank_bm25 backend ignores the kwarg.
    sparse_pairs: list[tuple[str, float]] = []
    if bm25_search.is_ready(db):
        sparse_pairs = bm25_search.search(query, top_k=SPARSE_CANDIDATES, db=db)

    # 3. RRF fusion — accumulate per-chunk scores
    rrf_scores: dict[str, float] = defaultdict(float)
    per_chunk_info: dict[str, dict] = {}

    for rank, c in enumerate(dense_results):
        rrf_scores[c["id"]] += 1.0 / (RRF_K + rank + 1)
        per_chunk_info.setdefault(c["id"], {}).update(
            dense_rank=rank + 1, dense_score=c["dense_score"],
        )

    for rank, (chunk_id, bm25_score) in enumerate(sparse_pairs):
        rrf_scores[chunk_id] += 1.0 / (RRF_K + rank + 1)
        per_chunk_info.setdefault(chunk_id, {}).update(
            bm25_rank=rank + 1, bm25_score=round(bm25_score, 4),
        )

    if not rrf_scores:
        return []

    # 4. Hydrate + assemble result rows
    already_hydrated = {c["id"]: c for c in dense_results}
    missing_ids = [cid for cid in rrf_scores if cid not in already_hydrated]
    extra = _hydrate_chunks(db, missing_ids)

    fused: list[dict] = []
    for chunk_id, score in rrf_scores.items():
        base = already_hydrated.get(chunk_id) or extra.get(chunk_id)
        if not base:
            continue
        # Apply category filter post-fuse (dense already filtered; sparse didn't)
        if categories:
            cat_values = {c.value if hasattr(c, "value") else str(c) for c in categories}
            if base["doc_category"] not in cat_values:
                continue
        enriched = {**base, **per_chunk_info.get(chunk_id, {}), "score": round(score, 5)}
        enriched.setdefault("dense_score", None)
        enriched.setdefault("bm25_score", None)
        fused.append(enriched)

    # 5. Sort by RRF score, over-sample, post-filter, Slice 4 dedup, truncate.
    fused.sort(key=lambda c: c["score"], reverse=True)
    # Over-sample more aggressively when multiview is active — we may lose up
    # to 2/3 of rows to parent_symbol_id dedup.
    fused = fused[: top_k * 3]

    if apply_overlap_filter:
        fused = _overlap_filter(fused, query, MIN_OVERLAP_TOKENS)

    # Slice 4 — collapse multiple views of the same symbol to one best-scored row.
    fused = _dedup_by_parent_symbol(fused)

    final = fused[:top_k]
    logger.info(
        "Hybrid retrieve: query=%r dense=%d sparse=%d fused=%d returned=%d",
        query[:60], len(dense_results), len(sparse_pairs), len(rrf_scores), len(final),
    )
    return final


# ── Taxonomy-driven 3-stage context builder ──────────────────────────────────

def build_context_with_taxonomy(
    feature_description: str,
    classification: dict,
    db: Session,
    *,
    per_query_top_k: int = 3,
    overall_top_k: int = 10,
    style_queries: list[str] | None = None,
    categories: list[DocCategory] | None = None,
) -> tuple[list[dict], str]:
    """Run the 3-stage retrieval used by RAG_SYSTEM and A2A-main.

    Stage 1: taxonomy analogue_queries (similar past features)
    Stage 2: feature-description semantic search
    Stage 3: optional doc-type style queries (caller supplies)

    De-duplicates by chunk_id across stages, ranks by *best* fused score
    any stage gave the chunk.

    Returns (chunks, context_string) where context_string is ready to paste
    into an LLM prompt.
    """
    from app.agents.taxonomy import get_analogue_queries  # local import to avoid cycles

    seen: dict[str, dict] = {}

    def _ingest(stage: str, results: list[dict]):
        for r in results:
            existing = seen.get(r["id"])
            if existing is None or r["score"] > existing["score"]:
                seen[r["id"]] = {**r, "stage": stage}

    # Stage 1: analogue queries from taxonomy
    for q in get_analogue_queries(classification):
        _ingest("analogue", hybrid_retrieve(q, db, top_k=per_query_top_k, categories=categories))

    # Stage 2: the feature description itself
    _ingest(
        "semantic",
        hybrid_retrieve(feature_description, db, top_k=per_query_top_k * 2, categories=categories),
    )

    # Stage 3: caller-supplied style queries
    if style_queries:
        for q in style_queries:
            _ingest("style", hybrid_retrieve(q, db, top_k=per_query_top_k, categories=categories))

    merged = sorted(seen.values(), key=lambda c: c["score"], reverse=True)[:overall_top_k]

    # Build prompt-ready context
    parts: list[str] = []
    for chunk in merged:
        header = f"[{chunk['doc_category']} | {chunk['source_file']} | stage={chunk.get('stage','?')}]"
        parts.append(f"{header}\n{chunk['content'].strip()}")
    context_str = "\n\n---\n\n".join(parts)
    return merged, context_str
