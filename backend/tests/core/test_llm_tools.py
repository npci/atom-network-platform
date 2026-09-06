# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""S5 — Claude tool-use + tools-block prompt caching (app.core.llm).

The Anthropic SDK is mocked at the client boundary so we exercise OUR code
(block flattening, usage/cache capture, tool caching, re-sendable assistant
turn) without a live API call.
"""
import asyncio
from types import SimpleNamespace

import app.core.llm as llm
from app.core.llm import (
    call_claude_tools, _tools_with_cache, _assistant_blocks_to_dicts,
    tool_result_block, ToolUseRequest, ClaudeToolTurn,
)


def _fake_response():
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="I'll read the file."),
            SimpleNamespace(type="tool_use", id="tu_1", name="read_file", input={"path": "A.java"}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=80, cache_creation_input_tokens=10,
        ),
    )


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_tools_with_cache_marks_last_only_and_does_not_mutate_input():
    tools = [{"name": "a"}, {"name": "b"}]
    out = _tools_with_cache(tools)
    assert out[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in out[0]
    assert "cache_control" not in tools[-1], "must not mutate the caller's list"


def test_assistant_blocks_flatten_text_and_tool_use():
    text, thinking, tools, blocks = _assistant_blocks_to_dicts(_fake_response().content)
    assert text == "I'll read the file."
    assert thinking == ""   # no thinking blocks in this fixture
    assert tools == [ToolUseRequest(id="tu_1", name="read_file", input={"path": "A.java"})]
    assert blocks == [
        {"type": "text", "text": "I'll read the file."},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "A.java"}},
    ]


def test_tool_result_block_shape():
    assert tool_result_block("tu_1", "ok") == {
        "type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}
    err = tool_result_block("tu_2", "read first", is_error=True)
    assert err["is_error"] is True


def test_wants_tools_predicate():
    assert ClaudeToolTurn("", [], "tool_use", []).wants_tools is True
    assert ClaudeToolTurn("done", [], "end_turn", []).wants_tools is False


# ── call_claude_tools (mocked SDK) ────────────────────────────────────────────

def test_call_claude_tools_end_to_end(monkeypatch):
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_response()

    monkeypatch.setattr(
        llm, "_get_anthropic_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=fake_create)),
    )

    tools = [{"name": "read_file", "input_schema": {}}, {"name": "edit_file", "input_schema": {}}]
    turn = asyncio.run(call_claude_tools(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        tools=tools, model="claude-test", agent_name="agentic",
    ))

    # response parsed
    assert turn.text == "I'll read the file."
    assert turn.wants_tools and turn.stop_reason == "tool_use"
    assert [t.name for t in turn.tool_uses] == ["read_file"]
    assert turn.tool_uses[0].input == {"path": "A.java"}
    # assistant turn is re-sendable verbatim
    assert turn.assistant_content[1]["type"] == "tool_use"
    # usage incl. cache read/write tokens captured
    assert turn.usage["cache_read_tokens"] == 80 and turn.usage["cache_write_tokens"] == 10
    # tools sent with cache_control on the LAST tool only; input list untouched
    assert "cache_control" not in captured["tools"][0]
    assert captured["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tools[-1]


def test_tool_cache_skipped_when_system_already_at_breakpoint_budget(monkeypatch):
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_response()

    monkeypatch.setattr(
        llm, "_get_anthropic_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=fake_create)),
    )
    # Pin the settings this test's premise depends on, independent of the ambient default:
    # caching ON (so the tool-cache path runs at all) but the rolling message-tail OFF, so the
    # keep=2 system-breakpoint trim (which only fires with the tail) does NOT collapse the 4
    # breakpoints below budget — otherwise the "system already at budget" scenario can't exist.
    monkeypatch.setattr(llm.settings, "prompt_cache_enabled", True)
    monkeypatch.setattr(llm.settings, "agentic_cache_message_tail", False)
    # A system that already spends all 4 cache breakpoints → tools must NOT add a 5th.
    seg = lambda i: {"type": "text", "text": f"s{i}", "cache_control": {"type": "ephemeral"}}
    asyncio.run(call_claude_tools(
        system=[seg(i) for i in range(4)],
        messages=[{"role": "user", "content": "x"}],
        tools=[{"name": "t", "input_schema": {}}], model="m",
    ))
    assert "cache_control" not in captured["tools"][-1]


def test_cache_tools_false_sends_plain_tools(monkeypatch):
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_response()

    monkeypatch.setattr(
        llm, "_get_anthropic_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=fake_create)),
    )
    asyncio.run(call_claude_tools(
        system="s", messages=[{"role": "user", "content": "x"}],
        tools=[{"name": "t", "input_schema": {}}], model="m", cache_tools=False,
    ))
    assert "cache_control" not in captured["tools"][-1]
