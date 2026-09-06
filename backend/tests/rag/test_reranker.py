# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the cross-encoder reranker (Slice 6).

Pure tests — `sentence_transformers.CrossEncoder` is NEVER actually loaded.
We mock at the module boundary (`reranker._get_model`) so these tests run
on any machine regardless of whether the real package is installed.

Also tests the retrieval.retrieve() wiring — flag on/off → reranker called or
skipped.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.rag import reranker, retrieval


# ──────────────────────────────────────────────────────────────────────────────
# Minimal fake cross-encoder
# ──────────────────────────────────────────────────────────────────────────────

class FakeCrossEncoder:
    """Deterministic stand-in for `sentence_transformers.CrossEncoder`.

    Scoring rule: len(overlap(query_tokens, content_tokens)) — straightforward
    lexical overlap so tests can construct predictable rankings.
    """
    def __init__(self, score_overrides: dict | None = None):
        self.score_overrides = score_overrides or {}
        self.predict_calls = 0

    def predict(self, pairs):
        self.predict_calls += 1
        out = []
        for query, content in pairs:
            if content in self.score_overrides:
                out.append(self.score_overrides[content])
            else:
                q_tokens = set(query.lower().split())
                c_tokens = set(content.lower().split())
                out.append(float(len(q_tokens & c_tokens)))
        return out


@pytest.fixture(autouse=True)
def _reset_reranker(monkeypatch):
    """Clear the module-level model AND score caches between tests, and pin the
    backend to "local".

    The Phase 2.4 score cache is keyed on (query, candidate ids, top_k) only —
    tests here reuse query "q" with the same 15 fixture chunks, so without this
    one test's reranked order is served straight back to the next one. That is
    what made the model-unavailable fallback test pass alone and fail in-suite.

    THE BACKEND PIN IS NOT COSMETIC. This file tests the IN-PROCESS scoring
    path: it stubs `reranker._get_model` with FakeCrossEncoder and asserts on
    the resulting order. `settings.reranker_backend` defaulted to "local" when
    these tests were written, so they relied on that default implicitly. The
    default became "remote" on 2026-08-28 when torch and sentence-transformers
    moved to the reranker sidecar (6 SBOM findings), at which point `rerank()`
    started routing to `_rerank_remote`, finding no URL configured, and failing
    open to RRF order — so every ordering assertion here broke while the code
    under test was perfectly fine.

    Pinning the value makes the dependency EXPLICIT rather than inherited from
    a default that can legitimately change. The remote path has its own
    coverage; these tests own the local one.
    """
    from app.core import config as _config
    monkeypatch.setattr(_config.settings, "reranker_backend", "local", raising=False)
    reranker._reset_model_for_tests()
    reranker._reset_reranker_cache_for_tests()
    yield
    reranker._reset_model_for_tests()
    reranker._reset_reranker_cache_for_tests()


# ──────────────────────────────────────────────────────────────────────────────
# rerank() — pure behavior
# ──────────────────────────────────────────────────────────────────────────────

def test_rerank_empty_returns_empty(monkeypatch):
    monkeypatch.setattr(reranker, "_get_model", lambda: FakeCrossEncoder())
    assert reranker.rerank("q", [], top_k=5) == []


def test_rerank_single_candidate_returns_unchanged(monkeypatch):
    monkeypatch.setattr(reranker, "_get_model", lambda: FakeCrossEncoder())
    c = [{"id": "a", "content": "hello"}]
    out = reranker.rerank("q", c, top_k=5)
    assert out == c
    # No rerank_score attached on single-candidate short-circuit path.


def test_rerank_orders_by_cross_encoder_score(monkeypatch):
    """Fake scorer gives higher score to content overlapping with query tokens."""
    monkeypatch.setattr(reranker, "_get_model", lambda: FakeCrossEncoder())

    candidates = [
        {"id": "low",  "content": "completely unrelated words"},
        {"id": "high", "content": "network mandate revocation flow"},
        {"id": "mid",  "content": "network mandate"},
    ]
    out = reranker.rerank("network mandate revocation", candidates, top_k=3)

    assert [c["id"] for c in out] == ["high", "mid", "low"]
    assert all("rerank_score" in c for c in out)
    assert out[0]["rerank_score"] > out[1]["rerank_score"] > out[2]["rerank_score"]


def test_rerank_truncates_to_top_k(monkeypatch):
    monkeypatch.setattr(reranker, "_get_model", lambda: FakeCrossEncoder())
    candidates = [{"id": f"c{i}", "content": f"word{i}"} for i in range(8)]
    out = reranker.rerank("word3", candidates, top_k=3)
    assert len(out) == 3
    assert out[0]["id"] == "c3"  # exact overlap wins


def test_rerank_batches_large_lists(monkeypatch):
    """With 40 candidates and BATCH_SIZE=16, predict() should be called 3 times."""
    fake = FakeCrossEncoder()
    monkeypatch.setattr(reranker, "_get_model", lambda: fake)
    candidates = [{"id": f"c{i}", "content": f"word{i}"} for i in range(40)]
    out = reranker.rerank("anything", candidates, top_k=5)
    assert fake.predict_calls == 3  # ceil(40/16)
    assert len(out) == 5


