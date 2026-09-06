# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Characterization tests for the RAG retrieval surface.

Slice 1 (Phase 3 backlog). These tests lock *current* behavior of retrieval
constants, `build_context` output format, the UPI taxonomy schema, BM25
tokenization, and the embedding dimension. They intentionally stay pure — no
database, no LLM, no network — so they run in milliseconds and catch silent
drift on future changes.

If any test here starts failing, either the constant/behavior changed on
purpose (update the test) or something regressed.
"""
from __future__ import annotations

from pathlib import Path

from app.agents.taxonomy import get_taxonomy
from app.rag import bm25_search, hybrid_search
from app.rag.embeddings import EMBEDDING_DIM
from app.rag.retrieval import build_context


# ── 1. Retrieval tunable constants ───────────────────────────────────────────

def test_hybrid_retrieve_tunable_constants():
    """Lock the RRF + candidate-size knobs. Changing any of these is a
    measurable behavior change — a failing assertion here forces an
    intentional update + eval-harness rerun."""
    assert hybrid_search.RRF_K == 60, (
        "RRF_K must stay 60 (Cormack et al. 2009 recommendation); if you change "
        "it, re-measure retrieval recall@10 first."
    )
    assert hybrid_search.DENSE_CANDIDATES == 10
    assert hybrid_search.SPARSE_CANDIDATES == 10
    assert hybrid_search.DEFAULT_TOP_K == 6
    assert hybrid_search.MIN_OVERLAP_TOKENS == 2


# ── 2. build_context output format ───────────────────────────────────────────

def test_build_context_header_format():
    """`build_context` must emit `[{doc_category} | {source_file}]` header per
    chunk, chunk bodies separated by `\\n---\\n`. Downstream agent prompts rely
    on this exact format for citation parsing."""
    chunks = [
        {
            "doc_category": "rbi_guideline",
            "source_file": "rbi/upi_master_direction.pdf",
            "content": "Payment Systems Operator shall ensure...",
        },
        {
            "doc_category": "upi_product_doc",
            "source_file": "npci/upi_handbook_v3.md",
            "content": "The UPI architecture consists of three roles...",
        },
    ]
    ctx = build_context(chunks, max_tokens=4000)

    # Both headers present, in chunk order
    assert "[rbi_guideline | rbi/upi_master_direction.pdf]" in ctx
    assert "[upi_product_doc | npci/upi_handbook_v3.md]" in ctx
    assert ctx.index("[rbi_guideline") < ctx.index("[upi_product_doc")

    # Chunks separated by the `\n---\n` delimiter
    assert "\n---\n" in ctx

    # Body content preserved
    assert "Payment Systems Operator" in ctx
    assert "three roles" in ctx


def test_build_context_honors_token_budget():
    """When the assembled context exceeds `max_tokens * 4` chars, later chunks
    are dropped (not truncated mid-chunk)."""
    big_chunk = {
        "doc_category": "upi_product_doc",
        "source_file": "big.md",
        "content": "x" * 5000,
    }
    second_chunk = {
        "doc_category": "upi_product_doc",
        "source_file": "second.md",
        "content": "SECOND CHUNK MARKER",
    }
    # max_tokens=1000 → char_limit=4000 → big_chunk alone (~5000 chars) should
    # fit (first chunk is always accepted; loop breaks BEFORE adding the second).
    ctx = build_context([big_chunk, second_chunk], max_tokens=1000)
    assert "SECOND CHUNK MARKER" not in ctx


# ── 3. Taxonomy schema ───────────────────────────────────────────────────────

def test_upi_taxonomy_has_ten_buckets_with_required_keys(monkeypatch):
    """The active pack's `feature_taxonomy` is the source of truth for
    classification + 3-stage retrieval + required-field gap detection. Pin
    DOMAIN_PACK to the UPI pack and lock its shape: 10 buckets, each carrying
    the four keys agents depend on (exactly what the hardcoded DOMAIN_TAXONOMY
    dict used to guarantee)."""
    from app.core.domain import registry

    expected_keys = {"label", "keywords", "required_fields", "analogue_queries"}
    upi_pack = Path(registry.__file__).resolve().parents[2] / "packs" / "network" / "network.yaml"
    monkeypatch.setenv("DOMAIN_PACK", str(upi_pack))
    registry._load.cache_clear()
    try:
        taxonomy = get_taxonomy()

        assert isinstance(taxonomy, dict)
        assert len(taxonomy) == 10, (
            f"The UPI pack must declare exactly 10 buckets; got {len(taxonomy)}. "
            "Adding a bucket requires updating dependent agents + this test."
        )
        assert next(iter(taxonomy)) == "payment_initiation", (
            "payment_initiation must stay first — it is the keyword-fallback default"
        )

        for bucket_key, bucket in taxonomy.items():
            assert isinstance(bucket, dict), f"Bucket {bucket_key!r} must be a dict"
            missing = expected_keys - set(bucket.keys())
            assert not missing, f"Bucket {bucket_key!r} missing required keys: {missing}"
            # Sanity-check list fields are non-empty lists
            for list_key in ("keywords", "required_fields", "analogue_queries"):
                val = bucket[list_key]
                assert isinstance(val, list), f"{bucket_key}.{list_key} must be a list"
                assert len(val) > 0, f"{bucket_key}.{list_key} must be non-empty"
    finally:
        registry._load.cache_clear()


# ── 4. BM25 tokenizer behavior ───────────────────────────────────────────────

def test_bm25_tokenize_behavior():
    """The BM25 tokenizer lowercases, splits on non-alphanumeric, drops
    stopwords, and drops tokens shorter than 2 chars. Locking this prevents
    silent index-corpus shifts."""
    # Lowercasing
    assert bm25_search.tokenize("Network") == ["network"]

    # Non-alphanumeric split — "0" from "v2.0" is dropped (len < 2)
    assert set(bm25_search.tokenize("Network-Lite, v2.0 mandate!")) == {
        "network", "lite", "v2", "mandate",
    }

    # Stopwords dropped
    assert bm25_search.tokenize("the mandate is a recurring payment") == [
        "mandate", "recurring", "payment",
    ]

    # Min length 2 enforced — single-letter tokens dropped
    assert "a" not in bm25_search.tokenize("a transaction b")
    assert "b" not in bm25_search.tokenize("a transaction b")
    assert bm25_search.tokenize("a transaction b") == ["transaction"]

    # Empty string and None-like handled
    assert bm25_search.tokenize("") == []


# ── 5. Embedding dimension ───────────────────────────────────────────────────

def test_embedding_dim_is_768():
    """pgvector columns, HNSW index, and ingest pipeline all assume 768-dim
    vectors from Ollama nomic-embed-text. Changing this requires an alembic
    migration + full re-embed."""
    assert EMBEDDING_DIM == 768
