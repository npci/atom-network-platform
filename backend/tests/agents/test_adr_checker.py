# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 11 ADR contradiction-check helpers.

Pure — no LLM, no I/O. Locks the prompt-suffix shape and the ADR-reference +
Concerns-section parsers.
"""
from __future__ import annotations

from app.agents import adr_checker


# ──────────────────────────────────────────────────────────────────────────────
# prompt_suffix — shape lock
# ──────────────────────────────────────────────────────────────────────────────

def test_prompt_suffix_contains_section_header():
    s = adr_checker.prompt_suffix()
    assert "## Design Review Cross-Check" in s
    assert "## Design Review Concerns" in s
    # Must tell the LLM to OMIT the section when no contradictions exist.
    assert "OMIT the section" in s or "omit the section" in s.lower()


def test_prompt_suffix_forbids_fabrication():
    s = adr_checker.prompt_suffix()
    assert "Do NOT invent" in s or "do not invent" in s.lower()


# ──────────────────────────────────────────────────────────────────────────────
# detect_potential_adr_references — pure parser
# ──────────────────────────────────────────────────────────────────────────────

def test_detect_finds_numbered_adrs():
    text = (
        "We adopted ADR-042 for rate limiting. Later, ADR 057 added tier "
        "awareness. Read adr-099 for the latest guidance."
    )
    refs = adr_checker.detect_potential_adr_references(text)
    # Dedup preserves first-occurrence casing.
    assert "ADR-042" in refs
    assert any(r.upper().startswith("ADR 057") or r.upper() == "ADR 057" for r in refs)
    assert any(r.lower().startswith("adr-099") for r in refs)


def test_detect_finds_decision_lines():
    text = "Decision: Use Redis sliding-window for rate limits.\nDecision:Prefer cell-based isolation."
    refs = adr_checker.detect_potential_adr_references(text)
    assert any("Decision" in r for r in refs)
    assert len(refs) >= 1


def test_detect_finds_architecture_decision_record_phrase():
    text = "As documented in the Architecture Decision Record for mandate handling..."
    refs = adr_checker.detect_potential_adr_references(text)
    assert any("Architecture Decision Record" in r for r in refs)


def test_detect_dedupes_preserving_order():
    text = "ADR-042 is mentioned. Also ADR-042 again. Then ADR-058 is separate. And ADR-042 a third time."
    refs = adr_checker.detect_potential_adr_references(text)
    # ADR-042 appears once, ADR-058 once, in document order.
    assert refs == ["ADR-042", "ADR-058"]


def test_detect_empty_and_no_matches():
    assert adr_checker.detect_potential_adr_references("") == []
    assert adr_checker.detect_potential_adr_references("no markers here at all") == []
    assert adr_checker.detect_potential_adr_references(None or "") == []


# ──────────────────────────────────────────────────────────────────────────────
# extract_concerns_section — pure parser
# ──────────────────────────────────────────────────────────────────────────────

_BRD_WITHOUT_CONCERNS = """# BRD

## 1. Executive Summary

Some exec summary text.

## 2. Functional Requirements

FR-01: Do the thing.

## Sources

- [1] source.pdf
"""


_BRD_WITH_CONCERNS = """# BRD

## 1. Executive Summary

Some exec summary text.

## Design Review Concerns

### Concern 1: Rate limit algorithm conflict
- **Prior decision:** ADR-042 standardised Redis sliding-window
- **New design claim:** We use token bucket instead
- **Resolution proposed:** Align with ADR-042 — switch to sliding-window

### Concern 2: Enterprise tier scoping
- **Prior decision:** Tier check happens at ingress filter
- **New design claim:** Tier check moved into app layer
- **Resolution proposed:** Update prior ADR — ingress doesn't see tier headers

## Sources

- [1] source.pdf
"""


def test_extract_concerns_absent_returns_empty_string():
    assert adr_checker.extract_concerns_section(_BRD_WITHOUT_CONCERNS) == ""


def test_extract_concerns_returns_section_with_header():
    section = adr_checker.extract_concerns_section(_BRD_WITH_CONCERNS)
    assert section.startswith("## Design Review Concerns")
    assert "Concern 1" in section
    assert "Concern 2" in section
    # Section must stop before the Sources header.
    assert "[1] source.pdf" not in section
    assert "## Sources" not in section


def test_extract_concerns_case_insensitive_header():
    md = "## DESIGN review concerns\n\n- some concern body\n\n## Next"
    section = adr_checker.extract_concerns_section(md)
    assert section.startswith("## DESIGN review concerns")
    assert "some concern body" in section
    assert "## Next" not in section


def test_extract_concerns_trailing_section_to_eof():
    """When the Concerns section is the last section in the doc."""
    md = "## 1. Intro\n\nIntro body.\n\n## Design Review Concerns\n\n- concern body here\n"
    section = adr_checker.extract_concerns_section(md)
    assert section.startswith("## Design Review Concerns")
    assert "concern body here" in section


def test_extract_concerns_empty_input_returns_empty():
    assert adr_checker.extract_concerns_section("") == ""


def test_extract_concerns_three_hash_header_accepted():
    """A ### Design Review Concerns subsection also matches."""
    md = "## Parent\n\n### Design Review Concerns\n\n- a concern\n\n## Next"
    section = adr_checker.extract_concerns_section(md)
    assert "a concern" in section
    assert "## Next" not in section