def test_rerank_model_load_failure_falls_back(monkeypatch):
    """_get_model returning None → rerank returns candidates[:top_k] unchanged."""
    monkeypatch.setattr(reranker, "_get_model", lambda: None)
    candidates = [
        {"id": "a", "content": "x"},
        {"id": "b", "content": "y"},
        {"id": "c", "content": "z"},
    ]
    out = reranker.rerank("q", candidates, top_k=2)
    assert [c["id"] for c in out] == ["a", "b"]
    assert "rerank_score" not in out[0]  # didn't run


def test_rerank_predict_exception_falls_back(monkeypatch):
    class ThrowingModel:
        def predict(self, pairs):
            raise RuntimeError("CUDA OOM")

    monkeypatch.setattr(reranker, "_get_model", lambda: ThrowingModel())
    candidates = [
        {"id": "a", "content": "x"},
        {"id": "b", "content": "y"},
    ]
    out = reranker.rerank("q", candidates, top_k=2)
    assert [c["id"] for c in out] == ["a", "b"]


def test_rerank_wrong_score_count_falls_back(monkeypatch):
    """If predict() somehow returns a mismatched number of scores, fall back."""
    class ShortModel:
        def predict(self, pairs):
            return [1.0]  # one score regardless of input size

    monkeypatch.setattr(reranker, "_get_model", lambda: ShortModel())
    candidates = [
        {"id": "a", "content": "x"},
        {"id": "b", "content": "y"},
        {"id": "c", "content": "z"},
    ]
    out = reranker.rerank("q", candidates, top_k=2)
    assert [c["id"] for c in out] == ["a", "b"]  # input order preserved


# ──────────────────────────────────────────────────────────────────────────────
# Lazy load + load-failure memoization
# ──────────────────────────────────────────────────────────────────────────────

def test_get_model_import_error_returns_none(monkeypatch):
    """When sentence_transformers isn't available, _get_model caches the failure."""
    import builtins
    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name.startswith("sentence_transformers"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)

    assert reranker._get_model() is None
    # Second call must NOT retry the import — sentinel caches failure.
    assert reranker._get_model() is None


# ──────────────────────────────────────────────────────────────────────────────
# retrieve() wiring — flag on / off
# ──────────────────────────────────────────────────────────────────────────────

def test_retrieve_flag_off_does_not_invoke_reranker(monkeypatch):
    monkeypatch.setattr(settings, "use_reranker", False)
    monkeypatch.setattr(settings, "use_query_understanding", False)

    def fake_hybrid(query, db, top_k=6, categories=None, **kwargs):
        return [{"id": f"c{i}", "source_file": "x", "doc_category": "d",
                 "content": f"w{i}", "chunk_index": 0, "score": 0.9 - i*0.1,
                 "parent_symbol_id": None} for i in range(top_k)]
    monkeypatch.setattr("app.rag.retrieval.hybrid_retrieve", fake_hybrid)

    # If the reranker were called, this would fail the test
    sentinel = {"called": False}
    def fake_rerank(*args, **kwargs):
        sentinel["called"] = True
        raise AssertionError("reranker should not be called when flag is off")
    monkeypatch.setattr(reranker, "rerank", fake_rerank)

    out = retrieval.retrieve("q", db=None, top_k=3)
    assert sentinel["called"] is False
    assert len(out) == 3


def test_retrieve_flag_on_invokes_reranker_and_returns_top_k(monkeypatch):
    monkeypatch.setattr(settings, "use_reranker", True)
    monkeypatch.setattr(settings, "use_query_understanding", False)

    def fake_hybrid(query, db, top_k=6, categories=None, **kwargs):
        # Over-sample expected: top_k * 5 = 15 when user asked for top_k=3.
        assert top_k == 15, f"Expected over-sample top_k=15, got {top_k}"
        return [{"id": f"c{i}", "source_file": "x", "doc_category": "d",
                 "content": f"content{i}", "chunk_index": 0, "score": 0.9 - i*0.05,
                 "parent_symbol_id": None} for i in range(15)]
    monkeypatch.setattr("app.rag.retrieval.hybrid_retrieve", fake_hybrid)
    monkeypatch.setattr(reranker, "_get_model", lambda: FakeCrossEncoder(
        score_overrides={"content7": 0.99, "content2": 0.95, "content11": 0.90}
    ))

    out = retrieval.retrieve("q", db=None, top_k=3)
    assert len(out) == 3
    assert out[0]["id"] == "c7"    # rerank pulled c7 to the front
    assert out[1]["id"] == "c2"
    assert out[2]["id"] == "c11"
    # rerank_score attached
    assert all("rerank_score" in c for c in out)


def test_retrieve_flag_on_model_unavailable_falls_back(monkeypatch):
    """When reranker model can't load, flag-on retrieve returns RRF order truncated."""
    monkeypatch.setattr(settings, "use_reranker", True)
    monkeypatch.setattr(settings, "use_query_understanding", False)
    monkeypatch.setattr(reranker, "_get_model", lambda: None)

    def fake_hybrid(query, db, top_k=6, categories=None, **kwargs):
        return [{"id": f"c{i}", "source_file": "x", "doc_category": "d",
                 "content": f"w{i}", "chunk_index": 0, "score": 0.9 - i*0.1,
                 "parent_symbol_id": None} for i in range(top_k)]
    monkeypatch.setattr("app.rag.retrieval.hybrid_retrieve", fake_hybrid)

    out = retrieval.retrieve("q", db=None, top_k=3)
    # Falls back to RRF order, top_k truncated.
    assert [c["id"] for c in out] == ["c0", "c1", "c2"]
    assert "rerank_score" not in out[0]
