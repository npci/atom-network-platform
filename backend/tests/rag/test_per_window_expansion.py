# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1.4 — `_expand_per_window_if_enabled` produces one row per
sliding window for oversized chunks, sharing a parent_symbol_id.
"""
from __future__ import annotations

import pytest

try:
    from app.rag import code_ingestion
    from app.rag import embeddings
except Exception as e:  # pragma: no cover
    pytest.skip(f"app.rag.code_ingestion not importable: {e}", allow_module_level=True)


def test_passthrough_when_flag_off(monkeypatch):
    monkeypatch.setattr(code_ingestion.settings, "use_per_window_chunk_storage", False)
    chunks = [{"path": "x.py", "content": "x" * 20_000, "chunk_index": 0}]
    out = code_ingestion._expand_per_window_if_enabled(chunks)
    assert out == chunks


def test_passthrough_for_short_chunks(monkeypatch):
    monkeypatch.setattr(code_ingestion.settings, "use_per_window_chunk_storage", True)
    chunks = [{"path": "x.py", "content": "short body", "chunk_index": 0}]
    out = code_ingestion._expand_per_window_if_enabled(chunks)
    assert len(out) == 1
    assert out[0]["content"] == "short body"


def test_oversized_chunk_expands(monkeypatch):
    monkeypatch.setattr(code_ingestion.settings, "use_per_window_chunk_storage", True)
    # 4× MAX_EMBED_CHARS triggers ≥ 4 windows after 50% overlap.
    body = "abcdef\n" * (embeddings.MAX_EMBED_CHARS // 2)
    chunks = [{
        "path": "big.java",
        "content": body,
        "chunk_index": 7,
        "symbol_kind": "method",
        "symbol_name": "doStuff",
    }]
    out = code_ingestion._expand_per_window_if_enabled(chunks)
    assert len(out) >= 2

    # All produced rows share a parent_symbol_id.
    parents = {row.get("parent_symbol_id") for row in out}
    assert len(parents) == 1
    assert next(iter(parents))  # non-empty

    # view_kind labels are window:0, window:1, ... in order.
    kinds = [row["view_kind"] for row in out]
    assert kinds[0] == "window:0"
    assert kinds[1] == "window:1"


def test_existing_parent_symbol_id_preserved(monkeypatch):
    monkeypatch.setattr(code_ingestion.settings, "use_per_window_chunk_storage", True)
    body = "x" * (embeddings.MAX_EMBED_CHARS * 3)
    chunks = [{
        "path": "f.py",
        "content": body,
        "chunk_index": 0,
        "parent_symbol_id": "preset-uuid",
    }]
    out = code_ingestion._expand_per_window_if_enabled(chunks)
    parents = {row["parent_symbol_id"] for row in out}
    assert parents == {"preset-uuid"}
