# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 2.3 — deterministic line-overlap compressor tests.

No LLM, no DB. Exercises the overlap path plus the dispatch logic in
`compress_chunks_sync`.
"""
from __future__ import annotations

import pytest

from app.rag import context_compressor as cc


def _long_chunk(lines: list[str]) -> dict:
    return {"id": "c1", "content": "\n".join(lines), "source_file": "x.py"}


def test_overlap_short_chunk_passthrough():
    # Below MIN_CHARS_TO_COMPRESS — should be returned unchanged.
    chunk = {"id": "c1", "content": "short content"}
    out = cc.compress_chunks_overlap("anything", [chunk])
    assert out[0]["content"] == "short content"


def test_overlap_skips_when_under_max_lines():
    # Above MIN_CHARS_TO_COMPRESS but under max_lines — pass through.
    body = ("token here is enough text to clear the min-chars gate. " * 10)
    chunk = {"id": "c1", "content": body}
    out = cc.compress_chunks_overlap("token", [chunk])
    assert out[0]["content"] == body


def test_overlap_keeps_high_score_lines_and_neighbors():
    # 30 lines, only line 14 mentions the query terms. Others are filler.
    # max_lines=25 (default) → compression triggers, but the matched line
    # plus neighbours must survive.
    lines = [f"filler text line {i} no match here" for i in range(30)]
    lines[14] = "this line mentions compute_hash directly"
    chunk = _long_chunk(lines)
    out = cc.compress_chunks_overlap("how does compute_hash work", [chunk])
    kept = out[0]["content"]
    assert "compute_hash directly" in kept
    # ±1 neighbours preserved.
    assert "filler text line 13" in kept or "filler text line 15" in kept


def test_overlap_no_token_overlap_returns_original():
    # 30 lines of filler with NO token overlap with the query → fail-open.
    lines = [f"unrelated filler line {i}" for i in range(30)]
    chunk = _long_chunk(lines)
    original = chunk["content"]
    out = cc.compress_chunks_overlap("queryterm xyz", [chunk])
    assert out[0]["content"] == original


def test_compress_chunks_sync_dispatches_off_mode(monkeypatch):
    chunks = [{"id": "c1", "content": "any content here at all"}]
    monkeypatch.setattr(cc, "asyncio", cc.asyncio)  # keep import alive
    # Force mode = off via settings.
    from app.core.config import settings as _s
    monkeypatch.setattr(_s, "context_compression_mode", "off", raising=False)
    out = cc.compress_chunks_sync("query", chunks)
    assert out == chunks


def test_compress_chunks_sync_dispatches_overlap_mode(monkeypatch):
    # Long chunk that should clearly trigger overlap compression.
    body = "\n".join(
        ["matching keyword goes here"] +
        [f"unrelated filler line {i} content body text" for i in range(40)]
    )
    chunks = [{"id": "c1", "content": body}]
    from app.core.config import settings as _s
    monkeypatch.setattr(_s, "context_compression_mode", "overlap", raising=False)
    monkeypatch.setattr(_s, "context_compression_max_lines", 5, raising=False)
    out = cc.compress_chunks_sync("matching keyword", chunks)
    # Trimmed shorter than original; matching line preserved.
    assert len(out[0]["content"]) < len(body)
    assert "matching keyword" in out[0]["content"]
