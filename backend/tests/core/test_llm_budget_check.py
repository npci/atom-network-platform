# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 0.3 (integration) — `call_llm` and `stream_llm` must raise
`ContextOverflowError` BEFORE dispatching when prompt > model window.

We stub the provider dispatch and run the async coroutines via
`asyncio.run` so this test file does not require pytest-asyncio. The
test environment in the sandbox doesn't ship sqlalchemy / pytest-asyncio
so these tests are gated by a try/except import — when the heavy deps
are absent the tests skip cleanly.
"""
from __future__ import annotations

import asyncio

import pytest

# Skip the entire module unless `app.core.llm` and its transitive deps
# (notably sqlalchemy / pydantic / etc.) actually import. The Phase-0.3
# behaviour is exercised at import time of `app.core.llm`; if the import
# can't complete in this sandbox, there's nothing to test here.
try:
    import app.core.llm as llm_mod  # noqa: F401
    from app.agents._context_packing import ContextOverflowError  # noqa: F401
except Exception as e:  # pragma: no cover
    pytest.skip(f"app.core.llm not importable in this env: {e}", allow_module_level=True)


@pytest.fixture
def stub_claude(monkeypatch):
    flags = {"call": 0, "stream": 0}

    async def fake_call(*a, **kw):
        flags["call"] += 1
        return "ok"

    async def fake_stream(*a, **kw):
        flags["stream"] += 1
        for tok in ("hello", " world"):
            yield tok

    monkeypatch.setattr(llm_mod, "_call_claude", fake_call)
    monkeypatch.setattr(llm_mod, "_stream_claude", fake_stream)
    monkeypatch.setattr(llm_mod, "get_provider", lambda: "claude")
    # Note: get_model accepts a `provider` kwarg in this fork; accept any kwargs.
    monkeypatch.setattr(llm_mod, "get_model", lambda *a, **kw: "claude-haiku-4-5")
    return flags


@pytest.fixture
def force_small_window(monkeypatch):
    import app.core.tokens as tok_mod
    import app.agents._context_packing as cp
    monkeypatch.setattr(tok_mod, "model_context_window", lambda m: 1000)
    monkeypatch.setattr(cp, "model_context_window", lambda m: 1000)
    monkeypatch.setattr(tok_mod, "count_tokens",
                        lambda s, model=None: max(1, len(s or "")))
    monkeypatch.setattr(cp, "count_tokens",
                        lambda s, model=None: max(1, len(s or "")))
    monkeypatch.setattr(cp, "count_messages_tokens",
                        lambda sys, msgs, model=None:
                        (len(sys or "") if isinstance(sys, str) else 0)
                        + sum(len(m.get("content") or "")
                              for m in (msgs or []) if isinstance(m, dict)))


def test_call_llm_passes_for_small_prompt(stub_claude, force_small_window):
    out = asyncio.run(llm_mod.call_llm(
        "system", [{"role": "user", "content": "hi"}], max_tokens=200,
    ))
    assert out == "ok"
    assert stub_claude["call"] == 1


def test_call_llm_raises_on_overflow(stub_claude, force_small_window):
    huge = "x" * 1500
    with pytest.raises(ContextOverflowError):
        asyncio.run(llm_mod.call_llm(
            huge, [{"role": "user", "content": "u"}], max_tokens=200,
        ))
    assert stub_claude["call"] == 0


def test_call_llm_gate_can_be_disabled(stub_claude, force_small_window, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "use_context_budget_check", False)
    huge = "x" * 1500
    out = asyncio.run(llm_mod.call_llm(
        huge, [{"role": "user", "content": "u"}], max_tokens=200,
    ))
    assert out == "ok"
    assert stub_claude["call"] == 1


def test_stream_llm_raises_on_overflow(stub_claude, force_small_window):
    huge = "x" * 1500

    async def consume():
        async for _ in llm_mod.stream_llm(
            huge, [{"role": "user", "content": "u"}], max_tokens=200,
        ):
            pass

    with pytest.raises(ContextOverflowError):
        asyncio.run(consume())
    assert stub_claude["stream"] == 0


def test_stream_llm_passes_for_small_prompt(stub_claude, force_small_window):
    async def consume():
        chunks = []
        async for c in llm_mod.stream_llm(
            "system", [{"role": "user", "content": "hi"}], max_tokens=200,
        ):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(consume())
    assert "".join(chunks) == "hello world"
    assert stub_claude["stream"] == 1
