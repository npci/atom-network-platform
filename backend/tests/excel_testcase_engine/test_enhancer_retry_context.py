# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Enhancer must not truncate its own output, nor go blind on retry.

Regression for the 2026-09-05 `stage.failed stage=enhance` run: the prompt
asked the model to echo `original_brief` verbatim, so the reply blew past
max_tokens=2000 and was cut mid-string at the same offset on every attempt
(`Invalid control character at: line 2 column 8855`). The retry then replaced
the user message with a bare correction, collapsing input 54k chars -> 3k, so
attempts 1 and 2 ran blind and returned nothing usable.
"""

from __future__ import annotations

import json

import pytest

from app.excel_testcase_engine.agents import enhancer
from app.excel_testcase_engine.agents._runtime import load_prompt


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _ScriptedClient:
    def __init__(self, payloads: list[str]) -> None:
        self._payloads = list(payloads)
        self.user_messages: list[str] = []
        self.max_tokens: list[int] = []

    async def complete(self, *, system, messages, **kwargs):
        self.user_messages.append(messages[-1].content)
        self.max_tokens.append(kwargs.get("max_tokens"))
        return _FakeResponse(self._payloads.pop(0))


_BRIEF = "SENTINEL_BRIEF — the compliance notification flow, at length. " * 40

_VALID = json.dumps({
    "archetype": "C",
    "feature_name": "Compliance Notification",
    "roles": ["Operator", "Maintenance Organisation"],
    "apis": ["ReqCompliance", "RespCompliance"],
    "coverage": ["happy_path"],
})


def test_prompt_no_longer_asks_the_model_to_echo_the_brief():
    """Echoing `original_brief` is what overran the output cap."""
    prompt = load_prompt("enhancer.md")
    assert "Do NOT include `original_brief`" in prompt
    assert '"original_brief": "<the caller\'s brief, verbatim>"' not in prompt


@pytest.mark.asyncio
async def test_output_cap_has_headroom(monkeypatch):
    """max_tokens=2000 truncated every real run; it must be well above that."""
    client = _ScriptedClient([_VALID])
    monkeypatch.setattr(enhancer, "get_client", lambda _n: client)

    await enhancer.enhance(_BRIEF, {})

    assert client.max_tokens[0] >= 8000


@pytest.mark.asyncio
async def test_retry_still_carries_the_brief(monkeypatch):
    """The corrective prompt must append to the original, not replace it."""
    client = _ScriptedClient(["not json at all", _VALID])
    monkeypatch.setattr(enhancer, "get_client", lambda _n: client)

    enriched = await enhancer.enhance(_BRIEF, {})

    assert enriched.roles == ["Operator", "Maintenance Organisation"]
    assert len(client.user_messages) == 2

    first, retry = client.user_messages
    assert "SENTINEL_BRIEF" in first
    assert "SENTINEL_BRIEF" in retry, "retry dropped the brief"
    assert len(retry) > len(first), "retry should extend the brief, not replace it"


@pytest.mark.asyncio
async def test_original_brief_is_filled_by_the_caller(monkeypatch):
    """The model omits it; the code must still populate it."""
    client = _ScriptedClient([_VALID])
    monkeypatch.setattr(enhancer, "get_client", lambda _n: client)

    enriched = await enhancer.enhance(_BRIEF, {})

    assert enriched.original_brief == _BRIEF


@pytest.mark.asyncio
async def test_roles_are_not_padded_to_match_archetype(monkeypatch):
    """Archetype C with 2 named roles must stay at 2 (see commit 00a3f20)."""
    client = _ScriptedClient([_VALID])
    monkeypatch.setattr(enhancer, "get_client", lambda _n: client)

    enriched = await enhancer.enhance(_BRIEF, {"archetype": "C"})

    assert enriched.archetype == "C"
    assert enriched.roles == ["Operator", "Maintenance Organisation"]
