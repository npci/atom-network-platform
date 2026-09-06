# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Integration tests for Slice 4 multiview chunk expansion + retrieval dedup.

Pure tests — no DB, no LLM calls. `expand_to_multiview` is a pure function;
`_dedup_by_parent_symbol` is a pure function over a list of synthetic dicts.
"""
from __future__ import annotations

from app.rag import code_chunker_ts, hybrid_search


# ──────────────────────────────────────────────────────────────────────────────
# expand_to_multiview — pure function
# ──────────────────────────────────────────────────────────────────────────────

def test_file_chunk_passes_through_unchanged():
    file_chunk = {
        "path": "RateLimiter.java",
        "content": "package x; ...",
        "symbol_kind": "file",
        "symbol_name": "RateLimiter.java",
    }
    out = code_chunker_ts.expand_to_multiview(file_chunk, nl_summary="some summary")
    assert out == [file_chunk]


def test_regex_chunk_without_symbol_kind_passes_through():
    # Regex chunker output has no `symbol_kind` key — must be untouched.
    regex_chunk = {"path": "x.java", "class_name": "Foo", "method_name": None,
                   "content": "class Foo {}", "chunk_index": 0}
    out = code_chunker_ts.expand_to_multiview(regex_chunk)
    assert out == [regex_chunk]


def test_symbol_chunk_with_summary_emits_three_views():
    symbol_chunk = {
        "path": "RateLimiter.java",
        "content": "public boolean acquire() { return count <= limit; }",
        "symbol_kind": "method",
        "symbol_name": "acquire",
        "signature": "public boolean acquire()",
        "line_start": 10,
        "line_end": 12,
        "language": "java",
        "chunk_index": 1,
    }
    out = code_chunker_ts.expand_to_multiview(symbol_chunk, nl_summary="Returns whether a request should be permitted.")

    assert len(out) == 3
    kinds = [v["view_kind"] for v in out]
    assert kinds == ["body", "signature", "nl_summary"]

    # All three share a parent_symbol_id
    pids = {v["parent_symbol_id"] for v in out}
    assert len(pids) == 1
    assert next(iter(pids))  # not None/empty

    # Distinct content per view
    assert out[0]["content"] == symbol_chunk["content"]  # body = original
    assert "[method]" in out[1]["content"] and "public boolean acquire()" in out[1]["content"]
    assert out[2]["content"] == "Returns whether a request should be permitted."


def test_symbol_chunk_without_summary_emits_two_views():
    symbol_chunk = {
        "path": "RateLimiter.java",
        "content": "public void noop() {}",
        "symbol_kind": "method",
        "symbol_name": "noop",
        "signature": "public void noop()",
        "language": "java",
    }
    out = code_chunker_ts.expand_to_multiview(symbol_chunk, nl_summary="")

    assert len(out) == 2
    kinds = [v["view_kind"] for v in out]
    assert kinds == ["body", "signature"]


def test_symbol_chunk_without_signature_skips_signature_view():
    symbol_chunk = {
        "path": "x.java",
        "content": "class X {}",
        "symbol_kind": "class",
        "symbol_name": "X",
        "signature": None,
        "language": "java",
    }
    out = code_chunker_ts.expand_to_multiview(symbol_chunk, nl_summary="A tiny empty class.")

    assert len(out) == 2
    kinds = [v["view_kind"] for v in out]
    assert kinds == ["body", "nl_summary"]


# ──────────────────────────────────────────────────────────────────────────────
# _dedup_by_parent_symbol — pure function
# ──────────────────────────────────────────────────────────────────────────────

def test_dedup_keeps_first_occurrence_per_parent():
    """Input is assumed score-sorted (caller's invariant). Dedup keeps the
    first row per parent_symbol_id — by convention, the highest-scoring view."""
    chunks = [
        {"id": "a", "parent_symbol_id": "sym1", "content": "body of sym1",      "score": 0.9},
        {"id": "b", "parent_symbol_id": "sym2", "content": "body of sym2",      "score": 0.8},
        {"id": "c", "parent_symbol_id": "sym1", "content": "signature of sym1", "score": 0.7},
        {"id": "d", "parent_symbol_id": "sym1", "content": "nl_summary of sym1","score": 0.5},
        {"id": "e", "parent_symbol_id": "sym2", "content": "signature of sym2", "score": 0.4},
    ]
    out = hybrid_search._dedup_by_parent_symbol(chunks)

    # sym1's "body" (highest-scored) survives, other two sym1 rows dropped.
    # sym2's "body" (highest-scored) survives, sym2's signature dropped.
    assert [c["id"] for c in out] == ["a", "b"]


def test_dedup_passes_through_rows_without_parent_symbol_id():
    """Rows from pre-Slice-4 data, file chunks, or non-code docs have
    parent_symbol_id=None — they always survive dedup."""
    chunks = [
        {"id": "a", "parent_symbol_id": None, "content": "doc chunk A", "score": 0.9},
        {"id": "b", "parent_symbol_id": None, "content": "doc chunk B", "score": 0.8},
        {"id": "c", "parent_symbol_id": None, "content": "doc chunk C", "score": 0.7},
    ]
    out = hybrid_search._dedup_by_parent_symbol(chunks)
    assert [c["id"] for c in out] == ["a", "b", "c"]


def test_dedup_mixed_rows():
    chunks = [
        {"id": "doc1", "parent_symbol_id": None,   "content": "doc A", "score": 0.95},
        {"id": "body1","parent_symbol_id": "sym1", "content": "body",  "score": 0.90},
        {"id": "sig1", "parent_symbol_id": "sym1", "content": "sig",   "score": 0.85},
        {"id": "doc2", "parent_symbol_id": None,   "content": "doc B", "score": 0.80},
        {"id": "body2","parent_symbol_id": "sym2", "content": "body",  "score": 0.75},
    ]
    out = hybrid_search._dedup_by_parent_symbol(chunks)
    assert [c["id"] for c in out] == ["doc1", "body1", "doc2", "body2"]


def test_dedup_on_empty_input():
    assert hybrid_search._dedup_by_parent_symbol([]) == []
