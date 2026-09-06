# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the Slice 20 graph retriever.

Layered like prior slices:
  1. Pure seed extraction (regex + stopword filtering)
  2. Pure Cypher builders (string-shape assertions)
  3. traverse_seeds with a fake run_cypher_fn (DI)
  4. hydrate_chunks with an in-memory stub session
  5. graph_retrieve end-to-end with monkeypatched kg_client + stub DB
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.rag import graph_retriever as gr
from app.rag.graph_retriever import (
    _extract_query_seeds,
    build_describes_cypher,
    build_neighbors_cypher,
    build_self_cypher,
    graph_retrieve,
    hydrate_chunks,
    traverse_seeds,
)


# ──────────────────────────────────────────────────────────────────────────────
# Seed extraction
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractQuerySeeds:

    def test_empty_query(self):
        assert _extract_query_seeds("") == []
        assert _extract_query_seeds(None) == []

    def test_plain_english_yields_no_seeds(self):
        assert _extract_query_seeds("how does the retry work") == []

    def test_single_camel_case(self):
        assert _extract_query_seeds("what does NetworkSwitchService do?") == ["NetworkSwitchService"]

    def test_multiple_camel_cases(self):
        out = _extract_query_seeds("describe NetworkSwitchService and PaymentHandler")
        assert set(out) == {"NetworkSwitchService", "PaymentHandler"}

    def test_dotted_path_expanded(self):
        out = _extract_query_seeds("check NetworkSwitchService.processBalance logic")
        # Full dotted path + both components (CamelCase match also adds NetworkSwitchService)
        assert "NetworkSwitchService.processBalance" in out
        assert "NetworkSwitchService" in out
        assert "processBalance" in out

    def test_backticked_identifier(self):
        out = _extract_query_seeds("the `doRetry` method is buggy")
        assert "doRetry" in out

    def test_backticked_with_parens(self):
        # `doRetry()` → doRetry (parens stripped)
        out = _extract_query_seeds("call `doRetry()` from here")
        assert "doRetry" in out

    def test_backticked_dotted_split(self):
        out = _extract_query_seeds("`NetworkSwitchService.processBalance` fails")
        assert "NetworkSwitchService.processBalance" in out
        assert "NetworkSwitchService" in out
        assert "processBalance" in out

    def test_stopwords_dropped(self):
        # API / JSON are on the generic stopword list (they look symbol-ish
        # but are English acronyms in docs, not class names).
        out = _extract_query_seeds("API and JSON and XML")
        assert out == []

    def test_domain_acronyms_dropped(self):
        # The active pack's own acronyms (pack key + all-uppercase participant
        # label words — "UPI"/"NPCI"/"PSP" for the UPI pack, "NLLN"/"NLLC" for
        # the library pack) are stopwords too, whatever the pack. Derive the
        # token from the pack so this holds under any DOMAIN_PACK.
        from app.core.domain.registry import get_active_pack

        acronym = get_active_pack().key.upper()
        out = _extract_query_seeds(f"{acronym} and API and JSON")
        assert out == []

    def test_dedup_preserves_order(self):
        out = _extract_query_seeds("FooBar, then FooBar again, and BazQux")
        # FooBar appears first in the query; should be first in seeds.
        assert out.index("FooBar") < out.index("BazQux")
        assert out.count("FooBar") == 1

    def test_max_seeds_cap(self):
        query = "A1Class B2Class C3Class D4Class E5Class F6Class G7Class"
        out = _extract_query_seeds(query, max_seeds=3)
        assert len(out) == 3

    def test_single_letter_tokens_dropped(self):
        out = _extract_query_seeds("X and Y and Z")
        assert out == []

    def test_lowercase_identifier_not_matched_as_camel(self):
        # "foo" is not CamelCase and not backticked — no seed.
        out = _extract_query_seeds("the foo thing")
        assert out == []

    def test_mixed_case_acronym_kept(self):
        # "UPIService" starts upper + has a lowercase later → valid CamelCase.
        out = _extract_query_seeds("UPIService is what we need")
        assert "UPIService" in out


# ──────────────────────────────────────────────────────────────────────────────
# Cypher builders
# ──────────────────────────────────────────────────────────────────────────────

