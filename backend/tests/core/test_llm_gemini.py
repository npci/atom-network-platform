# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pure tests for the Gemini provider adapter.

No real API calls are made here; the tests cover provider selection, payload
translation, response parsing, and dispatcher wiring.
"""
from __future__ import annotations

import asyncio

from app.core import llm as llm_mod
from app.core.config import settings


def test_provider_aliases_normalize_to_dispatch_names():
    assert llm_mod.normalize_provider("anthropic") == "claude"
    assert llm_mod.normalize_provider("google") == "gemini"
    assert llm_mod.normalize_provider("google_ai") == "gemini"
    assert llm_mod.normalize_provider("ollama") == "ollama"


def test_get_model_uses_gemini_model(monkeypatch):
    monkeypatch.setattr(settings, "gemini_model", "gemini-test-model")
    assert llm_mod.get_model("gemini") == "gemini-test-model"
    assert llm_mod.get_model("google") == "gemini-test-model"


def test_build_gemini_payload_maps_roles_and_system_parts():
    payload = llm_mod._build_gemini_payload(
        "System rules.",
        [
            {"role": "developer", "content": "Developer rules."},
            {"role": "user", "content": "User asks."},
            {"role": "assistant", "content": "Assistant answered."},
            {"role": "user", "content": [{"text": "List content."}]},
        ],
        max_tokens=123,
    )

    assert payload["systemInstruction"]["parts"][0]["text"] == (
        "System rules.\n\nDeveloper rules."
    )
    assert payload["generationConfig"] == {
        "maxOutputTokens": 123,
        "thinkingConfig": {"thinkingLevel": "minimal"},
    }
    assert payload["contents"] == [
        {"role": "user", "parts": [{"text": "User asks."}]},
        {"role": "model", "parts": [{"text": "Assistant answered."}]},
        {"role": "user", "parts": [{"text": "List content."}]},
    ]


def test_build_gemini_payload_uses_25_flash_thinking_budget(monkeypatch):
    monkeypatch.setattr(settings, "gemini_thinking_budget", 0)
    payload = llm_mod._build_gemini_payload(
        "sys",
        [{"role": "user", "content": "hi"}],
        max_tokens=50,
        model="gemini-2.5-flash",
    )

    assert payload["generationConfig"] == {
        "maxOutputTokens": 50,
        "thinkingConfig": {"thinkingBudget": 0},
    }


def test_parse_gemini_response_extracts_text_finish_and_usage():
    text, finish_reason, usage = llm_mod._parse_gemini_response({
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "Hello"}, {"text": " world"}]},
        }],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 3,
        },
    })

    assert text == "Hello world"
    assert finish_reason == "STOP"
    assert usage["promptTokenCount"] == 10
    assert usage["candidatesTokenCount"] == 3


def test_call_llm_dispatches_gemini(monkeypatch):
    seen = {}

    async def fake_call_gemini(system, messages, model, max_tokens, agent_name=None):
        seen.update({
            "system": system,
            "messages": messages,
            "model": model,
            "max_tokens": max_tokens,
            "agent_name": agent_name,
        })
        return "ok"

    monkeypatch.setattr(settings, "use_context_budget_check", False)
    monkeypatch.setattr(llm_mod, "_call_gemini", fake_call_gemini)

    result = asyncio.run(llm_mod.call_llm(
        "sys",
        [{"role": "user", "content": "hi"}],
        max_tokens=77,
        model="gemini-test",
        provider="google",
        agent_name="unit",
    ))

    assert result == "ok"
    assert seen == {
        "system": "sys",
        "messages": [{"role": "user", "content": "hi"}],
        "model": "gemini-test",
        "max_tokens": 77,
        "agent_name": "unit",
    }


def test_stream_gemini_yields_single_compat_chunk(monkeypatch):
    async def fake_call_gemini(*args, **kwargs):
        return "whole response"

    monkeypatch.setattr(llm_mod, "_call_gemini", fake_call_gemini)

    async def collect():
        chunks = []
        async for chunk in llm_mod._stream_gemini(
            "sys",
            [{"role": "user", "content": "hi"}],
            "gemini-test",
            100,
        ):
            chunks.append(chunk)
        return chunks

    assert asyncio.run(collect()) == ["whole response"]
