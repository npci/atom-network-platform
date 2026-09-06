# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cross-encoder reranker (Slice 6).

Runs after the full RRF + filter + dedup pipeline. Two backends, selected
via `settings.reranker_backend`:

  - **local** (default): in-process CrossEncoder via sentence-transformers.
    Lazy-loaded on first call; weights cached on disk afterwards. Requires
    either internet on first call OR a pre-populated HuggingFace cache.

  - **remote**: HTTP POST to an external reranker service. Request shape:
        POST <reranker_url>
        {"query": str, "candidates": [{"text": str, ...}], "top_k": int}
    Response shape:
        {"results": [{"text": str, "score": float, ...}], "latency_ms": float}
    The service is expected to fail-open server-side (returns RRF order if
    its own model is unavailable). We additionally fail-open client-side on
    any HTTP/parse error.

Both backends share a single `rerank()` entrypoint. Fail-open is preserved
end-to-end — retrieval never raises because of reranker infra.

Design:
  - **Backend selection** at call time via `settings.reranker_backend`.
    Misconfiguration falls back to "local" with a logged warning.
  - **Local backend**: model init guarded by an RLock; failure on first
    call sets a sentinel so we don't retry on every subsequent request.
  - **Remote backend**: stateless — one httpx call per `rerank()`. No
    process-level model load.
  - **Fail-open** at every layer (network error, schema mismatch,
    score-count mismatch, individual chunk drop) → return
    `candidates[:top_k]` unchanged.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

BATCH_SIZE = 16

# Sentinel used to mark a failed model-load so we don't retry on every call.
_LOAD_FAILED = object()

_model: Any = None
_model_lock = threading.RLock()

# ── Phase 2.4 — Reranker score cache ──────────────────────────────────────────
# Process-local LRU keyed on SHA256(query + sorted-by-input chunk-id list)
# → list of fully scored candidate dicts (in score order, top-K already
# applied). Backend-agnostic: stores the post-rerank rows regardless of
# whether they came from local or remote.
_score_cache: "OrderedDict[str, list[dict]]" = OrderedDict()
_score_cache_lock = threading.RLock()
_score_cache_hits = 0
_score_cache_misses = 0


def _score_cache_capacity() -> int:
    try:
        return int(getattr(settings, "reranker_score_cache_size", 512) or 0)
    except Exception:
        return 512


def _score_cache_enabled() -> bool:
    try:
        return bool(getattr(settings, "use_reranker_score_cache", True))
    except Exception:
        return True


def _score_cache_key(query: str, candidates: list[dict], top_k: int) -> str:
    """Build a stable key from (query, ordered tuple of chunk ids, top_k).

    Falls back to position-based ids for chunks lacking an `id`. `top_k` is
    included so a 5-item ask doesn't reuse a 3-item cached cut.
    """
    parts = [query or ""]
    for i, c in enumerate(candidates):
        parts.append(str(c.get("id") or f"_idx_{i}"))
    parts.append(f"k={top_k}")
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()
    return digest


def _score_cache_get(key: str) -> "list[dict] | None":
    global _score_cache_hits, _score_cache_misses
    if _score_cache_capacity() <= 0 or not _score_cache_enabled():
        return None
    with _score_cache_lock:
        val = _score_cache.get(key)
        if val is None:
            _score_cache_misses += 1
            return None
        _score_cache.move_to_end(key)
        _score_cache_hits += 1
        # Defensive copy — don't expose a shared list reference to callers.
        return [dict(r) for r in val]


def _score_cache_put(key: str, value: list[dict]) -> None:
    cap = _score_cache_capacity()
    if cap <= 0 or not _score_cache_enabled():
        return
    snapshot = [dict(r) for r in value]
    with _score_cache_lock:
        if key in _score_cache:
            _score_cache.move_to_end(key)
            _score_cache[key] = snapshot
            return
        _score_cache[key] = snapshot
        while len(_score_cache) > cap:
            _score_cache.popitem(last=False)


def reranker_cache_stats() -> dict:
    with _score_cache_lock:
        return {
            "size":     len(_score_cache),
            "capacity": _score_cache_capacity(),
            "enabled":  _score_cache_enabled(),
            "hits":     _score_cache_hits,
            "misses":   _score_cache_misses,
        }


def _reset_reranker_cache_for_tests() -> None:
    global _score_cache_hits, _score_cache_misses
    with _score_cache_lock:
        _score_cache.clear()
        _score_cache_hits = 0
        _score_cache_misses = 0


def _load_cross_encoder():
    """The blocking model load (import + ~600MB first-call download). Extracted so the
    deadline wrapper in _get_model can run it in a thread and so tests can stub it."""
    from sentence_transformers import CrossEncoder
    return CrossEncoder(settings.reranker_model)