class TestCypherBuilders:

    def test_self_cypher_shape(self):
        out = build_self_cypher("Foo")
        assert "MATCH (n) WHERE n.symbol_name = 'Foo'" in out
        assert "RETURN DISTINCT n.chunk_id AS cid" in out

    def test_neighbors_cypher_undirected_per_edge_label(self):
        # AGE parser rejects pipe-union edge syntax, so we emit one query
        # per label. Verify default label (CALLS) + explicit INHERITS.
        out_default = build_neighbors_cypher("Foo")
        assert "MATCH (n) WHERE n.symbol_name = 'Foo'" in out_default
        assert "(n)-[r:CALLS]-(m)" in out_default     # undirected (no arrow)
        assert "->" not in "(n)-[r:CALLS]-(m)"

        out_inh = build_neighbors_cypher("Foo", "INHERITS")
        assert "(n)-[r:INHERITS]-(m)" in out_inh

        out_impl = build_neighbors_cypher("Foo", "IMPLEMENTS")
        assert "(n)-[r:IMPLEMENTS]-(m)" in out_impl

    def test_neighbors_cypher_rejects_unknown_label(self):
        import pytest as _pytest
        with _pytest.raises(ValueError, match="unsupported edge label"):
            build_neighbors_cypher("Foo", "BOGUS")

    def test_describes_cypher_reverse_doc_to_symbol(self):
        out = build_describes_cypher("Foo")
        assert "MATCH (n) WHERE n.symbol_name = 'Foo'" in out
        assert "MATCH (d:DocChunk)-[r:DESCRIBES]->(n)" in out
        assert "RETURN DISTINCT d.chunk_id AS cid" in out

    def test_seed_with_single_quote_escaped(self):
        out = build_self_cypher("it's")
        # escape_cypher_literal backslash-escapes single quotes.
        assert "'it\\'s'" in out

    def test_seed_with_backslash_escaped(self):
        out = build_self_cypher("a\\b")
        assert "'a\\\\b'" in out


# ──────────────────────────────────────────────────────────────────────────────
# traverse_seeds (DI)
# ──────────────────────────────────────────────────────────────────────────────

class TestTraverseSeeds:

    def test_empty_seeds_empty_result(self):
        out = traverse_seeds([], run_cypher_fn=lambda c: [])
        assert out == {}

    def test_self_match_scored_1_0(self):
        def fake(cypher: str) -> list[dict]:
            if "n.chunk_id AS cid" in cypher and "DocChunk" not in cypher and "-[r:" not in cypher:
                return [{"cid": "sym-1"}]
            return []
        out = traverse_seeds(["Foo"], run_cypher_fn=fake)
        assert out == {"sym-1": 1.0}

    def test_describes_scored_0_8(self):
        def fake(cypher: str) -> list[dict]:
            if "DocChunk" in cypher:
                return [{"cid": "doc-1"}]
            return []
        out = traverse_seeds(["Foo"], run_cypher_fn=fake)
        assert out == {"doc-1": 0.8}

    def test_neighbor_scored_0_7(self):
        def fake(cypher: str) -> list[dict]:
            if "m.chunk_id AS cid" in cypher:
                return [{"cid": "neighbor-1"}]
            return []
        out = traverse_seeds(["Foo"], run_cypher_fn=fake)
        assert out == {"neighbor-1": 0.7}

    def test_max_score_wins_when_chunk_hit_multiple_ways(self):
        # Same chunk appears via both neighbor (0.7) and describes (0.8).
        def fake(cypher: str) -> list[dict]:
            return [{"cid": "chunk-x"}]
        out = traverse_seeds(["Foo"], run_cypher_fn=fake)
        assert out == {"chunk-x": 1.0}   # self wins over describes/neighbor

    def test_multiple_seeds_accumulate(self):
        mapping = {
            "Foo": {"self": "f-self", "neighbor": "f-n"},
            "Bar": {"self": "b-self"},
        }
        def fake(cypher: str) -> list[dict]:
            for seed, row_set in mapping.items():
                if f"'{seed}'" in cypher:
                    if "m.chunk_id AS cid" in cypher and "neighbor" in row_set:
                        return [{"cid": row_set["neighbor"]}]
                    if "DocChunk" in cypher:
                        return []
                    if "self" in row_set:
                        return [{"cid": row_set["self"]}]
            return []
        out = traverse_seeds(["Foo", "Bar"], run_cypher_fn=fake)
        assert out["f-self"] == 1.0
        assert out["f-n"] == 0.7
        assert out["b-self"] == 1.0

    def test_exception_in_one_query_doesnt_kill_others(self):
        # Plan is 5 queries per seed: self, describes, CALLS, INHERITS,
        # IMPLEMENTS. Raise on the 2nd (describes); the other 4 still run.
        calls = {"n": 0}
        def fake(cypher: str) -> list[dict]:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("AGE hiccup")
            return [{"cid": f"c{calls['n']}"}]
        out = traverse_seeds(["Foo"], run_cypher_fn=fake)
        assert out["c1"] == 1.0
        assert all(out[f"c{i}"] == 0.7 for i in (3, 4, 5))
        assert "c2" not in out           # the failing query's cid never landed
        assert len(out) == 4

    def test_non_string_cid_skipped(self):
        def fake(cypher: str) -> list[dict]:
            return [{"cid": None}, {"cid": 42}, {"cid": "valid"}, {"other": "x"}]
        out = traverse_seeds(["Foo"], run_cypher_fn=fake)
        assert out == {"valid": 1.0}


