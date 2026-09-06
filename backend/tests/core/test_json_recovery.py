# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Stage-2 LLM repair bounds: repair only what can be re-emitted WHOLE.

A repair over a truncated payload either wastes the call or — worse — closes the
brackets early and returns valid JSON with the tail data silently dropped. So the
parser must send the FULL payload when it fits, and skip the repair entirely when
it doesn't (falling through to lenient extraction).
"""
import asyncio

from app.core import json_recovery as JR


def test_repair_gets_the_whole_payload_not_a_head_slice(monkeypatch):
    bad = '{"a": 1 "b": 2}'                      # missing comma — survives stages 1/1b/1c
    seen = {}

    async def fake_call_llm(*args, **kwargs):
        seen["user"] = kwargs["messages"][0]["content"]
        return '{"a": 1, "b": 2}'

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
    out = asyncio.run(JR.parse_llm_json(bad))
    assert out == {"a": 1, "b": 2}
    assert bad in seen["user"]                   # full payload reached the repair model


def test_oversized_payload_skips_llm_repair(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        raise AssertionError("stage-2 repair must not run on an oversized payload")

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
    huge = '{"a": "' + "x" * (JR._REPAIR_MAX_CHARS + 100)    # malformed, unterminated string
    assert asyncio.run(JR.parse_llm_json(huge, fallback="FB")) == "FB"


def test_giant_numeric_blob_returns_fallback_not_valueerror():
    """CPython raises ValueError (not JSONDecodeError) parsing integers >4300 digits —
    recovery must swallow it and return the fallback, never propagate."""
    blob = '{"a": ' + "1" * 30_000 + "}"
    assert JR.parse_llm_json_sync(blob, fallback="FB") == "FB"


def test_small_prose_wrapped_verdict_never_reaches_repair(monkeypatch):
    """Fenced stage (1c) answers before the repair call is even considered."""
    async def fake_call_llm(*args, **kwargs):
        raise AssertionError("fenced extraction should have answered first")

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
    text = 'Reasoning about [D1] first...\n```json\n[{"why": "x"}]\n```\ndone'
    assert asyncio.run(JR.parse_llm_json(text, expect_array=True)) == [{"why": "x"}]
