# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the synthetic NL code summarizer (Slice 4).

Pure unit tests — no real LLM calls. `call_llm` is monkeypatched to:
  - return a canned summary (happy path)
  - raise (fail-closed path)
  - return empty (empty-response path)
"""
from __future__ import annotations

import pytest

from app.rag import code_summarizer


@pytest.mark.asyncio
async def test_synthesize_returns_summary_on_success(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=250, **kwargs):
        return "Enforces per-tenant request quotas using a sliding window counter."

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    result = await code_summarizer.synthesize(
        code="public boolean acquire() { return true; }",
        language="java",
        symbol_kind="method",
    )
    assert "per-tenant" in result


@pytest.mark.asyncio
async def test_synthesize_fails_closed_on_exception(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=250, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    result = await code_summarizer.synthesize("code", "java", "method")
    assert result == ""


@pytest.mark.asyncio
async def test_synthesize_empty_input_short_circuits(monkeypatch):
    # Call should never reach the LLM for empty/whitespace input.
    called = {"n": 0}

    async def fake_call_llm(system, messages, max_tokens=250, **kwargs):
        called["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    assert await code_summarizer.synthesize("", "java", "method") == ""
    assert await code_summarizer.synthesize("   \n\t", "java", "method") == ""
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_synthesize_strips_whitespace(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=250, **kwargs):
        return "   \n summary with padding \n\n"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    result = await code_summarizer.synthesize("code", "java", "method")
    assert result == "summary with padding"


@pytest.mark.asyncio
async def test_synthesize_empty_response_returns_empty(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=250, **kwargs):
        return ""

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    result = await code_summarizer.synthesize("code", "java", "method")
    assert result == ""
