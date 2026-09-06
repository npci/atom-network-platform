# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 5 query understanding + multi-pass retrieval orchestration.

Pure tests — no DB, no real LLM. `call_llm` is monkeypatched; `hybrid_retrieve`
is monkeypatched for the multi-pass orchestration test.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.rag import query_understanding, retrieval


# ──────────────────────────────────────────────────────────────────────────────
# EnrichedQuery.variants_for_retrieval
# ──────────────────────────────────────────────────────────────────────────────

def test_variants_original_only_when_empty_enrichment():
    eq = query_understanding.EnrichedQuery(original="How does the network work?")
    assert eq.variants_for_retrieval() == ["How does the network work?"]


def test_variants_deduplicates_preserves_order():
    eq = query_understanding.EnrichedQuery(
        original="What is the network?",
        sub_questions=["What is the network?", "How does it work?"],  # first duplicates original
        hyde_text="the network is a real-time payment rail.",
    )
    variants = eq.variants_for_retrieval()
    # Order: original, hyde, then unique sub-questions
    assert variants == [
        "What is the network?",
        "the network is a real-time payment rail.",
        "How does it work?",
    ]


def test_variants_caps_sub_questions_at_3():
    eq = query_understanding.EnrichedQuery(
        original="q",
        sub_questions=[f"sub {i}" for i in range(10)],
    )
    assert len(eq.variants_for_retrieval()) == 1 + query_understanding.MAX_SUB_QUESTIONS


# ──────────────────────────────────────────────────────────────────────────────
# enrich — happy path
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enrich_parses_structured_json(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=500, **kwargs):
        return json.dumps({
            "sub_questions": ["What is a mandate?", "How is revocation handled?"],
            "entities": ["the network AutoPay", "emandate"],
            "hypothetical_answer": "Mandate revocation requires PSP to call the deregistration API with the unique mandate reference number.",
        })

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    eq = await query_understanding.enrich("How does mandate revocation work?")
    assert eq.original == "How does mandate revocation work?"
    assert eq.sub_questions == ["What is a mandate?", "How is revocation handled?"]
    assert eq.entities == ["the network AutoPay", "emandate"]
    assert "revocation" in eq.hyde_text.lower() or "mandate" in eq.hyde_text.lower()


@pytest.mark.asyncio
async def test_enrich_tolerates_markdown_fenced_json(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=500, **kwargs):
        return "```json\n" + json.dumps({
            "sub_questions": [],
            "entities": ["NPCI"],
            "hypothetical_answer": "the network is an authority-run payment rail.",
        }) + "\n```"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    eq = await query_understanding.enrich("What is the network?")
    assert eq.entities == ["NPCI"]
    assert eq.hyde_text.startswith("the network is")