def _get_model():
    """Lazy-load the cross-encoder. Returns the model, or None if unavailable.

    First call pays the ~600MB download + PyTorch init cost; cached thereafter.
    If loading fails (package not installed, disk full, offline), we record a
    sentinel and return None forever — caller falls back.
    """
    global _model
    # Fast path — already loaded (or already failed). No lock needed.
    if _model is _LOAD_FAILED:
        return None
    if _model is not None:
        return _model

    with _model_lock:
        # Re-check inside the lock.
        if _model is _LOAD_FAILED:
            return None
        if _model is not None:
            return _model

        # Bound the load: the ~600MB first-call download can STALL with no network timeout
        # and would otherwise hang the whole agent loop forever (the "stuck on Analyzing
        # existing schemas" failure). Run it in a daemon thread with a deadline; on timeout
        # OR error, fail open to RRF order and disable reranking for this process — never
        # block the caller. A network-level HF timeout makes a stall surface as an error too.
        import os
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "20")
        timeout_s = getattr(settings, "reranker_load_timeout_s", 45) or 45
        box: dict = {}

        def _load():
            try:
                box["model"] = _load_cross_encoder()
            except Exception as exc:  # noqa: BLE001 — reported to the caller below
                box["err"] = exc

        logger.info("Reranker: loading cross-encoder %r (deadline %ss; first call downloads ~600MB)",
                    settings.reranker_model, timeout_s)
        t = threading.Thread(target=_load, name="reranker-load", daemon=True)
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            logger.warning("Reranker: load of %r exceeded %ss — falling back to RRF order "
                           "(reranking disabled for this process). Pre-cache the model or set "
                           "reranker_backend=remote to avoid the in-process download.",
                           settings.reranker_model, timeout_s)
            _model = _LOAD_FAILED
            return None
        if "model" in box:
            _model = box["model"]
            logger.info("Reranker: model loaded")
        else:
            logger.warning("Reranker: failed to load %r — falling back to RRF order forever: %s",
                           settings.reranker_model, box.get("err"))
            _model = _LOAD_FAILED
            return None

    return _model