# ──────────────────────────────────────────────────────────────────────────────
# hydrate_chunks
# ──────────────────────────────────────────────────────────────────────────────

class _FakeRow:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows
    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows
    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.calls = 0
    def execute(self, stmt):
        self.calls += 1
        return _FakeResult(self._rows)


class TestHydrateChunks:

    def test_empty_scored_returns_empty(self):
        out = hydrate_chunks(_FakeSession([]), {})
        assert out == []

    def test_rows_hydrated_and_sorted_by_score_desc(self):
        rows = [
            _FakeRow(id="a", source_file="x.py", doc_category="d", content="A",
                     chunk_index=0, parent_symbol_id=None),
            _FakeRow(id="b", source_file="y.py", doc_category="d", content="B",
                     chunk_index=0, parent_symbol_id=None),
            _FakeRow(id="c", source_file="z.py", doc_category="d", content="C",
                     chunk_index=0, parent_symbol_id=None),
        ]
        session = _FakeSession(rows)
        out = hydrate_chunks(session, {"a": 0.7, "b": 1.0, "c": 0.8})

        assert [r["id"] for r in out] == ["b", "c", "a"]
        assert out[0]["score"] == 1.0
        assert out[0]["graph_score"] == 1.0
        assert "content" in out[0] and "source_file" in out[0]

    def test_missing_chunk_ids_silently_dropped(self):
        # Graph returned chunk_ids that no longer exist in document_chunks.
        session = _FakeSession([])   # DB returns zero rows
        out = hydrate_chunks(session, {"gone-1": 1.0, "gone-2": 0.7})
        assert out == []

    def test_hydration_db_exception_returns_empty(self):
        class _BoomSession:
            def execute(self, stmt):
                raise RuntimeError("db down")
        out = hydrate_chunks(_BoomSession(), {"a": 1.0})
        assert out == []


# ──────────────────────────────────────────────────────────────────────────────
# graph_retrieve end-to-end (DI via monkeypatch)
# ──────────────────────────────────────────────────────────────────────────────

