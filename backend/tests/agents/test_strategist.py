# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Strategist stage (P2): one structural recommendation after repeated failed fix rounds,
rendered as a binding change-of-approach directive in the code agent's feedback."""
import asyncio

from app.agents import strategist as S
from app.agents.agentic_subagents import _feedback_block


def test_structural_advice_builds_history_prompt(monkeypatch):
    captured = {}

    async def fake(system, messages, max_tokens=4000, model=None, agent_name=None, provider=None):
        captured["system"] = system
        captured["prompt"] = messages[0]["content"]
        return "  Move the mapping into PayLoadMapper instead of patching the validator.  "
    monkeypatch.setattr("app.core.llm.call_llm", fake)

    out = asyncio.run(S.structural_advice(
        plan_summary="add retryFlag to Pay", attempts=4,
        error_history=[{"errors": ["A.java:10 cannot find symbol", "B.java:2 x"]}, "raw note"],
        diff_stat="2 files changed"))
    assert out.startswith("Move the mapping")
    assert "failed 4 consecutive" in captured["prompt"]
    assert "retryFlag" in captured["prompt"] and "A.java:10" in captured["prompt"]
    assert "structural" in captured["system"].lower()


def test_structural_advice_fails_open(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr("app.core.llm.call_llm", boom)
    assert asyncio.run(S.structural_advice(plan_summary="", attempts=3, error_history=[])) == ""


def test_feedback_block_renders_strategy_as_binding():
    fb = _feedback_block({"gates": {"compile": False}, "errors": ["A.java:1 err"],
                          "strategy": "Split the change; land the schema first."})
    assert "STRATEGIST" in fb and "Split the change" in fb
    assert "CHANGE APPROACH" in fb


def test_feedback_block_without_strategy_unchanged():
    fb = _feedback_block({"gates": {"compile": False}, "errors": ["A.java:1 err"]})
    assert "STRATEGIST" not in fb
