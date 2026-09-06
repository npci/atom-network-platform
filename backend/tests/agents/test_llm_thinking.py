# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Extended-thinking block handling (§reasoning).

With interleaved thinking + tool use, Anthropic REQUIRES the signed thinking blocks
be echoed back verbatim in the next assistant turn, or it 400s. These tests pin that
`_assistant_blocks_to_dicts` preserves them (and exposes the thinking text)."""
from types import SimpleNamespace

from app.core.llm import _assistant_blocks_to_dicts


def test_thinking_blocks_preserved_and_text_extracted():
    content = [
        SimpleNamespace(type="thinking", thinking="let me reason about X", signature="sig123"),
        SimpleNamespace(type="text", text="the answer"),
        SimpleNamespace(type="tool_use", id="t1", name="read_file", input={"path": "a.java"}),
    ]
    text, thinking, tool_uses, blocks = _assistant_blocks_to_dicts(content)

    assert text == "the answer"
    assert thinking == "let me reason about X"
    assert [t.name for t in tool_uses] == ["read_file"]

    # thinking block echoed back FIRST, verbatim, with its signature (Anthropic requirement)
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["thinking"] == "let me reason about X"
    assert blocks[0]["signature"] == "sig123"
    # and the tool_use block survives too
    assert any(b["type"] == "tool_use" and b["name"] == "read_file" for b in blocks)


def test_redacted_thinking_preserved():
    content = [SimpleNamespace(type="redacted_thinking", data="ENCRYPTED")]
    _text, thinking, _tools, blocks = _assistant_blocks_to_dicts(content)
    assert thinking == ""                                  # no plain text to surface
    assert blocks == [{"type": "redacted_thinking", "data": "ENCRYPTED"}]


def test_no_thinking_is_backward_compatible():
    content = [SimpleNamespace(type="text", text="hi")]
    text, thinking, tool_uses, blocks = _assistant_blocks_to_dicts(content)
    assert text == "hi" and thinking == "" and tool_uses == []
    assert blocks == [{"type": "text", "text": "hi"}]