def _rerank_local(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """In-process CrossEncoder rerank. Original implementation."""
    model = _get_model()
    if model is None:
        return list(candidates[:top_k])

    pairs = [(query or "", c.get("content") or "") for c in candidates]
    scores: list[float] = []
    try:
        for start in range(0, len(pairs), BATCH_SIZE):
            batch = pairs[start : start + BATCH_SIZE]
            batch_scores = model.predict(batch)
            if hasattr(batch_scores, "tolist"):
                batch_scores = batch_scores.tolist()
            scores.extend(float(s) for s in batch_scores)
    except Exception as e:
        logger.warning("Reranker[local] predict failed — falling back: %s", e)
        return list(candidates[:top_k])

    if len(scores) != len(candidates):
        logger.warning(
            "Reranker[local] produced %d scores for %d candidates — falling back",
            len(scores), len(candidates),
        )
        return list(candidates[:top_k])

    scored = [{**c, "rerank_score": s} for c, s in zip(candidates, scores)]
    scored.sort(key=lambda c: c["rerank_score"], reverse=True)
    logger.debug(
        "Reranker[local]: scored %d, keeping top %d (best=%.4f worst=%.4f)",
        len(scored), min(top_k, len(scored)),
        scored[0]["rerank_score"], scored[-1]["rerank_score"],
    )
    return scored[:top_k]


def _rerank_remote(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """HTTP rerank against an external embed_svc-style `/rerank` endpoint.

    Request body shape:
        {"query": str, "candidates": [{"text": str, ...}], "top_k": int}
    Expected response:
        {"results": [{"text": str, "score": float, ...}], "latency_ms": float}

    We send the candidate's `content` as `text` (their service expects
    `text`) plus metadata (`source_file` → `file_path` for their noise
    filter, `doc_category` → `source` for their RRF tie-break). On the
    way back, we re-attach the original chunk dict (preserving `id`,
    `chunk_index`, etc.) and copy the service's `score` to `rerank_score`.

    Fail-open on every error.
    """
    url = (settings.reranker_url or "").strip()
    if not url:
        logger.warning("Reranker[remote]: reranker_url not set — falling back")
        return list(candidates[:top_k])

    # Cap the outbound payload. The remote service runs CrossEncoder with
    # max_length=512 tokens (~2000 chars) and its own RERANKER_MAX_CANDIDATES
    # default of 12. Sending 40 raw chunks × 6000 chars each (~240KB body)
    # caused 10-second timeouts on every call in the 2026-05-03 production
    # run. Truncating client-side matches what the model actually sees and
    # cuts wire size by ~85%.
    MAX_CANDIDATES_REMOTE = 12
    MAX_TEXT_CHARS_REMOTE = 2000
    candidates_for_remote = candidates[:MAX_CANDIDATES_REMOTE]

    # Build a stable key per candidate so we can re-attach the original
    # dict after the service returns. Using `id` if present, else position.
    keyed: list[tuple[str, dict]] = []
    payload_candidates: list[dict] = []
    for i, c in enumerate(candidates_for_remote):
        key = str(c.get("id") or f"_idx_{i}")
        keyed.append((key, c))
        text_full = c.get("content") or ""
        payload_candidates.append({
            "text":      text_full[:MAX_TEXT_CHARS_REMOTE],
            "file_path": c.get("source_file") or "",
            "source":    c.get("doc_category") or "",
            "_key":      key,    # round-trip key — service preserves extra fields
        })

    body = {
        "query":      query or "",
        "candidates": payload_candidates,
        "top_k":      top_k,
    }

    try:
        import httpx
        with httpx.Client(timeout=settings.reranker_timeout_sec) as client:
            resp = client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Reranker[remote] HTTP call failed (%s) — falling back: %s",
                       url, e)
        return list(candidates[:top_k])

    results = data.get("results")
    if not isinstance(results, list):
        logger.warning("Reranker[remote] missing 'results' list in response — falling back")
        return list(candidates[:top_k])

    by_key = {k: c for k, c in keyed}
    out: list[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        key = r.get("_key")
        original = by_key.get(key)
        if original is None:
            # Service stripped our _key; try matching by text as last resort.
            txt = (r.get("text") or "").strip()
            if txt:
                for k, c in keyed:
                    if (c.get("content") or "").strip() == txt:
                        original = c
                        break
        if original is None:
            continue
        score = r.get("score")
        try:
            score_f = float(score) if score is not None else 0.0
        except (TypeError, ValueError):
            score_f = 0.0
        out.append({**original, "rerank_score": score_f})

    if not out:
        logger.warning("Reranker[remote] returned 0 reusable results — falling back")
        return list(candidates[:top_k])

    latency_ms = data.get("latency_ms")
    logger.debug(
        "Reranker[remote] (%s): %d candidates → %d results, latency=%sms, best=%.4f",
        url, len(candidates), len(out), latency_ms,
        out[0].get("rerank_score", 0.0),
    )
    return out[:top_k]


def rerank(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """Re-order candidates by reranker score (local or remote backend).

    Backend selection is governed by `settings.reranker_backend`:
      - "local"  → in-process CrossEncoder (default).
      - "remote" → HTTP POST to `settings.reranker_url`.

    Each returned chunk carries a `rerank_score` field. Sorted by score
    descending, truncated to `top_k`.

    On any failure (model load, HTTP error, schema mismatch), falls back
    to `candidates[:top_k]` — this function never raises.

    Phase 2.4 — two cheap optimisations layered on top:
      1. Early-exit when `len(candidates) <= reranker_min_candidates`. With
         the default of 2, ranking a 1- or 2-item list (where order is
         barely meaningful) skips the cross-encoder entirely.
      2. Process-local LRU score cache keyed on (query, ordered chunk
         ids, top_k). Repeat asks reuse the prior result without any
         model call.

    Args:
        query: User query text (raw, not HyDE-rewritten).
        candidates: Chunks from hybrid retrieval. Must contain `content`.
        top_k: Max rows to return.
    """
    if not candidates or top_k <= 0:
        return []

    # Phase 2.4 — early-exit on too-few candidates. Generalises the original
    # 1-candidate skip and saves the model load when the retrieval pool is
    # below the configured threshold.
    try:
        min_cands = int(getattr(settings, "reranker_min_candidates", 2) or 0)
    except Exception:
        min_cands = 2
    if len(candidates) <= max(min_cands, 1):
        return list(candidates[:top_k])

    # Phase 2.4 — score cache hit short-circuits the backend call entirely.
    cache_key = _score_cache_key(query, candidates, top_k)
    cached = _score_cache_get(cache_key)
    if cached is not None:
        logger.debug(
            "Reranker: score-cache hit (q=%r, n=%d, top_k=%d)",
            (query or "")[:40], len(candidates), top_k,
        )
        return cached

    backend = (settings.reranker_backend or "local").strip().lower()
    if backend == "remote":
        result = _rerank_remote(query, candidates, top_k)
    elif backend == "local":
        result = _rerank_local(query, candidates, top_k)
    else:
        logger.warning(
            "Reranker: unknown reranker_backend=%r — falling back to 'local'",
            backend,
        )
        result = _rerank_local(query, candidates, top_k)

    # Only cache when the backend actually scored — i.e. at least one row
    # has a rerank_score field. Fail-open paths that return the input order
    # unchanged shouldn't poison the cache.
    if result and any("rerank_score" in r for r in result):
        _score_cache_put(cache_key, result)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Test-only hook
# ──────────────────────────────────────────────────────────────────────────────

def _reset_model_for_tests():
    """Clear the cached model so tests can re-exercise the lazy-load path."""
    global _model
    with _model_lock:
        _model = None
