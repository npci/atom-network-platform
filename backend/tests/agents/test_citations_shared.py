# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the shared citation-enforcement helpers (Slice 9b/9c)."""
from __future__ import annotations

import pytest

from app.agents.citations import (
    GENERATE_RULES,
    PRESERVE_RULES,
    generate_suffix,
    preserve_suffix,
    upstream_has_citations,
)
from app.core.config import settings


# ──────────────────────────────────────────────────────────────────────────────
# Rule blocks are non-empty + distinct
# ──────────────────────────────────────────────────────────────────────────────

class TestRuleBlocks:

    def test_generate_rules_non_empty(self):
        assert "[N]" in GENERATE_RULES
        assert "Sources" in GENERATE_RULES

    def test_preserve_rules_non_empty(self):
        assert "preserve" in PRESERVE_RULES.lower()
        assert "[NO SOURCE]" in PRESERVE_RULES

    def test_blocks_are_distinct(self):
        # 9b/9c is meaningless if both buckets emit the same text.
        assert GENERATE_RULES != PRESERVE_RULES


# ──────────────────────────────────────────────────────────────────────────────
# Suffix helpers gated by flag
# ──────────────────────────────────────────────────────────────────────────────

class TestSuffixHelpers:

    def test_generate_suffix_empty_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(settings, "use_citation_enforcement", False)
        assert generate_suffix() == ""

    def test_generate_suffix_returns_rules_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(settings, "use_citation_enforcement", True)
        assert generate_suffix() == GENERATE_RULES

    def test_preserve_suffix_empty_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(settings, "use_citation_enforcement", False)
        assert preserve_suffix() == ""

    def test_preserve_suffix_returns_rules_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(settings, "use_citation_enforcement", True)
        assert preserve_suffix() == PRESERVE_RULES


# ──────────────────────────────────────────────────────────────────────────────
# upstream_has_citations probe
# ──────────────────────────────────────────────────────────────────────────────

class TestUpstreamHasCitations:

    def test_simple_citation(self):
        assert upstream_has_citations("Limit is ₹1 lakh [3].") is True

    def test_multi_citation_styles(self):
        assert upstream_has_citations("multi-source [1][4].") is True
        assert upstream_has_citations("multi-source [1, 4].") is True

    def test_no_citation(self):
        assert upstream_has_citations("plain prose with no markers.") is False

    def test_empty_or_none(self):
        assert upstream_has_citations("") is False
        assert upstream_has_citations(None) is False

    def test_does_not_match_bracketed_words(self):
        # `[NOTE]` etc. must not look like a citation
        assert upstream_has_citations("see [NOTE] above") is False
        assert upstream_has_citations("see [TODO] above") is False


# ──────────────────────────────────────────────────────────────────────────────
# Agent integration smoke tests — flag on, upstream cited → suffix appears
# ──────────────────────────────────────────────────────────────────────────────


class TestCanvasIntegration:

    @pytest.mark.asyncio
    async def test_preserve_suffix_appended_when_research_has_citations(self, monkeypatch):
        from app.agents import canvas
        captured: list = []

        async def fake_stream_llm(system, messages, max_tokens, **kwargs):
            captured.append(system)
            if False: yield ""    # type: ignore[unreachable]

        monkeypatch.setattr(canvas, "stream_llm", fake_stream_llm)
        monkeypatch.setattr(settings, "use_citation_enforcement", True)

        async for _ in canvas.stream_canvas_turn(
            enriched_prompt="ep",
            research_report="cited research [1]",
            conversation_history=[], new_user_message="generate",
        ):
            pass
        assert "Citation Preservation" in captured[0]
