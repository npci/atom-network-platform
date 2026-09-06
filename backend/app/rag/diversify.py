# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Maximal Marginal Relevance (MMR) chunk diversification — Phase 2.5.

Pure-Python cosine MMR with no numpy dependency so this module is cheap to
import and test in isolation. The standard formulation, picking one chunk
at a time:

    next = argmax over c in pool of:
              lambda * sim(c, query)
              - (1 - lambda) * max over s in selected of sim(c, s)

`lambda = 1.0` is pure relevance (no diversification — equivalent to top-K
by similarity). `lambda = 0.0` is pure novelty. `0.5` is the literature
standard balance.

Each chunk passed to `mmr_select` should carry an `embedding` field — a
list/tuple/iterable of floats. Chunks missing or with malformed embeddings
are excluded from MMR ranking but appended in original order to backfill
the budget so callers always get up to `k` rows.

The function never raises — on any internal failure it returns the input
slice `chunks[:k]` unchanged.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors. 0.0 on any
    structural mismatch (zero-norm, length mismatch, non-iterable, etc.)."""
    try:
        if a is None or b is None:
            return 0.0
        if len(a) != len(b):
            return 0.0
        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for x, y in zip(a, b):
            xf = float(x)
            yf = float(y)
            dot += xf * yf
            norm_a += xf * xf
            norm_b += yf * yf
        if norm_a <= 0.0 or norm_b <= 0.0:
            return 0.0
        return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    except (TypeError, ValueError):
        return 0.0


def _coerce_embedding(value) -> list[float] | None:
    """Best-effort conversion of an embedding field to list[float].
    Accepts list, tuple, numpy-like (`tolist`), or pgvector-text
    `"[0.1,0.2,...]"`. Returns None on any failure."""
    if value is None:
        return None
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            value = tolist()
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        if not s:
            return None
        try:
            return [float(x) for x in s.split(",")]
        except ValueError:
            return None
    if isinstance(value, (list, tuple)):
        try:
            return [float(x) for x in value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, Iterable):
        try:
            return [float(x) for x in value]
        except (TypeError, ValueError):
            return None
    return None


def mmr_select(
    chunks: list[dict],
    query_vec: Sequence[float],
    k: int,
    lambda_: float = 0.5,
) -> list[dict]:
    """Pick `k` chunks from `chunks` using cosine MMR against `query_vec`.

    Returns a new list of up to `k` chunks in selection order. On any
    failure (missing embeddings, malformed query vector) falls open to
    `chunks[:k]`.
    """
    if not chunks or k <= 0:
        return []
    try:
        lam = max(0.0, min(1.0, float(lambda_)))
    except (TypeError, ValueError):
        lam = 0.5

    q = _coerce_embedding(query_vec)
    if q is None:
        return list(chunks[:k])

    # Build parallel list of (orig_index, embedding) for chunks with usable
    # embeddings. Chunks without embeddings stay in `chunks` for backfill.
    embedded: list[tuple[int, list[float]]] = []
    for idx, chunk in enumerate(chunks):
        emb = _coerce_embedding(chunk.get("embedding"))
        if emb is not None:
            embedded.append((idx, emb))

    if not embedded:
        return list(chunks[:k])

    rel = {idx: _cosine(emb, q) for idx, emb in embedded}
    emb_by_idx = {idx: emb for idx, emb in embedded}
    remaining = set(emb_by_idx.keys())
    selected: list[int] = []

    target = min(k, len(embedded))
    while remaining and len(selected) < target:
        best_idx = -1
        best_score = -float("inf")
        for cand_idx in remaining:
            if not selected:
                penalty = 0.0
            else:
                penalty = max(
                    _cosine(emb_by_idx[cand_idx], emb_by_idx[s])
                    for s in selected
                )
            score = lam * rel[cand_idx] - (1.0 - lam) * penalty
            if score > best_score:
                best_score = score
                best_idx = cand_idx
        if best_idx < 0:
            break
        selected.append(best_idx)
        remaining.discard(best_idx)

    out: list[dict] = [chunks[i] for i in selected]

    # Backfill from chunks that lacked embeddings so callers still get
    # min(k, len(chunks)) rows when MMR couldn't fill the budget.
    if len(out) < min(k, len(chunks)):
        seen = set(selected)
        for idx, chunk in enumerate(chunks):
            if idx in seen:
                continue
            out.append(chunk)
            if len(out) >= min(k, len(chunks)):
                break

    return out[:k]
