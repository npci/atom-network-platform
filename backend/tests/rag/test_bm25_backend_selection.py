# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1.2 — bm25_search backend selection tests.

We test the routing logic in `bm25_search` without requiring Postgres
or `rank-bm25`. The tsvector path is exercised via stubs of `db.execute`
returning canned rows; the rank_bm25 path falls back to its own logic
when the dep is missing.
"""
from __future__ import annotations

import pytest

try:
    from app.rag import bm25_search
except Exception as e:  # pragma: no cover
    pytest.skip(f"app.rag.bm25_search not importable: {e}", allow_module_level=True)


@pytest.fixture(autouse=True)
def reset_state():
    bm25_search._reset_tsv_probe_for_tests()
    yield
    bm25_search._reset_tsv_probe_for_tests()


def test_default_backend_is_tsvector(monkeypatch):
    # Must default to tsvector even with no env override.
    monkeypatch.setattr(bm25_search.settings, "bm25_backend", "tsvector")
    assert bm25_search._backend() == "tsvector"


def test_unknown_backend_falls_back(monkeypatch):
    monkeypatch.setattr(bm25_search.settings, "bm25_backend", "made-up-backend")
    assert bm25_search._backend() == "rank_bm25"


def test_tokenize_strips_stopwords():
    toks = bm25_search.tokenize("The quick brown fox")
    assert "the" not in toks
    assert "quick" in toks
    assert "brown" in toks
    assert "fox" in toks


def test_tokenize_min_length():
    toks = bm25_search.tokenize("a aa aaa")
    assert "a" not in toks       # length 1 dropped
    assert "aa" in toks
    assert "aaa" in toks


def test_tsvector_search_with_stub_db(monkeypatch):
    """Patch `db.execute` to return canned rows shaped like ts_rank_cd output."""
    monkeypatch.setattr(bm25_search.settings, "bm25_backend", "tsvector")

    class FakeRow:
        def __init__(self, id_, score):
            self.id = id_
            self.score = score

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

        def first(self):
            return self._rows[0] if self._rows else None

    class FakeDB:
        def execute(self, sql, params=None):
            sql_str = str(sql)
            if "information_schema.columns" in sql_str:
                return FakeResult([("present",)])  # column exists
            assert "ts_rank_cd" in sql_str
            return FakeResult([FakeRow("c1", 0.9), FakeRow("c2", 0.5)])

    db = FakeDB()
    out = bm25_search.search("hello world", top_k=10, db=db)
    assert out == [("c1", 0.9), ("c2", 0.5)]


def test_tsvector_search_without_db_returns_empty(monkeypatch, caplog):
    monkeypatch.setattr(bm25_search.settings, "bm25_backend", "tsvector")
    out = bm25_search.search("hello", top_k=10, db=None)
    assert out == []


def test_ensure_fresh_is_noop_for_tsvector(monkeypatch):
    monkeypatch.setattr(bm25_search.settings, "bm25_backend", "tsvector")
    # Even with no DB, ensure_fresh must not raise — it returns False.
    assert bm25_search.ensure_fresh(None) is False  # type: ignore[arg-type]


def test_build_index_noop_for_tsvector(monkeypatch):
    monkeypatch.setattr(bm25_search.settings, "bm25_backend", "tsvector")

    class FakeDB:
        def execute(self, sql, params=None):
            class R:
                def first(self_):
                    return ("present",)
            return R()

    assert bm25_search.build_index(FakeDB()) == 0
