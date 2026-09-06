# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 0.5 — unit tests confirming `build_context` uses real tokens.

The legacy budget was `chars/4`; the new budget is `count_tokens(...)`.
We stub `count_tokens` to a deterministic char-counter and verify that
the budget cap is enforced in token space, not char space.
"""
from __future__ import annotations

import pytest

try:
    from app.rag import retrieval
except Exception as e:  # pragma: no cover — missing optional deps in sandbox
    pytest.skip(f"app.rag.retrieval not importable: {e}", allow_module_level=True)


def _chunk(category: str, source: str, content: str) -> dict:
    return {
        "doc_category": category,
        "source_file": source,
        "content": content,
    }


@pytest.fixture(autouse=True)
def stub_count_tokens(monkeypatch):
    """1 char ≈ 1 token so `max_tokens` directly bounds chars."""
    import app.core.tokens as tok
    monkeypatch.setattr(tok, "count_tokens", lambda s, model=None: max(1, len(s or "")))


@pytest.fixture(autouse=True)
def disable_compression(monkeypatch):
    """Don't pull in the LLM compressor during these tests."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "use_context_compression", False)


def test_build_context_respects_token_budget(monkeypatch):
    chunks = [
        _chunk("docs", "a.md", "x" * 100),  # ~100 + ~20 header tokens
        _chunk("docs", "b.md", "y" * 100),
        _chunk("docs", "c.md", "z" * 100),
    ]
    out = retrieval.build_context(chunks, max_tokens=150, model="m")
    # Should fit at most one chunk (~120 tokens) within a 150-token budget.
    assert "a.md" in out
    assert "b.md" not in out
    assert "c.md" not in out


def test_build_context_includes_all_when_budget_large(monkeypatch):
    chunks = [
        _chunk("docs", "a.md", "x" * 50),
        _chunk("docs", "b.md", "y" * 50),
    ]
    out = retrieval.build_context(chunks, max_tokens=10_000, model="m")
    assert "a.md" in out and "b.md" in out


def test_build_context_handles_empty_chunks():
    out = retrieval.build_context([], max_tokens=4000, model="m")
    assert out == ""


def test_build_context_default_model_resolution(monkeypatch):
    """When `model=None`, the helper should still function (resolves via
    `app.core.llm.get_model` or falls back gracefully)."""
    chunks = [_chunk("docs", "x.md", "hello")]
    out = retrieval.build_context(chunks, max_tokens=4000)
    assert "x.md" in out
