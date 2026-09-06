# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for sub-slice 20b — graph_retrieve fusion inside retrieve().

The graph pass sits between primary retrieval (hybrid / multi-pass) and
the reranker. Guarded by `settings.use_graph_retrieval`. Fail-open: any
exception or empty graph result leaves the primary ranking untouched.

We stub out `hybrid_retrieve`, `graph_retrieve`, and the reranker so the
tests stay pure and run in milliseconds — they exercise the fusion logic
in `retrieve()`, not the underlying retrieval engines.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.rag import retrieval


# ──────────────────────────────────────────────────────────────────────────────
# Shared stubs
# ──────────────────────────────────────────────────────────────────────────────

def _mk_chunk(cid: str, score: float = 0.9) -> dict:
    """Chunk dict in the shape hybrid_retrieve / graph_retrieve produce."""
    return {
        "id":                cid,
        "source_file":       f"{cid}.py",
        "doc_category":      "d",
        "content":           cid,
        "chunk_index":       0,
        "score":             score,
        "parent_symbol_id":  None,
    }


@pytest.fixture(autouse=True)
def _disable_query_understanding_and_reranker(monkeypatch):
    """Default each test to the single-pass hybrid path (no QU, no reranker).

    Individual tests can override via their own monkeypatches.
    """
    monkeypatch.setattr(settings, "use_query_understanding", False)
    monkeypatch.setattr(settings, "use_reranker", False)


# ──────────────────────────────────────────────────────────────────────────────
# Flag-off behaviour: no graph call
# ──────────────────────────────────────────────────────────────────────────────

class TestFlagOff:

    def test_graph_retrieve_not_called_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(settings, "use_graph_retrieval", False)

        called = {"hybrid": 0, "graph": 0}

        def fake_hybrid(*a, **kw):
            called["hybrid"] += 1
            return [_mk_chunk("h1"), _mk_chunk("h2")]

        def fake_graph_retrieve(*a, **kw):
            called["graph"] += 1
            return [_mk_chunk("g1")]

        monkeypatch.setattr("app.rag.retrieval.hybrid_retrieve", fake_hybrid)
        monkeypatch.setattr(
            "app.rag.graph_retriever.graph_retrieve", fake_graph_retrieve,
        )

        out = retrieval.retrieve("q", db=None, top_k=3)

        assert called["hybrid"] == 1
        assert called["graph"] == 0
        assert [c["id"] for c in out] == ["h1", "h2"]


# ──────────────────────────────────────────────────────────────────────────────
# Flag-on behaviour: graph fuses with primary
# ──────────────────────────────────────────────────────────────────────────────

