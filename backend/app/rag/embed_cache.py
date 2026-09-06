# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 3.2 — content-hash keyed embedding cache.

Looks up previously-computed embedding vectors by `(content_sha256, model,
view_kind)` so re-ingests of unchanged chunks reuse the previous vector
instead of paying the Ollama round-trip.

Public API:
    sha_for(content: str) -> str
        Stable SHA256 hex digest of the chunk content. Same function ingest
        code should use to key cache lookups.

    get_many(db, shas, model, view_kind) -> dict[sha, list[float]]
        Bulk fetch — single SELECT.

    put_many(db, items, model) -> int
        Bulk upsert — INSERT ... ON CONFLICT DO NOTHING. Returns number of
        rows successfully inserted (cache misses that just got persisted).

    bypass_enabled() -> bool
        Returns True when EMBED_BYPASS_CACHE is set, honouring the per-run
        escape hatch.

    stats() -> dict
        Module-level hit/miss counters for ops dashboards.

Fail-soft everywhere: any DB error logs a warning and degrades to a cache
miss (the caller falls back to a fresh embedding call). This module must
NEVER raise into the ingest loop — a broken cache must not break ingestion.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Iterable

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── Counters (process-local) ──────────────────────────────────────────────────

_lock = threading.RLock()
_hits = 0
_misses = 0
_writes = 0
_errors = 0


def stats() -> dict:
    with _lock:
        return {"hits": _hits, "misses": _misses, "writes": _writes, "errors": _errors}


def _reset_stats_for_tests() -> None:
    global _hits, _misses, _writes, _errors
    with _lock:
        _hits = _misses = _writes = _errors = 0


# ── Helpers ──────────────────────────────────────────────────────────────────

def sha_for(content: str | None) -> str:
    """Stable hex SHA256 of a chunk's textual content."""
    if content is None:
        content = ""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def bypass_enabled() -> bool:
    """Operator escape hatch — `EMBED_BYPASS_CACHE=1` skips the cache."""
    return os.getenv("EMBED_BYPASS_CACHE", "").lower() in ("1", "true", "yes")


def _vector_to_pgliteral(vec: list[float]) -> str:
    """pgvector accepts vectors written as the canonical "[a,b,c]" string.
    Building this is cheaper than instantiating a sqlalchemy Vector type."""
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def _parse_pgvector(text_val: str | None) -> list[float] | None:
    """Reverse — parse pgvector's "[a,b,c]" output back to list[float]."""
    if not text_val:
        return None
    s = text_val.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s:
        return None
    try:
        return [float(x) for x in s.split(",")]
    except ValueError:
        return None


# ── Public ops ────────────────────────────────────────────────────────────────

def get_many(
    db: Session,
    shas: Iterable[str],
    model: str,
    view_kind: str = "",
) -> dict[str, list[float]]:
    """Bulk lookup. Returns `{sha: vector}` for any rows that exist.

    Args:
        db:        SQLAlchemy session. Caller controls the transaction.
        shas:      Iterable of hex SHA256 strings. Duplicates tolerated.
        model:     Embedding model id (must match what was stored).
        view_kind: '' for default; multiview/window suffixes are distinct keys.
    """
    global _hits, _misses, _errors
    sha_list = list({s for s in shas if s})
    if not sha_list:
        return {}
    if bypass_enabled():
        with _lock:
            _misses += len(sha_list)
        return {}

    try:
        rows = db.execute(
            sql_text(
                "SELECT content_sha256, embedding::text AS emb "
                "FROM embedding_cache "
                "WHERE model = :model "
                "  AND view_kind = :view_kind "
                "  AND content_sha256 = ANY(:shas)"
            ),
            {"model": model, "view_kind": view_kind or "", "shas": sha_list},
        ).all()
    except Exception as e:
        # Most likely "relation embedding_cache does not exist" if the
        # migration hasn't run yet. Don't block ingest — log once and
        # degrade to cache-miss for the whole batch.
        with _lock:
            _errors += 1
        logger.warning(
            "embed_cache.get_many failed (%s) — degrading to cache miss", e,
        )
        return {}

    out: dict[str, list[float]] = {}
    for row in rows:
        vec = _parse_pgvector(row.emb)
        if vec is not None:
            out[row.content_sha256] = vec

    with _lock:
        _hits += len(out)
        _misses += len(sha_list) - len(out)
    return out


