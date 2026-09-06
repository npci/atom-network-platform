# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 9 citation validator.

Pure — the validator has no external dependencies. These tests lock the
parsing, flagging, and sources-section semantics.
"""
from __future__ import annotations

from app.agents import citation_validator


# ──────────────────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────────────────

CLEAN_MARKDOWN = """# Research Report

## 1. Market Research

The network transaction volumes have grown 40x since 2018, crossing 18 billion per month in late 2025 [1]. This puts India well ahead of any other real-time payments market globally [2].

## 2. Product & Ecosystem

The Product Settlement Operator (PSO) role is held by the Authority as the central switch coordinating banks and PSPs [1]. Independent PSPs like Google Pay and PhonePe now process more than 85% of total the network volume [3].

## Sources

- [1] rbi_guidelines/master_direction_payments.pdf
- [2] upi_product_docs/UPI_Complete_Guide.md
- [3] past_brds/psp_onboarding.docx
"""


def test_clean_markdown_passes_validation():
    report = citation_validator.validate(CLEAN_MARKDOWN, num_sources=3)
    assert report["validated"] is True
    assert report["uncited_claim_paragraphs"] == []
    assert report["unique_citations"] == [1, 2, 3]
    assert report["cited_paragraphs"] >= 2
    assert report["sources_section_present"] is True
    assert report["sources_section_complete"] is True
    assert report["out_of_range_citations"] == []


# ──────────────────────────────────────────────────────────────────────────────
# Missing citations on claim paragraphs
# ──────────────────────────────────────────────────────────────────────────────

def test_uncited_claim_paragraph_flagged():
    md = """## Market

The network handles more than 18 billion transactions per month as of late 2025. This number far exceeds any other rail globally including Brazil's Pix and China's Alipay — but no sources were cited above.

## Sources

- [1] source.pdf
"""
    report = citation_validator.validate(md, num_sources=1)
    assert report["validated"] is False
    assert len(report["uncited_claim_paragraphs"]) == 1
    assert "The network handles more than 18 billion" in report["uncited_claim_paragraphs"][0]


def test_multiple_uncited_paragraphs_all_flagged():
    md = """First paragraph of meaningful length without any citation markers at all included.

Second paragraph of meaningful length also without any citation markers present here.

Third paragraph of similar length and likewise contains no citation markers at all.
"""
    report = citation_validator.validate(md)
    assert report["validated"] is False
    assert len(report["uncited_claim_paragraphs"]) == 3


# ──────────────────────────────────────────────────────────────────────────────
# [NO SOURCE] prefix
# ──────────────────────────────────────────────────────────────────────────────

def test_no_source_prefix_counts_separately_not_as_uncited():
    md = """[NO SOURCE] This is a speculative paragraph the model marked as an assumption, which is acceptable per the citation rules. It is long enough to qualify as a claim.

The per-transaction the network limit is one lakh rupees for most banks in India [1].

## Sources

- [1] upi_product_docs/limits.md
"""
    report = citation_validator.validate(md, num_sources=1)
    assert report["validated"] is True  # explicit-no-source doesn't block
    assert report["explicit_no_source_paragraphs"] == 1
    assert report["cited_paragraphs"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# Citation flattening (multiple markers, comma-separated)
# ──────────────────────────────────────────────────────────────────────────────

def test_multiple_bracket_citations_flattened():
    md = """the network is governed jointly by the Authority and RBI [1][2]. The newer Credit line rails add merchant settlement flows [3, 4].

## Sources

- [1] a.pdf
- [2] b.pdf
- [3] c.pdf
- [4] d.pdf
"""
    report = citation_validator.validate(md, num_sources=4)
    assert report["citation_count"] == 4
    assert report["unique_citations"] == [1, 2, 3, 4]
    assert report["validated"] is True


def test_comma_list_citation_flattened():
    md = """A single paragraph attributing a claim to multiple sources via a comma-separated marker [1, 2, 3] format that the LLM might emit.

## Sources

- [1] a.md
- [2] b.md
- [3] c.md
"""
    report = citation_validator.validate(md, num_sources=3)
    assert report["citation_count"] == 3
    assert report["unique_citations"] == [1, 2, 3]


# ──────────────────────────────────────────────────────────────────────────────
# Out-of-range citations
# ──────────────────────────────────────────────────────────────────────────────

def test_out_of_range_citation_flagged():
    md = """A paragraph citing an invalid source marker [9] that doesn't exist in the source list, which should be flagged in the report.

## Sources

- [1] only.md
"""
    report = citation_validator.validate(md, num_sources=1)
    assert report["validated"] is False
    assert 9 in report["out_of_range_citations"]


def test_out_of_range_check_skipped_when_num_sources_none():
    md = """Paragraph with a high citation [99] but no known source count passed to the validator, so it should not be flagged in out_of_range.

## Sources

- [99] x.md
"""
    report = citation_validator.validate(md)  # no num_sources
    assert report["out_of_range_citations"] == []


# ──────────────────────────────────────────────────────────────────────────────
# Sources section
# ──────────────────────────────────────────────────────────────────────────────

def test_missing_sources_section_flagged():
    md = """Paragraph with a citation marker [1] but no Sources section at the end of the document at all.
"""
    report = citation_validator.validate(md, num_sources=1)
    assert report["sources_section_present"] is False


def test_incomplete_sources_section_flagged():
    md = """Claim one cited [1]. Claim two cited separately in another paragraph.

Separate paragraph citing another source [2] entirely for a different reason.

## Sources

- [1] only-one.md
"""
    report = citation_validator.validate(md, num_sources=2)
    assert report["sources_section_present"] is True
    # Cited [1] and [2] in body but Sources only lists [1]
    assert report["sources_section_complete"] is False


# ──────────────────────────────────────────────────────────────────────────────
# Short paragraphs / headings excluded from claim-gating
# ──────────────────────────────────────────────────────────────────────────────

def test_short_paragraphs_not_gated():
    md = """## Heading

Short.

OK.

TBD.

## Sources

- [1] x.md
"""
    report = citation_validator.validate(md, num_sources=1)
    # Short paragraphs shouldn't count against citation coverage.
    assert report["uncited_claim_paragraphs"] == []
    assert report["validated"] is True


def test_heading_only_paragraphs_not_gated():
    md = """# Top Level Heading of Substantial Length Possibly Violating Claim Threshold

## Subheading of similar length to qualify as meaningful paragraph length

## Sources

- [1] x.md
"""
    report = citation_validator.validate(md, num_sources=1)
    assert report["uncited_claim_paragraphs"] == []


# ──────────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────────

def test_empty_input_validates_trivially():
    report = citation_validator.validate("")
    assert report["validated"] is True
    assert report["citation_count"] == 0
    assert report["cited_paragraphs"] == 0


def test_excerpt_truncated():
    long_para = "This is a very long paragraph " * 20  # ~600 chars
    report = citation_validator.validate(long_para)
    assert len(report["uncited_claim_paragraphs"]) == 1
    excerpt = report["uncited_claim_paragraphs"][0]
    assert len(excerpt) <= 141  # 140 + '…'
    assert excerpt.endswith("…")


def test_uncited_excerpts_capped_at_12():
    paragraphs = "\n\n".join(
        f"Paragraph number {i} without a citation marker of length sufficient to trigger the validator flag."
        for i in range(30)
    )
    report = citation_validator.validate(paragraphs)
    assert len(report["uncited_claim_paragraphs"]) == 12  # capped
