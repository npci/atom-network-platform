# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 2.1 — parallel multi-pass variant tests.

Stub `hybrid_retrieve` and `SessionLocal` so we can verify:
  - all variants execute
  - each task gets its own session
  - per-variant exceptions don't break the overall ranking
  - serial mode still works when the flag is off

These tests skip at import time when sqlalchemy is unavailable in the
sandbox; they run cleanly in CI.
"""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("sqlalchemy")

from app.rag import retrieval as rt


class _StubSession:
    """Pretends to be a SessionLocal() instance."""
    _counter = 0
    _lock = threading.Lock()

    def __init__(self):
        with _StubSession._lock:
            _StubSession._counter += 1
            self.idx = _StubSession._counter
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_stub_counter():
    _StubSession._counter = 0
    yield


def _patch_pipeline(monkeypatch, fake_retrieve):
    """Patch SessionLocal and hybrid_retrieve so the parallel path runs
    without a real DB."""
    monkeypatch.setattr(
        "app.core.database.SessionLocal", _StubSession, raising=False,
    )
    monkeypatch.setattr(rt, "hybrid_retrieve", fake_retrieve, raising=True)


def test_parallel_runs_all_variants(monkeypatch):
    seen_variants: list[str] = []
    seen_sessions: list[int] = []
    lock = threading.Lock()

    def fake_retrieve(query, db, top_k, categories):
        with lock:
            seen_variants.append(query)
            seen_sessions.append(db.idx)
        return [{"id": f"chunk-{query[:5]}", "score": 1.0}]

    _patch_pipeline(monkeypatch, fake_retrieve)
    out = rt._run_variants_parallel(["v1", "v2", "v3"], 5, None)
    assert len(out) == 3
    assert sorted(seen_variants) == ["v1", "v2", "v3"]
    # Each task got its own session — distinct ids.
    assert len(set(seen_sessions)) == 3


def test_parallel_swallows_per_variant_exceptions(monkeypatch):
    def fake_retrieve(query, db, top_k, categories):
        if query == "boom":
            raise RuntimeError("synthetic")
        return [{"id": f"chunk-{query}", "score": 1.0}]

    _patch_pipeline(monkeypatch, fake_retrieve)
    out = rt._run_variants_parallel(["good1", "boom", "good2"], 5, None)
    # Only the two surviving variants come back.
    assert len(out) == 2
    flat_ids = [r["id"] for ranking in out for r in ranking]
    assert "chunk-boom" not in flat_ids


def test_parallel_returns_empty_when_session_local_unavailable(monkeypatch):
    # Force the import inside _run_variants_parallel to fail by patching
    # the sys.modules entry so attribute lookup raises.
    import sys
    # Drop the database module from sys.modules and prevent reimport.
    monkeypatch.setitem(sys.modules, "app.core.database", None)
    out = rt._run_variants_parallel(["v1", "v2"], 5, None)
    # The helper falls open — caller sees empty rankings → final raw-query path.
    assert out == []


def test_serial_still_works_when_flag_off(monkeypatch):
    seen = []

    def fake_retrieve(query, db, top_k, categories):
        seen.append(query)
        return [{"id": f"c-{query}", "score": 1.0}]

    monkeypatch.setattr(rt, "hybrid_retrieve", fake_retrieve, raising=True)
    out = rt._run_variants_serial(["a", "b", "c"], db=object(), per_pass_k=5, categories=None)
    assert len(out) == 3
    assert seen == ["a", "b", "c"]