class TestGraphRetrieveEndToEnd:

    def test_empty_query_short_circuits(self):
        out = graph_retrieve("", MagicMock())
        assert out == []

    def test_no_seeds_short_circuits(self):
        # Pure english, no CamelCase/dotted/backticked tokens.
        out = graph_retrieve("how does the system work", MagicMock())
        assert out == []

    def test_age_unavailable_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "app.rag.graph_retriever.kg_client.is_age_available",
            lambda db: False,
        )
        out = graph_retrieve("NetworkSwitchService is broken", MagicMock())
        assert out == []

    def test_happy_path_seeds_traversal_hydration(self, monkeypatch):
        # This exercises the AGE/Cypher path; pin the backend to "age" so the
        # default graph_backend="sql" dispatch doesn't route past run_cypher.
        monkeypatch.setattr("app.core.config.settings.graph_backend", "age", raising=False)
        # Stub AGE probe → True, run_cypher returns synthetic rows, DB
        # hydration returns fake rows matching.
        monkeypatch.setattr(
            "app.rag.graph_retriever.kg_client.is_age_available",
            lambda db: True,
        )

        def fake_run_cypher(db, cypher, *, graph_name=None, return_cols=None):
            # Self match on NetworkSwitchService → chunk "s1"
            if "n.chunk_id AS cid" in cypher and "DocChunk" not in cypher and "[r:" not in cypher:
                return [{"cid": "s1"}]
            # Neighbors → "n1"
            if "m.chunk_id AS cid" in cypher:
                return [{"cid": "n1"}]
            # Describes → "d1"
            if "DocChunk" in cypher:
                return [{"cid": "d1"}]
            return []

        monkeypatch.setattr(
            "app.rag.graph_retriever.kg_client.run_cypher",
            fake_run_cypher,
        )

        hydrated_rows = [
            _FakeRow(id="s1", source_file="Svc.java", doc_category="java_source",
                     content="class body", chunk_index=0, parent_symbol_id=None),
            _FakeRow(id="n1", source_file="Svc.java", doc_category="java_source",
                     content="helper body", chunk_index=1, parent_symbol_id=None),
            _FakeRow(id="d1", source_file="design.md", doc_category="past_brd",
                     content="design notes", chunk_index=0, parent_symbol_id=None),
        ]
        session = _FakeSession(hydrated_rows)

        out = graph_retrieve("explain NetworkSwitchService please", session, top_k=10)

        ids_in_order = [r["id"] for r in out]
        # Self wins (1.0), then describes (0.8), then neighbor (0.7).
        assert ids_in_order == ["s1", "d1", "n1"]
        assert out[0]["score"] == 1.0
        assert out[1]["score"] == 0.8
        assert out[2]["score"] == 0.7

    def test_top_k_cap_applied(self, monkeypatch):
        # AGE/Cypher path — pin the backend so default graph_backend="sql"
        # doesn't route past run_cypher.
        monkeypatch.setattr("app.core.config.settings.graph_backend", "age", raising=False)
        monkeypatch.setattr(
            "app.rag.graph_retriever.kg_client.is_age_available",
            lambda db: True,
        )
        # 5 distinct chunks across queries, cap at 2.
        def fake_run_cypher(db, cypher, *, graph_name=None, return_cols=None):
            if "n.chunk_id AS cid" in cypher and "DocChunk" not in cypher and "[r:" not in cypher:
                return [{"cid": "s1"}, {"cid": "s2"}]
            if "m.chunk_id AS cid" in cypher:
                return [{"cid": "n1"}]
            if "DocChunk" in cypher:
                return [{"cid": "d1"}, {"cid": "d2"}]
            return []
        monkeypatch.setattr(
            "app.rag.graph_retriever.kg_client.run_cypher",
            fake_run_cypher,
        )

        rows = [
            _FakeRow(id=_id, source_file="f", doc_category="d", content=_id,
                     chunk_index=0, parent_symbol_id=None)
            for _id in ("s1", "s2", "n1", "d1", "d2")
        ]
        out = graph_retrieve("NetworkSwitchService", _FakeSession(rows), top_k=2)
        assert len(out) == 2
        # Top-2 must be the 1.0-scored self hits (s1, s2).
        assert {r["id"] for r in out} == {"s1", "s2"}

    def test_all_cypher_fail_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "app.rag.graph_retriever.kg_client.is_age_available",
            lambda db: True,
        )
        def boom(db, cypher, *, graph_name=None, return_cols=None):
            raise RuntimeError("AGE errored")
        monkeypatch.setattr(
            "app.rag.graph_retriever.kg_client.run_cypher", boom,
        )
        out = graph_retrieve("NetworkSwitchService", _FakeSession([]))
        assert out == []


# ──────────────────────────────────────────────────────────────────────────────
# Integration placeholder — live AGE
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.age
def test_graph_retrieve_against_live_graph():
    """Project a tiny corpus to real AGE, call graph_retrieve, clean up."""
    from app.core.database import SessionLocal
    from app.kg.client import is_age_available, run_cypher
    from app.kg.schema import initialise_graph

    db = SessionLocal()
    try:
        if not is_age_available(db):
            pytest.skip("Apache AGE not available")
        initialise_graph(db)

        # Seed the graph with a class node we can retrieve.
        run_cypher(db,
            "MERGE (n:Class {chunk_id: 'gr-test-1', symbol_name: 'GraphRetrieverSmokeClass', "
            "source_file: 'gr_smoke.java'})",
        )
        db.commit()

        # The corresponding DocumentChunk must exist for hydration to succeed.
        from app.models.document_chunk import DocumentChunk
        existing = db.get(DocumentChunk, "gr-test-1")
        created_here = False
        if existing is None:
            db.add(DocumentChunk(
                id="gr-test-1",
                source_file="gr_smoke.java",
                doc_category="java_source",
                content="class body",
                chunk_index=0,
                symbol_kind="class",
                symbol_name="GraphRetrieverSmokeClass",
            ))
            db.commit()
            created_here = True

        try:
            out = gr.graph_retrieve(
                "explain GraphRetrieverSmokeClass please",
                db, top_k=5,
            )
            assert any(r["id"] == "gr-test-1" for r in out), (
                f"expected gr-test-1 in {[r['id'] for r in out]}"
            )
        finally:
            # Cleanup — DETACH DELETE + optionally remove row we created.
            run_cypher(db,
                "MATCH (n:Class {chunk_id: 'gr-test-1'}) DETACH DELETE n",
            )
            db.commit()
            if created_here:
                db.delete(db.get(DocumentChunk, "gr-test-1"))
                db.commit()
    finally:
        db.close()