def put_many(
    db: Session,
    items: list[tuple[str, list[float], str]],
    model: str,
) -> int:
    """Bulk upsert. Each `items` tuple is `(sha, vector, view_kind)`.

    Uses `ON CONFLICT DO NOTHING` so concurrent ingest workers don't fight
    over the same row. Caller commits.

    Returns the number of rows that were actually inserted (i.e., genuine
    cache misses that are now persisted). Conflicts (already cached) are
    not counted.
    """
    global _writes, _errors
    if not items:
        return 0
    if bypass_enabled():
        return 0

    # Build parameter list. Skip any zero/empty vectors — a row with no real
    # embedding is a poison entry and would mask future legitimate computes.
    params: list[dict] = []
    for sha, vec, view_kind in items:
        if not sha or not vec or len(vec) == 0:
            continue
        if not any(abs(float(v)) > 1e-9 for v in vec):
            # All zeros — skip caching. embed_texts returns [0.0]*EMBED_DIM
            # on hard failure; we don't want to cache that.
            continue
        params.append({
            "sha":       sha,
            "model":     model,
            "view_kind": view_kind or "",
            "embedding": _vector_to_pgliteral(vec),
        })

    if not params:
        return 0

    try:
        db.execute(
            sql_text(
                "INSERT INTO embedding_cache "
                "  (content_sha256, model, view_kind, embedding) "
                "VALUES "
                "  (:sha, :model, :view_kind, CAST(:embedding AS vector)) "
                "ON CONFLICT (content_sha256, model, view_kind) DO NOTHING"
            ),
            params,
        )
    except Exception as e:
        with _lock:
            _errors += 1
        logger.warning(
            "embed_cache.put_many failed (%s) — continuing without caching this batch",
            e,
        )
        return 0

    with _lock:
        _writes += len(params)
    return len(params)


def embed_chunks_with_cache(
    db: Session,
    chunks: list[dict],
    *,
    text_key: str = "content",
) -> list[list[float]]:
    """High-level wrapper used by the ingest pipeline.

    Phase 3.2 entrypoint. Consults the embedding cache for every chunk first,
    only invokes the embedder for the misses, and persists the new vectors
    back to the cache for future re-ingests.

    Each chunk dict may supply a `view_kind` field (empty string when absent)
    so multiview / per-window rows are cached under distinct keys for the
    same source text.

    Fail-soft: if the cache table is missing or any DB op fails, this
    degrades transparently to plain `embed_texts(...)` — ingestion is never
    blocked by a broken cache.

    Returns: list of embedding vectors, one per chunk, in the same order
    as the input. Same shape as `embed_texts(...)`.
    """
    if not chunks:
        return []

    # Lazy import — avoids a circular import at module load (embeddings.py
    # is imported by embed_cache callers indirectly via the ingest loop).
    from app.rag.embeddings import embed_texts, EMBEDDING_DIM, EMBEDDING_MODEL

    # Step 1 — gather lookup keys. Group sha lookups by (model, view_kind)
    # because the SQL filter is per-(model, view_kind).
    keys_per_chunk: list[tuple[str, str]] = []      # (sha, view_kind) per chunk
    shas_by_view: dict[str, set[str]] = {}
    for c in chunks:
        sha = sha_for(c.get(text_key) or "")
        vk = (c.get("view_kind") or "") if isinstance(c.get("view_kind"), (str, type(None))) else ""
        keys_per_chunk.append((sha, vk))
        shas_by_view.setdefault(vk, set()).add(sha)

    # Step 2 — bulk get per view_kind bucket.
    cached: dict[tuple[str, str], list[float]] = {}
    for vk, shas in shas_by_view.items():
        hits = get_many(db, list(shas), model=EMBEDDING_MODEL, view_kind=vk)
        for sha, vec in hits.items():
            cached[(sha, vk)] = vec

    # Step 3 — embed only the misses, preserving original positions.
    miss_indices: list[int] = []
    miss_texts: list[str] = []
    for i, (sha, vk) in enumerate(keys_per_chunk):
        if (sha, vk) not in cached:
            miss_indices.append(i)
            miss_texts.append(chunks[i].get(text_key) or "")

    fresh_vectors: list[list[float]] = []
    if miss_texts:
        fresh_vectors = embed_texts(miss_texts)

    # Step 4 — stitch the result list together in input order.
    out: list[list[float]] = [None] * len(chunks)  # type: ignore[list-item]
    fresh_cursor = 0
    new_cache_entries: list[tuple[str, list[float], str]] = []
    for i, (sha, vk) in enumerate(keys_per_chunk):
        hit = cached.get((sha, vk))
        if hit is not None:
            out[i] = hit
        else:
            vec = fresh_vectors[fresh_cursor]
            fresh_cursor += 1
            out[i] = vec
            new_cache_entries.append((sha, vec, vk))

    # Step 5 — persist newly-computed vectors. Cache misses-now-becoming-hits.
    if new_cache_entries:
        put_many(db, new_cache_entries, model=EMBEDDING_MODEL)

    return out
