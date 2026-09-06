# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 2.4 — Reranker score cache + early-exit tests.

The local CrossEncoder is not loaded — we monkeypatch `_rerank_local`
to record invocations and return a deterministic ranking.
"""
from __future__ import annotations

import pytest

from app.rag import reranker as rr
from app.rag.reranker import (
    _reset_reranker_cache_for_tests,
    rerank,
    reranker_cache_stats,
)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    _reset_reranker_cache_for_tests()
    # Force backend = local so we hit the patched fn.
    from app.core.config import settings as _s
    monkeypatch.setattr(_s, "reranker_backend", "local", raising=False)
    yield
    _reset_reranker_cache_for_tests()


def _candidates(n: int) -> list[dict]:
    return [{"id": f"c{i}", "content": f"chunk {i}"} for i in range(n)]


def _patch_local(monkeypatch):
    calls = {"n": 0}

    def fake_local(query, cands, top_k):
        calls["n"] += 1
        # Deterministic — return first top_k with descending fake scores.
        return [
            {**c, "rerank_score": float(top_k - i)}
            for i, c in enumerate(cands[:top_k])
        ]

    monkeypatch.setattr(rr, "_rerank_local", fake_local)
    return calls


def test_early_exit_skips_backend_below_threshold(monkeypatch):
    calls = _patch_local(monkeypatch)
    from app.core.config import settings as _s
    monkeypatch.setattr(_s, "reranker_min_candidates", 2, raising=False)
    # 2 candidates → at threshold → early-exit.
    out = rerank("q", _candidates(2), top_k=2)
    assert calls["n"] == 0
    assert [c["id"] for c in out] == ["c0", "c1"]
    # 3 candidates → above threshold → backend runs.
    rerank("q", _candidates(3), top_k=3)
    assert calls["n"] == 1


def test_score_cache_hits_skip_backend(monkeypatch):
    calls = _patch_local(monkeypatch)
    cands = _candidates(5)
    # First call — miss, backend runs.
    rerank("query a", cands, top_k=3)
    # Identical inputs — cache hit, backend not invoked.
    rerank("query a", cands, top_k=3)
    rerank("query a", cands, top_k=3)
    assert calls["n"] == 1
    stats = reranker_cache_stats()
    assert stats["hits"] >= 2
    assert stats["misses"] == 1


def test_score_cache_distinguishes_top_k(monkeypatch):
    calls = _patch_local(monkeypatch)
    cands = _candidates(5)
    rerank("q", cands, top_k=3)
    # Same query+candidates, different top_k → different cache key.
    rerank("q", cands, top_k=5)
    assert calls["n"] == 2


def test_score_cache_disabled_via_flag(monkeypatch):
    calls = _patch_local(monkeypatch)
    from app.core.config import settings as _s
    monkeypatch.setattr(_s, "use_reranker_score_cache", False, raising=False)
    cands = _candidates(5)
    rerank("q", cands, top_k=3)
    rerank("q", cands, top_k=3)
    assert calls["n"] == 2  # cache off → backend ran twice


def test_zero_candidates_returns_empty(monkeypatch):
    _patch_local(monkeypatch)
    assert rerank("q", [], top_k=3) == []
