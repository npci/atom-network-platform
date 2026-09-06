# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 6.1 + 6.2 — prompt_blocks helpers.

Pure-Python — no LLM, no DB.
"""
from __future__ import annotations

from app.core.prompt_blocks import (
    PROD_OUTPUT_RULES,
    CITATION_RULES,
    MARKDOWN_RULES,
    assemble_system_prompt,
    segments_for_anthropic_cache,
)


def test_block_constants_are_non_empty():
    assert PROD_OUTPUT_RULES.strip()
    assert CITATION_RULES.strip()
    assert MARKDOWN_RULES.strip()


def test_assemble_joins_with_separator():
    out = assemble_system_prompt(["Role preface.", CITATION_RULES, PROD_OUTPUT_RULES])
    assert "Role preface." in out
    assert "[S#]" in out
    assert "STRICT" in out
    # Separator between blocks.
    assert "\n\n---\n\n" in out


def test_assemble_drops_empty_parts():
    out = assemble_system_prompt(["", None, "X", "   ", "Y"])
    assert out == "X\n\n---\n\nY"


def test_segments_marks_only_cacheable_with_cache_control():
    segs = segments_for_anthropic_cache([
        ("Role preface.", False),
        (PROD_OUTPUT_RULES, True),
        (CITATION_RULES, True),
    ])
    assert segs[0]["type"] == "text"
    assert "cache_control" not in segs[0]
    assert segs[1]["cache_control"] == {"type": "ephemeral"}
    assert segs[2]["cache_control"] == {"type": "ephemeral"}


def test_segments_drops_empty_parts():
    segs = segments_for_anthropic_cache([
        ("", True),       # empty — dropped
        ("   ", True),    # whitespace — dropped
        ("real", False),
    ])
    assert len(segs) == 1
    assert segs[0]["text"] == "real"


def test_segments_caps_cache_slots_at_four():
    # Anthropic permits at most 4 cache_control markers per request.
    segs = segments_for_anthropic_cache([
        (f"block {i}", True) for i in range(6)
    ])
    cached = [s for s in segs if s.get("cache_control")]
    assert len(cached) == 4
    # First 4 get the marker; later ones become uncached text segments.
    assert all("cache_control" not in s for s in segs[4:])
