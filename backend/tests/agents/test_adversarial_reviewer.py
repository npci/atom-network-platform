# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 13 — adversarial reviewer (strict-prompt second-pass LLM).

Pure — `call_llm` is monkeypatched. All tests verify parsing + normalisation
behaviour without hitting a real LLM.
"""
from __future__ import annotations

import json

import pytest

from app.agents import adversarial_reviewer


# ──────────────────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_findings_on_success(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        return json.dumps([
            {
                "severity":   "high",
                "category":   "security",
                "file":       "src/Auth.java",
                "issue":      "Token validator ignores expired-token flag.",
                "suggestion": "Check token.isExpired() before trusting claims.",
            },
            {
                "severity":   "medium",
                "category":   "concurrency",
                "issue":      "Counter increment is not atomic under concurrent access.",
                "suggestion": "Use AtomicInteger or synchronise the increment block.",
            },
        ])

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    out = await adversarial_reviewer.review_adversarially("public void auth() { ... }")
    assert len(out) == 2
    assert out[0]["severity"] == "high"
    assert out[0]["category"] == "security"
    assert out[0]["file"] == "src/Auth.java"
    # Optional `file` omitted on second finding
    assert "file" not in out[1]


# ──────────────────────────────────────────────────────────────────────────────
# Fail-open paths
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_input_short_circuits(monkeypatch):
    called = {"n": 0}

    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        called["n"] += 1
        return "[]"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    assert await adversarial_reviewer.review_adversarially("") == []
    assert await adversarial_reviewer.review_adversarially("  \n") == []
    assert called["n"] == 0


def _assert_sentinel(out):
    """A LOST verdict must come back as the review-gap sentinel — a missing
    review is never indistinguishable from a clean one."""
    assert len(out) == 1
    assert out[0]["review_gap"] is True
    assert out[0]["severity"] == "high"
    assert "MISSING review" in out[0]["issue"]


@pytest.mark.asyncio
async def test_llm_exception_returns_sentinel(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    _assert_sentinel(await adversarial_reviewer.review_adversarially("some code"))


@pytest.mark.asyncio
async def test_non_list_response_returns_sentinel(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        return json.dumps({"not": "a list"})

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    _assert_sentinel(await adversarial_reviewer.review_adversarially("code"))


@pytest.mark.asyncio
async def test_non_json_response_returns_sentinel(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        return "Looks fine to me honestly"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    _assert_sentinel(await adversarial_reviewer.review_adversarially("code"))


# ──────────────────────────────────────────────────────────────────────────────
# Normalisation
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_malformed_findings_filtered(monkeypatch):
    """Findings missing `issue` or `suggestion` are dropped."""
    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        return json.dumps([
            {"severity": "high", "category": "security",
             "issue": "Real issue", "suggestion": "Real suggestion"},
            {"severity": "medium"},  # missing issue + suggestion
            "not a dict",
            {"issue": "Has issue", "suggestion": ""},  # empty suggestion
        ])

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    out = await adversarial_reviewer.review_adversarially("code")
    assert len(out) == 1
    assert out[0]["issue"] == "Real issue"


@pytest.mark.asyncio
async def test_unknown_severity_normalised_to_medium(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        return json.dumps([
            {"severity": "WHATEVER", "category": "other",
             "issue": "Some issue", "suggestion": "Some suggestion"},
        ])

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    out = await adversarial_reviewer.review_adversarially("code")
    assert out[0]["severity"] == "medium"


@pytest.mark.asyncio
async def test_unknown_category_normalised_to_other(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        return json.dumps([
            {"severity": "low", "category": "business_logic",
             "issue": "Some issue", "suggestion": "Some suggestion"},
        ])

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    out = await adversarial_reviewer.review_adversarially("code")
    assert out[0]["category"] == "other"


@pytest.mark.asyncio
async def test_findings_truncated_to_max(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        return json.dumps([
            {"severity": "low", "category": "other",
             "issue": f"Issue {i}", "suggestion": f"Fix {i}"}
            for i in range(20)
        ])

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    out = await adversarial_reviewer.review_adversarially("code", max_findings=5)
    assert len(out) == 5
    assert out[0]["issue"] == "Issue 0"
    assert out[-1]["issue"] == "Issue 4"


# ──────────────────────────────────────────────────────────────────────────────
# Context blocks in the prompt
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_code_plan_passed_in_context(monkeypatch):
    captured = {}

    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        captured["user"] = messages[-1]["content"]
        return "[]"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    plan = {"files": [{"path": "x.java", "action": "create", "intent": "Add X reasonably"}]}
    await adversarial_reviewer.review_adversarially("code", code_plan=plan)
    assert "CODE_PLAN" in captured["user"]
    assert "x.java" in captured["user"]


@pytest.mark.asyncio
async def test_primary_findings_passed_as_dont_repeat(monkeypatch):
    captured = {}

    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        captured["user"] = messages[-1]["content"]
        return "[]"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    primary = [{"issue": "Primary caught this", "severity": "high"}]
    await adversarial_reviewer.review_adversarially("code", primary_findings=primary)
    assert "PRIMARY_FINDINGS" in captured["user"]
    assert "Primary caught this" in captured["user"]


@pytest.mark.asyncio
async def test_no_extra_context_when_none_provided(monkeypatch):
    captured = {}

    async def fake_call_llm(system, messages, max_tokens=2000, **kwargs):
        captured["user"] = messages[-1]["content"]
        return "[]"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    await adversarial_reviewer.review_adversarially("code")
    assert "CODE_PLAN" not in captured["user"]
    assert "PRIMARY_FINDINGS" not in captured["user"]
    assert "CODE_UNDER_REVIEW" in captured["user"]
