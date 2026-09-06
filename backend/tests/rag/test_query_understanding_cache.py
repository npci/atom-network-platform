# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 2.2 — enriched-query LRU cache tests.

Stub `_enrich_sync_uncached` so we don't actually call an LLM. Verify
hit/miss counters, capacity eviction, and the disable-via-zero-capacity
behaviour.
"""
from __future__ import annotations

import pytest

from app.rag import query_understanding as qu
from app.rag.query_understanding import (
    EnrichedQuery,
    _reset_query_understanding_cache_for_tests,
    enrich_sync,
    query_understanding_cache_stats,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    _reset_query_understanding_cache_for_tests()
    yield
    _reset_query_understanding_cache_for_tests()


def _stub_uncached(monkeypatch, payload: dict[str, EnrichedQuery]):
    """Patch the underlying enrich_sync_uncached to return canned answers
    and count invocations."""
    calls = {"n": 0}

    def fake(q: str) -> EnrichedQuery:
        calls["n"] += 1
        return payload.get(q, EnrichedQuery(original=q, hyde_text="dummy hyde"))

    monkeypatch.setattr(qu, "_enrich_sync_uncached", fake)
    return calls


def test_cache_hit_skips_underlying_call(monkeypatch):
    calls = _stub_uncached(monkeypatch, {})
    e1 = enrich_sync("how does X work?")
    e2 = enrich_sync("how does X work?")
    e3 = enrich_sync("how does X work?")
    assert calls["n"] == 1
    assert e1.original == e2.original == e3.original == "how does X work?"
    stats = query_understanding_cache_stats()
    assert stats["hits"] >= 2
    assert stats["misses"] == 1


def test_distinct_queries_are_cached_separately(monkeypatch):
    calls = _stub_uncached(monkeypatch, {})
    enrich_sync("query A")
    enrich_sync("query B")
    enrich_sync("query A")
    enrich_sync("query B")
    assert calls["n"] == 2  # one LLM call per unique query


def test_zero_capacity_disables_cache(monkeypatch):
    calls = _stub_uncached(monkeypatch, {})
    from app.core.config import settings as _s
    monkeypatch.setattr(_s, "query_understanding_cache_size", 0, raising=False)
    enrich_sync("same query")
    enrich_sync("same query")
    enrich_sync("same query")
    # Without a cache, every call goes through.
    assert calls["n"] == 3


def test_lru_eviction_when_capacity_reached(monkeypatch):
    calls = _stub_uncached(monkeypatch, {})
    from app.core.config import settings as _s
    monkeypatch.setattr(_s, "query_understanding_cache_size", 2, raising=False)
    enrich_sync("q1")
    enrich_sync("q2")
    enrich_sync("q3")  # should evict q1 (LRU)
    enrich_sync("q1")  # miss → re-enrich
    # q1 was called twice (once initial, once after eviction), q2/q3 once.
    assert calls["n"] == 4


def test_empty_query_does_not_populate_cache(monkeypatch):
    calls = _stub_uncached(monkeypatch, {})
    enrich_sync("")
    enrich_sync("   ")
    # Empty queries short-circuit before reaching the cache or the LLM.
    assert calls["n"] == 0
    assert query_understanding_cache_stats()["size"] == 0
