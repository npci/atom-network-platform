# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 8 context compression.

Pure — no real LLM. `app.core.llm.call_llm` monkeypatched to return canned
JSON arrays. Also exercises `build_context()` wiring (flag on/off + with
and without query).
"""
from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.rag import context_compressor, retrieval


# ──────────────────────────────────────────────────────────────────────────────
# Sentence splitter — pure
# ──────────────────────────────────────────────────────────────────────────────

def test_split_sentences_basic():
    text = "the network is a payment system. It was launched by the Authority. It handles millions of transactions."
    out = context_compressor._split_sentences(text)
    assert out == [
        "the network is a payment system.",
        "It was launched by the Authority.",
        "It handles millions of transactions.",
    ]


def test_split_sentences_handles_numbers_and_punctuation():
    text = "Limit is 1 lakh. Can this be raised? Yes! Contact your bank."
    out = context_compressor._split_sentences(text)
    assert len(out) == 4
    assert out[0].endswith(".")
    assert out[1].endswith("?")
    assert out[2].endswith("!")


def test_split_sentences_empty():
    assert context_compressor._split_sentences("") == []
    assert context_compressor._split_sentences("   \n") == []


# ──────────────────────────────────────────────────────────────────────────────
# compress_chunks — happy paths
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_keeps_only_selected_sentences(monkeypatch):
    """LLM returns [0, 2] → compressed content has sentences 0 and 2."""
    async def fake_call_llm(system, messages, max_tokens=150, **kwargs):
        return "[0, 2]"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    chunk = {
        "content": (
            "the network has transaction limits. It supports QR-code payments. "
            "The default per-transaction limit is one lakh rupees. "
            "Merchants can opt for higher limits. There are also daily caps."
        ),
    }
    out = await context_compressor.compress_chunks(
        "what is the network transaction limit?", [chunk], min_chars_to_compress=0,
    )

    assert len(out) == 1
    compressed = out[0]["content"]
    assert "the network has transaction limits." in compressed
    assert "one lakh rupees" in compressed
    # Middle sentence (index 1) and last two (index 3, 4) dropped
    assert "QR-code" not in compressed
    assert "higher limits" not in compressed


@pytest.mark.asyncio
async def test_compress_preserves_chunk_fields(monkeypatch):
    """Other chunk dict fields are preserved unchanged; only `content` mutates."""
    async def fake_call_llm(system, messages, max_tokens=150, **kwargs):
        return "[0]"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    chunk = {
        "id": "abc",
        "source_file": "x.md",
        "doc_category": "upi_product_doc",
        "content": "First. Second. Third.",
        "chunk_index": 5,
        "score": 0.87,
    }
    out = await context_compressor.compress_chunks("q", [chunk], min_chars_to_compress=0)
    assert len(out) == 1
    assert out[0]["id"] == "abc"
    assert out[0]["source_file"] == "x.md"
    assert out[0]["chunk_index"] == 5
    assert out[0]["score"] == 0.87
    assert out[0]["content"] == "First."


@pytest.mark.asyncio
async def test_compress_skips_short_chunks(monkeypatch):
    """Chunks below MIN_CHARS_TO_COMPRESS pass through unchanged; no LLM call."""
    called = {"n": 0}

    async def fake_call_llm(system, messages, max_tokens=150, **kwargs):
        called["n"] += 1
        return "[]"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    short_chunk = {"content": "Very short."}
    out = await context_compressor.compress_chunks("q", [short_chunk])
    assert out[0]["content"] == "Very short."
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_compress_skips_when_query_empty(monkeypatch):
    called = {"n": 0}

    async def fake_call_llm(system, messages, max_tokens=150, **kwargs):
        called["n"] += 1
        return "[]"
    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    chunk = {"content": "x" * 400}
    out = await context_compressor.compress_chunks("", [chunk])
    assert out[0]["content"] == "x" * 400
    assert called["n"] == 0

    out = await context_compressor.compress_chunks("   ", [chunk])
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_compress_parallelizes_multiple_chunks(monkeypatch):
    """Verifies the LLM is called once per (compressible) chunk."""
    calls = {"n": 0}

    async def fake_call_llm(system, messages, max_tokens=150, **kwargs):
        calls["n"] += 1
        return "[0]"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    chunks = [
        {"content": "Sentence A. " * 30},
        {"content": "Sentence B. " * 30},
        {"content": "Sentence C. " * 30},
    ]
    out = await context_compressor.compress_chunks("q", chunks)
    assert calls["n"] == 3
    assert len(out) == 3


# ──────────────────────────────────────────────────────────────────────────────
# compress_chunks — fail-open paths
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_llm_exception_keeps_original(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=150, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    original = "First. Second. Third. Fourth. " * 20
    out = await context_compressor.compress_chunks("q", [{"content": original}])
    assert out[0]["content"] == original


@pytest.mark.asyncio
async def test_compress_non_json_response_keeps_original(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=150, **kwargs):
        return "I think all sentences are useful honestly"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    original = "First. Second. Third. " * 20
    out = await context_compressor.compress_chunks("q", [{"content": original}])
    assert out[0]["content"] == original


@pytest.mark.asyncio
async def test_compress_empty_indices_keeps_original(monkeypatch):
    """If the LLM returns [] (dropping everything), we keep the original —
    zero kept is suspicious, better to preserve than lose content."""
    async def fake_call_llm(system, messages, max_tokens=150, **kwargs):
        return "[]"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    original = "First. Second. Third. Fourth. " * 20
    out = await context_compressor.compress_chunks("q", [{"content": original}])
    assert out[0]["content"] == original


@pytest.mark.asyncio
async def test_compress_out_of_range_indices_ignored(monkeypatch):
    """Invalid indices in LLM output are filtered out; valid ones still kept."""
    async def fake_call_llm(system, messages, max_tokens=150, **kwargs):
        # Return a mix of valid (0) and invalid (-1, 999, "abc") indices
        return json.dumps([0, -1, 999, "abc"])

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    original = "First sentence here. Second. Third. Fourth."
    out = await context_compressor.compress_chunks("q", [{"content": original + (" filler." * 20)}])
    assert out[0]["content"].startswith("First sentence here.")


@pytest.mark.asyncio
async def test_compress_empty_chunk_list_returns_empty():
    out = await context_compressor.compress_chunks("q", [])
    assert out == []


# ──────────────────────────────────────────────────────────────────────────────
# build_context wiring
# ──────────────────────────────────────────────────────────────────────────────

def test_build_context_flag_off_skips_compressor(monkeypatch):
    """Flag OFF → compressor module never touched."""
    monkeypatch.setattr(settings, "use_context_compression", False)

    chunks = [{
        "doc_category": "d", "source_file": "f",
        "content": "First. Second. Third. Fourth. " * 10,
    }]

    # Sentinel: patch compress_chunks_sync to fail loudly if called.
    def should_not_be_called(*a, **kw):
        raise AssertionError("compress should not run when flag is off")
    monkeypatch.setattr(context_compressor, "compress_chunks_sync", should_not_be_called)

    ctx = retrieval.build_context(chunks, query="any")
    assert "First." in ctx
    assert "Fourth." in ctx


def test_build_context_no_query_skips_compressor(monkeypatch):
    """Flag ON but no query passed → compressor skipped."""
    monkeypatch.setattr(settings, "use_context_compression", True)

    chunks = [{"doc_category": "d", "source_file": "f",
               "content": "First. Second. " * 10}]

    def should_not_be_called(*a, **kw):
        raise AssertionError("compress should not run without query")
    monkeypatch.setattr(context_compressor, "compress_chunks_sync", should_not_be_called)

    ctx = retrieval.build_context(chunks)  # no query kwarg
    assert "First." in ctx


def test_build_context_flag_on_with_query_invokes_compressor(monkeypatch):
    monkeypatch.setattr(settings, "use_context_compression", True)

    chunks = [{"doc_category": "d", "source_file": "f",
               "content": "First. Second. Third. Fourth. " * 10}]

    called = {"n": 0}
    def fake_compress(query, chunks_in, **kw):
        called["n"] += 1
        # Return shortened chunks
        return [{**c, "content": "Compressed output."} for c in chunks_in]

    monkeypatch.setattr(context_compressor, "compress_chunks_sync", fake_compress)

    ctx = retrieval.build_context(chunks, query="what is the network?")
    assert called["n"] == 1
    assert "Compressed output." in ctx
    assert "First." not in ctx  # original text dropped