class TestFlagOnFusion:

    def test_graph_chunks_appear_in_fused_output(self, monkeypatch):
        monkeypatch.setattr(settings, "use_graph_retrieval", True)

        monkeypatch.setattr(
            "app.rag.retrieval.hybrid_retrieve",
            lambda *a, **kw: [_mk_chunk("h1"), _mk_chunk("h2")],
        )
        monkeypatch.setattr(
            "app.rag.graph_retriever.graph_retrieve",
            lambda *a, **kw: [_mk_chunk("g1"), _mk_chunk("g2")],
        )

        out = retrieval.retrieve("q", db=None, top_k=10)
        ids = {c["id"] for c in out}
        assert ids == {"h1", "h2", "g1", "g2"}

    def test_chunk_in_both_passes_ranks_higher_than_singleton(self, monkeypatch):
        """Fusion over two rankings where `h1` is rank 1 in both:
             hybrid: h1, h2, h3
             graph:  h1, g1, g2
        h1 RRF score > any of h2/h3/g1/g2 → it ranks first."""
        monkeypatch.setattr(settings, "use_graph_retrieval", True)
        monkeypatch.setattr(
            "app.rag.retrieval.hybrid_retrieve",
            lambda *a, **kw: [_mk_chunk("h1"), _mk_chunk("h2"), _mk_chunk("h3")],
        )
        monkeypatch.setattr(
            "app.rag.graph_retriever.graph_retrieve",
            lambda *a, **kw: [_mk_chunk("h1"), _mk_chunk("g1"), _mk_chunk("g2")],
        )

        out = retrieval.retrieve("q", db=None, top_k=5)
        assert out[0]["id"] == "h1"

    def test_empty_graph_result_leaves_primary_unchanged(self, monkeypatch):
        """Graph returns [] → fusion block skipped entirely."""
        monkeypatch.setattr(settings, "use_graph_retrieval", True)
        monkeypatch.setattr(
            "app.rag.retrieval.hybrid_retrieve",
            lambda *a, **kw: [_mk_chunk("h1"), _mk_chunk("h2")],
        )
        monkeypatch.setattr(
            "app.rag.graph_retriever.graph_retrieve", lambda *a, **kw: [],
        )

        out = retrieval.retrieve("q", db=None, top_k=3)
        assert [c["id"] for c in out] == ["h1", "h2"]

    def test_graph_exception_falls_back_to_primary(self, monkeypatch):
        """Graph-retrieve raising is caught + logged; primary wins."""
        monkeypatch.setattr(settings, "use_graph_retrieval", True)
        monkeypatch.setattr(
            "app.rag.retrieval.hybrid_retrieve",
            lambda *a, **kw: [_mk_chunk("h1"), _mk_chunk("h2")],
        )

        def boom(*a, **kw):
            raise RuntimeError("graph_retrieve errored")

        monkeypatch.setattr(
            "app.rag.graph_retriever.graph_retrieve", boom,
        )

        out = retrieval.retrieve("q", db=None, top_k=3)
        assert [c["id"] for c in out] == ["h1", "h2"]

    def test_fusion_applied_with_multi_pass_primary(self, monkeypatch):
        """QU on → primary is multi-pass; graph still fuses on top."""
        monkeypatch.setattr(settings, "use_query_understanding", True)
        monkeypatch.setattr(settings, "use_graph_retrieval", True)

        # Stub enrich_sync to yield one sub-question.
        from app.rag import query_understanding
        monkeypatch.setattr(
            query_understanding, "enrich_sync",
            lambda q: query_understanding.EnrichedQuery(
                original=q, sub_questions=["sub"],
            ),
        )

        # Hybrid returns 2 chunks regardless of variant.
        monkeypatch.setattr(
            "app.rag.retrieval.hybrid_retrieve",
            lambda *a, **kw: [_mk_chunk("h1"), _mk_chunk("h2")],
        )
        monkeypatch.setattr(
            "app.rag.graph_retriever.graph_retrieve",
            lambda *a, **kw: [_mk_chunk("g1")],
        )

        out = retrieval.retrieve("q", db=None, top_k=5)
        ids = {c["id"] for c in out}
        assert "g1" in ids          # graph pass contributed
        assert "h1" in ids and "h2" in ids


# ──────────────────────────────────────────────────────────────────────────────
# Fusion + reranker interaction
# ──────────────────────────────────────────────────────────────────────────────

class TestFusionPlusReranker:

    def test_reranker_sees_fused_pool(self, monkeypatch):
        """The reranker input should include both hybrid AND graph chunks."""
        monkeypatch.setattr(settings, "use_graph_retrieval", True)
        monkeypatch.setattr(settings, "use_reranker", True)

        monkeypatch.setattr(
            "app.rag.retrieval.hybrid_retrieve",
            lambda *a, **kw: [_mk_chunk("h1"), _mk_chunk("h2")],
        )
        monkeypatch.setattr(
            "app.rag.graph_retriever.graph_retrieve",
            lambda *a, **kw: [_mk_chunk("g1")],
        )

        captured_pool: list[dict] = []
        def fake_rerank(query, results, top_k):
            captured_pool[:] = list(results)
            return results[:top_k]

        # Patch the module's `rerank` attribute directly — avoids sys.modules
        # games and survives the full-suite test-order.
        from app.rag import reranker as _reranker_mod
        monkeypatch.setattr(_reranker_mod, "rerank", fake_rerank)

        out = retrieval.retrieve("q", db=None, top_k=2)
        ids_in_pool = {c["id"] for c in captured_pool}
        assert ids_in_pool == {"h1", "h2", "g1"}
        assert len(out) == 2
