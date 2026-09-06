# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 6.2 — system-prompt coercion helpers in app.core.llm.

These do NOT exercise the actual LLM clients; just the small string-vs-list
helpers that route the segmented Anthropic system prompt correctly.
"""
from __future__ import annotations

from app.core.llm import _coerce_system_to_str, _system_for_claude


def test_coerce_string_passthrough():
    assert _coerce_system_to_str("hello") == "hello"


def test_coerce_none_returns_empty():
    assert _coerce_system_to_str(None) == ""


def test_coerce_segment_list_joined_with_separator():
    segs = [
        {"type": "text", "text": "preface"},
        {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}},
    ]
    out = _coerce_system_to_str(segs)
    assert "preface" in out
    assert "rules" in out
    assert "\n\n---\n\n" in out


def test_coerce_skips_empty_segments():
    segs = [
        {"type": "text", "text": ""},
        {"type": "text", "text": "kept"},
    ]
    out = _coerce_system_to_str(segs)
    assert out == "kept"


def test_system_for_claude_string_stays_string():
    assert _system_for_claude("plain") == "plain"


def test_system_for_claude_list_passthrough_when_valid():
    segs = [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}},
    ]
    out = _system_for_claude(segs)
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[1]["cache_control"] == {"type": "ephemeral"}


def test_system_for_claude_list_with_no_valid_entries_collapses_to_string():
    segs = [{"foo": "bar"}, {"text": ""}]
    out = _system_for_claude(segs)
    assert isinstance(out, str)


def test_system_for_claude_adds_missing_type_field():
    segs = [{"text": "no-type-field"}]
    out = _system_for_claude(segs)
    assert isinstance(out, list)
    assert out[0]["type"] == "text"
