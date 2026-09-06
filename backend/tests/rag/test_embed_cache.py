# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 3.2 — embedding cache tests.

The pure-Python pieces (sha_for, _vector_to_pgliteral, _parse_pgvector) plus
the high-level `embed_chunks_with_cache` wrapper with `embed_texts`
monkey-patched and `get_many`/`put_many` stubbed in-memory.
"""
from __future__ import annotations

import pytest

from app.rag import embed_cache


def _stub_in_memory_cache(monkeypatch):
    """Replace get_many/put_many with a process-local dict so we don't need a
    DB. Returns the underlying dict so the test can inspect it."""
    storage: dict[tuple[str, str, str], list[float]] = {}

    def fake_get_many(db, shas, model, view_kind=""):
        out: dict[str, list[float]] = {}
        for sha in shas:
            v = storage.get((sha, model, view_kind or ""))
            if v is not None:
                out[sha] = v
        return out

    def fake_put_many(db, items, model):
        written = 0
        for sha, vec, view_kind in items:
            key = (sha, model, view_kind or "")
            if not any(abs(float(v)) > 1e-9 for v in vec):
                continue
            if key not in storage:
                storage[key] = list(vec)
                written += 1
        return written

    monkeypatch.setattr(embed_cache, "get_many", fake_get_many)
    monkeypatch.setattr(embed_cache, "put_many", fake_put_many)
    return storage


@pytest.fixture(autouse=True)
def _reset_counters():
    embed_cache._reset_stats_for_tests()
    yield
    embed_cache._reset_stats_for_tests()


def test_sha_for_is_stable_and_distinct():
    a = embed_cache.sha_for("hello world")
    b = embed_cache.sha_for("hello world")
    c = embed_cache.sha_for("hello world!")
    assert a == b
    assert a != c
    assert len(a) == 64  # hex digest


def test_pgvector_roundtrip():
    lit = embed_cache._vector_to_pgliteral([0.1, -0.2, 3.5])
    parsed = embed_cache._parse_pgvector(lit)
    assert parsed == [0.1, -0.2, 3.5]


def test_embed_chunks_with_cache_full_miss(monkeypatch):
    storage = _stub_in_memory_cache(monkeypatch)
    # Stub embed_texts so we don't need Ollama.
    embed_calls = {"n": 0, "inputs": []}

    def fake_embed_texts(texts):
        embed_calls["n"] += 1
        embed_calls["inputs"].extend(texts)
        return [[1.0, 0.0] for _ in texts]

    from app.rag import embeddings
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(embeddings, "EMBEDDING_DIM", 2, raising=False)
    monkeypatch.setattr(embeddings, "EMBEDDING_MODEL", "test-model", raising=False)

    chunks = [
        {"content": "a"},
        {"content": "b"},
        {"content": "c"},
    ]
    vecs = embed_cache.embed_chunks_with_cache(db=None, chunks=chunks)
    assert len(vecs) == 3
    assert embed_calls["n"] == 1
    assert embed_calls["inputs"] == ["a", "b", "c"]
    # All three should now be in the cache for the next run.
    assert len(storage) == 3


def test_embed_chunks_with_cache_full_hit(monkeypatch):
    storage = _stub_in_memory_cache(monkeypatch)
    from app.rag import embeddings
    monkeypatch.setattr(embeddings, "EMBEDDING_DIM", 2, raising=False)
    monkeypatch.setattr(embeddings, "EMBEDDING_MODEL", "test-model", raising=False)
    # Pre-populate the cache.
    storage[(embed_cache.sha_for("a"), "test-model", "")] = [9.0, 9.0]
    storage[(embed_cache.sha_for("b"), "test-model", "")] = [8.0, 8.0]

    embed_calls = {"n": 0}
    def fake_embed_texts(texts):
        embed_calls["n"] += 1
        return [[0.0, 0.0]] * len(texts)
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed_texts)

    vecs = embed_cache.embed_chunks_with_cache(db=None, chunks=[{"content": "a"}, {"content": "b"}])
    assert vecs == [[9.0, 9.0], [8.0, 8.0]]
    assert embed_calls["n"] == 0  # no embedding round-trip


def test_embed_chunks_with_cache_partial_hit(monkeypatch):
    storage = _stub_in_memory_cache(monkeypatch)
    from app.rag import embeddings
    monkeypatch.setattr(embeddings, "EMBEDDING_DIM", 2, raising=False)
    monkeypatch.setattr(embeddings, "EMBEDDING_MODEL", "test-model", raising=False)
    storage[(embed_cache.sha_for("hit"), "test-model", "")] = [1.0, 1.0]

    embed_calls = {"n": 0, "inputs": []}
    def fake_embed_texts(texts):
        embed_calls["n"] += 1
        embed_calls["inputs"].extend(texts)
        return [[2.0, 2.0] for _ in texts]
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed_texts)

    vecs = embed_cache.embed_chunks_with_cache(
        db=None,
        chunks=[
            {"content": "hit"},     # cache hit
            {"content": "miss-1"},  # miss
            {"content": "hit"},     # same as first — still one cache hit
            {"content": "miss-2"},
        ],
    )
    assert vecs[0] == [1.0, 1.0]
    assert vecs[2] == [1.0, 1.0]
    # The two distinct misses get embedded.
    assert sorted(embed_calls["inputs"]) == ["miss-1", "miss-2"]


def test_embed_chunks_with_cache_view_kind_isolation(monkeypatch):
    """Same sha under different view_kinds must NOT collide."""
    storage = _stub_in_memory_cache(monkeypatch)
    from app.rag import embeddings
    monkeypatch.setattr(embeddings, "EMBEDDING_DIM", 2, raising=False)
    monkeypatch.setattr(embeddings, "EMBEDDING_MODEL", "test-model", raising=False)

    embed_calls = {"n": 0, "inputs": []}
    def fake_embed_texts(texts):
        embed_calls["n"] += 1
        embed_calls["inputs"].extend(texts)
        return [[3.0, 3.0] for _ in texts]
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed_texts)

    # Pre-cache only the body view.
    storage[(embed_cache.sha_for("foo"), "test-model", "body")] = [7.0, 7.0]

    vecs = embed_cache.embed_chunks_with_cache(
        db=None,
        chunks=[
            {"content": "foo", "view_kind": "body"},      # hit
            {"content": "foo", "view_kind": "signature"}, # miss — different key
        ],
    )
    assert vecs[0] == [7.0, 7.0]
    assert vecs[1] == [3.0, 3.0]
    assert embed_calls["inputs"] == ["foo"]  # signature view embedded once


def test_embed_chunks_with_cache_skips_zero_vectors(monkeypatch):
    """Hard-failure zero vectors must not be persisted to the cache."""
    storage = _stub_in_memory_cache(monkeypatch)
    from app.rag import embeddings
    monkeypatch.setattr(embeddings, "EMBEDDING_DIM", 2, raising=False)
    monkeypatch.setattr(embeddings, "EMBEDDING_MODEL", "test-model", raising=False)

    def fake_embed_texts(texts):
        return [[0.0, 0.0] for _ in texts]
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed_texts)

    embed_cache.embed_chunks_with_cache(db=None, chunks=[{"content": "x"}])
    assert len(storage) == 0
