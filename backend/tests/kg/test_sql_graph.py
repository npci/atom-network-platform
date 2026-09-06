# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1.5 — pure-SQL graph helpers.

We don't require Postgres for these tests — instead we stub `db.execute`
to verify the SQL is built correctly and the result rows are mapped.
"""
from __future__ import annotations

import contextlib

import pytest

try:
    from app.kg import sql_graph
except Exception as e:  # pragma: no cover
    pytest.skip(f"app.kg.sql_graph not importable: {e}", allow_module_level=True)


class _SavepointDB:
    """Base for the fake sessions below.

    Every sql_graph query runs inside `db.begin_nested()` so a failed statement
    rolls back to a SAVEPOINT instead of poisoning the caller's transaction. A
    fake that only stubs `execute` makes that call raise AttributeError, which
    the module's fail-soft handler swallows into an empty result — so the test
    would go green-to-empty and assert nothing real.
    """

    def begin_nested(self):
        return contextlib.nullcontext()


class FakeRow:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def _make_row(id_, symbol_name=None, content="body"):
    return FakeRow(
        id=id_,
        source_file=f"{id_}.py",
        doc_category="python_source",
        content=content,
        chunk_index=0,
        parent_symbol_id=None,
        symbol_name=symbol_name,
        symbol_kind="method",
        language="python",
    )


def test_expand_neighbors_empty_seeds():
    assert sql_graph.expand_neighbors(None, [], max_hops=1) == []  # type: ignore[arg-type]


def test_expand_neighbors_self_match_then_outbound(monkeypatch):
    """First call: find_chunks_by_symbol_name → returns the seed chunk.
    Second call: outbound expansion → returns a neighbour."""
    calls = []

    class FakeDB(_SavepointDB):
        def execute(self, sql, params):
            calls.append((str(sql), params))
            sql_str = str(sql)
            if "symbol_name = ANY" in sql_str:
                return FakeResult([_make_row("seed", symbol_name="Foo")])
            if "ts_rank_cd" in sql_str:
                pytest.fail("ts_rank_cd should not be invoked from sql_graph")
            # Outbound expansion query
            return FakeResult([_make_row("neighbor", symbol_name="Bar")])

    rows = sql_graph.expand_neighbors(FakeDB(), ["Foo"], max_hops=1)
    ids = {r["id"] for r in rows}
    assert "seed" in ids
    assert "neighbor" in ids


def test_inbound_callers_empty():
    assert sql_graph.inbound_callers(None, []) == []  # type: ignore[arg-type]


def test_inbound_callers_returns_callers(monkeypatch):
    class FakeDB(_SavepointDB):
        def execute(self, sql, params):
            return FakeResult([
                _make_row("caller-1"),
                _make_row("caller-2"),
            ])

    rows = sql_graph.inbound_callers(FakeDB(), ["TargetSymbol"])
    assert {r["id"] for r in rows} == {"caller-1", "caller-2"}


def test_find_chunks_by_symbol_name_dedups():
    class FakeDB(_SavepointDB):
        def execute(self, sql, params):
            return FakeResult([_make_row("a"), _make_row("a"), _make_row("b")])

    rows = sql_graph.find_chunks_by_symbol_name(FakeDB(), ["X"])
    # Underlying SQL uses ANY(:syms) — no dedup here, returns whatever
    # Postgres returned. The hydrate path dedups itself.
    assert len(rows) == 3


def test_inbound_callers_swallows_exceptions():
    class BrokenDB(_SavepointDB):
        def execute(self, sql, params):
            raise RuntimeError("boom")

    rows = sql_graph.inbound_callers(BrokenDB(), ["X"])
    assert rows == []


def test_expand_neighbors_swallows_outbound_exception():
    """If the outbound expansion raises, the self-match results still survive."""
    state = {"calls": 0}

    class FakeDB(_SavepointDB):
        def execute(self, sql, params):
            state["calls"] += 1
            if state["calls"] == 1:
                # Self-match returns one row.
                return FakeResult([_make_row("seed", symbol_name="X")])
            raise RuntimeError("outbound failed")

    rows = sql_graph.expand_neighbors(FakeDB(), ["X"])
    assert {r["id"] for r in rows} == {"seed"}