# ──────────────────────────────────────────────────────────────────────────────
# enrich — fail-open paths
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enrich_llm_exception_falls_back(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=500, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    eq = await query_understanding.enrich("any query")
    assert eq.original == "any query"
    assert eq.sub_questions == []
    assert eq.entities == []
    assert eq.hyde_text == ""


@pytest.mark.asyncio
async def test_enrich_bad_json_falls_back(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=500, **kwargs):
        return "this is not JSON at all"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    eq = await query_understanding.enrich("q")
    assert eq.sub_questions == []
    assert eq.entities == []
    assert eq.hyde_text == ""


@pytest.mark.asyncio
async def test_enrich_partial_json_keeps_what_parsed(monkeypatch):
    """Missing fields produce empty values, not fallback of the whole object."""
    async def fake_call_llm(system, messages, max_tokens=500, **kwargs):
        return json.dumps({
            "sub_questions": ["only sub q"],
            # "entities" missing
            # "hypothetical_answer" missing
        })

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    eq = await query_understanding.enrich("q")
    assert eq.sub_questions == ["only sub q"]
    assert eq.entities == []
    assert eq.hyde_text == ""


@pytest.mark.asyncio
async def test_enrich_empty_input_short_circuits(monkeypatch):
    called = {"n": 0}

    async def fake_call_llm(system, messages, max_tokens=500, **kwargs):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    eq = await query_understanding.enrich("")
    assert eq.original == ""
    assert called["n"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# retrieve() multi-pass orchestration
# ──────────────────────────────────────────────────────────────────────────────

def test_retrieve_flag_off_single_pass(monkeypatch):
    """Flag OFF → behaviour byte-identical to direct hybrid_retrieve."""
    monkeypatch.setattr(settings, "use_query_understanding", False)
    called = {"n": 0, "queries": []}

    def fake_hybrid(query, db, top_k=6, categories=None, **kwargs):
        called["n"] += 1
        called["queries"].append(query)
        return [{"id": f"c{called['n']}", "source_file": "x", "doc_category": "d",
                 "content": query, "chunk_index": 0, "score": 0.9}]

    monkeypatch.setattr("app.rag.retrieval.hybrid_retrieve", fake_hybrid)

    out = retrieval.retrieve("How does the network work?", db=None, top_k=3)
    assert called["n"] == 1
    assert called["queries"] == ["How does the network work?"]
    assert len(out) == 1


def test_retrieve_flag_on_multi_pass(monkeypatch):
    """Flag ON → enrich + per-variant hybrid_retrieve + RRF fusion."""
    monkeypatch.setattr(settings, "use_query_understanding", True)
    # Isolate RRF fusion from the Slice 6 reranker — when enabled via env
    # the cross-encoder reorders the synthetic chunks and breaks the ordering
    # assertion below.
    monkeypatch.setattr(settings, "use_reranker", False)

    # Stub enrich_sync to avoid any LLM involvement
    def fake_enrich_sync(query):
        return query_understanding.EnrichedQuery(
            original=query,
            sub_questions=["sub A"],
            hyde_text="hypothetical answer",
        )
    monkeypatch.setattr(query_understanding, "enrich_sync", fake_enrich_sync)

    # Stub hybrid_retrieve — returns 3 synthetic chunks per pass with
    # different ids so we can observe fusion
    def fake_hybrid(query, db, top_k=6, categories=None, **kwargs):
        base = {
            "raw":            [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "hyde":           [{"id": "b"}, {"id": "a"}, {"id": "d"}],
            "sub A":          [{"id": "a"}, {"id": "e"}, {"id": "f"}],
        }
        # Match on a few characters of the variant
        if query.startswith("hypothetical"):
            rows = base["hyde"]
        elif query == "sub A":
            rows = base["sub A"]
        else:
            rows = base["raw"]
        return [{"id": r["id"], "source_file": "x", "doc_category": "d",
                 "content": r["id"], "chunk_index": 0, "score": 0.9,
                 "parent_symbol_id": None} for r in rows]

    monkeypatch.setattr("app.rag.retrieval.hybrid_retrieve", fake_hybrid)

    out = retrieval.retrieve("the original", db=None, top_k=3)

    # `a` appears in all 3 passes, so its fused RRF should rank highest.
    assert out[0]["id"] == "a"
    # `b` appears in 2 passes → second
    assert out[1]["id"] == "b"
    # Final list capped at top_k
    assert len(out) == 3


def test_retrieve_flag_on_dedups_by_parent_symbol(monkeypatch):
    """When the same symbol (via different views) ranks in multiple passes,
    retrieve() must collapse them to one best-scored row."""
    monkeypatch.setattr(settings, "use_query_understanding", True)

    monkeypatch.setattr(
        query_understanding,
        "enrich_sync",
        lambda q: query_understanding.EnrichedQuery(
            original=q, hyde_text="hyde text",
        ),
    )

    def fake_hybrid(query, db, top_k=6, categories=None, **kwargs):
        # Two rows per symbol "sym1" (different views), one row for "sym2".
        return [
            {"id": "body1", "parent_symbol_id": "sym1", "source_file": "f",
             "doc_category": "d", "content": "body", "chunk_index": 0, "score": 0.9},
            {"id": "sig1",  "parent_symbol_id": "sym1", "source_file": "f",
             "doc_category": "d", "content": "sig",  "chunk_index": 0, "score": 0.8},
            {"id": "sym2",  "parent_symbol_id": "sym2", "source_file": "g",
             "doc_category": "d", "content": "x",    "chunk_index": 0, "score": 0.7},
        ]

    monkeypatch.setattr("app.rag.retrieval.hybrid_retrieve", fake_hybrid)

    out = retrieval.retrieve("q", db=None, top_k=5)
    parent_ids = [c.get("parent_symbol_id") for c in out]
    # sym1 appears exactly once (dedup), sym2 also once
    assert parent_ids.count("sym1") == 1
    assert parent_ids.count("sym2") == 1


def test_retrieve_flag_on_variant_failure_skipped(monkeypatch):
    """A single variant's hybrid_retrieve throwing must not break the pass."""
    monkeypatch.setattr(settings, "use_query_understanding", True)

    monkeypatch.setattr(
        query_understanding,
        "enrich_sync",
        lambda q: query_understanding.EnrichedQuery(
            original=q, hyde_text="hyde text",
        ),
    )

    def fake_hybrid(query, db, top_k=6, categories=None, **kwargs):
        if query.startswith("hyde"):
            raise RuntimeError("simulated per-variant failure")
        return [{"id": "raw-only", "parent_symbol_id": None, "source_file": "x",
                 "doc_category": "d", "content": "r", "chunk_index": 0, "score": 0.5}]

    monkeypatch.setattr("app.rag.retrieval.hybrid_retrieve", fake_hybrid)

    out = retrieval.retrieve("q", db=None, top_k=3)
    # We get back the successful-pass result
    assert len(out) == 1
    assert out[0]["id"] == "raw-only"
