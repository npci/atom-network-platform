# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1.3 — query-embedding LRU cache.

Cache populates on first call and serves subsequent calls without an
HTTP round-trip. Capacity is enforced via `settings.query_embedding_cache_size`.
"""
from __future__ import annotations

import pytest

try:
    from app.rag import embeddings  # noqa: F401
except Exception as e:  # pragma: no cover
    pytest.skip(f"app.rag.embeddings not importable: {e}", allow_module_level=True)


@pytest.fixture(autouse=True)
def reset_cache():
    embeddings._reset_query_embedding_cache_for_tests()
    yield
    embeddings._reset_query_embedding_cache_for_tests()


@pytest.fixture
def stub_http(monkeypatch):
    """Replace httpx client with a stub that records POSTs and returns a
    deterministic embedding."""
    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"embeddings": [[0.42] * embeddings.EMBEDDING_DIM]}

    class FakeClient:
        def post(self, url, json):
            calls["n"] += 1
            return FakeResp()

    monkeypatch.setattr(embeddings, "_get_client", lambda: FakeClient())
    return calls


def test_first_call_hits_http(stub_http, monkeypatch):
    monkeypatch.setattr(embeddings.settings, "query_embedding_cache_size", 32)
    vec = embeddings.embed_query("hello world")
    assert len(vec) == embeddings.EMBEDDING_DIM
    assert stub_http["n"] == 1


def test_repeat_call_uses_cache(stub_http, monkeypatch):
    monkeypatch.setattr(embeddings.settings, "query_embedding_cache_size", 32)
    embeddings.embed_query("hello world")
    embeddings.embed_query("hello world")
    embeddings.embed_query("hello world")
    assert stub_http["n"] == 1
    stats = embeddings.query_embedding_cache_stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1


def test_disabled_cache_always_hits_http(stub_http, monkeypatch):
    monkeypatch.setattr(embeddings.settings, "query_embedding_cache_size", 0)
    embeddings.embed_query("a")
    embeddings.embed_query("a")
    assert stub_http["n"] == 2


def test_lru_eviction(stub_http, monkeypatch):
    monkeypatch.setattr(embeddings.settings, "query_embedding_cache_size", 2)
    embeddings.embed_query("q1")  # miss → cache: [q1]
    embeddings.embed_query("q2")  # miss → cache: [q1, q2]
    embeddings.embed_query("q3")  # miss → cache: [q2, q3] (q1 evicted)
    embeddings.embed_query("q1")  # miss again because q1 was evicted
    assert stub_http["n"] == 4


def test_returns_defensive_copy(stub_http, monkeypatch):
    """Mutating the returned vector must not poison the cache."""
    monkeypatch.setattr(embeddings.settings, "query_embedding_cache_size", 8)
    v1 = embeddings.embed_query("immutable")
    v1[0] = 999.0  # mutate the caller's copy
    v2 = embeddings.embed_query("immutable")
    assert v2[0] == 0.42
