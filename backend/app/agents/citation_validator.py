# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Citation validator (Slice 9).

Parses markdown output from agents that enforce inline `[N]` citations
(plan §6.5 "no citation = no claim"). Identifies which paragraphs carry at
least one citation, flags uncited factual paragraphs, and verifies the
Sources section lists every cited `N`.

Pure — no LLM, no I/O. Safe to run synchronously after streaming completes.

Usage:
    from app.agents.citation_validator import validate
    report = validate(markdown_output, num_sources=len(chunks))

Returns:
    {
      "validated": bool,                # True when zero uncited-claim paragraphs
      "citation_count": int,            # total [N] references in body
      "unique_citations": list[int],    # sorted set of N's actually cited
      "cited_paragraphs": int,
      "uncited_claim_paragraphs": list[str],   # excerpts (≤140 chars)
      "explicit_no_source_paragraphs": int,    # prefixed with [NO SOURCE]
      "out_of_range_citations": list[int],     # N > num_sources
      "sources_section_present": bool,
      "sources_section_complete": bool,        # every cited N appears in Sources
    }
"""
from __future__ import annotations

import re
from typing import Optional

# Matches [N], [1,2], [1, 2], [1][2], [NO SOURCE]
_CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_NO_SOURCE_RE = re.compile(r"\[NO\s+SOURCE\]", re.IGNORECASE)

# Matches a "## Sources" section header (case-insensitive).
_SOURCES_HEADER_RE = re.compile(r"^##+\s+sources\b", re.IGNORECASE | re.MULTILINE)

# Lines that are CLAIMS worth gating — prose paragraphs, not just headings,
# empty lines, bullet intros, or the Sources section itself.
_CLAIM_EXCERPT_LEN = 140
_MIN_CLAIM_CHARS = 40       # below this, too short to demand a citation
_MAX_UNCITED_REPORTED = 12  # cap report size


def _extract_citations(text: str) -> list[int]:
    """Flatten `[1,2,3]` and `[1][2]` into [1, 2, 3, 1, 2]."""
    out: list[int] = []
    for m in _CITATION_RE.finditer(text):
        for part in m.group(1).split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
    return out


def _split_body_and_sources(markdown: str) -> tuple[str, Optional[str]]:
    """Return (body_before_sources, sources_section) — either may be empty.

    Sources section starts at the first `## Sources` heading and runs to EOF.
    """
    m = _SOURCES_HEADER_RE.search(markdown)
    if not m:
        return markdown, None
    return markdown[: m.start()], markdown[m.start() :]


def _paragraphs_of(body: str) -> list[str]:
    """Split into paragraphs by blank lines; strip each; drop empties."""
    raw = re.split(r"\n\s*\n", body)
    return [p.strip() for p in raw if p.strip()]


def _is_claim_paragraph(paragraph: str) -> bool:
    """A paragraph is a "claim" worth gating if it's prose of meaningful length.
    Excluded: headings, short stubs, and pure bullet/numeric lists are NOT
    excluded — a numbered-list item can be a factual claim too. We only skip
    pure heading-only paragraphs and paragraphs shorter than _MIN_CLAIM_CHARS.
    """
    if len(paragraph) < _MIN_CLAIM_CHARS:
        return False
    # Heading-only paragraph (starts with # and is one line)
    if paragraph.startswith("#") and "\n" not in paragraph:
        return False
    return True


def _excerpt(paragraph: str, max_len: int = _CLAIM_EXCERPT_LEN) -> str:
    one_line = re.sub(r"\s+", " ", paragraph).strip()
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 1].rstrip() + "…"


def validate(markdown: str, *, num_sources: Optional[int] = None) -> dict:
    """Validate inline citations in agent markdown output.

    Args:
        markdown: The full agent response text.
        num_sources: Known number of source chunks (enables out-of-range check).
                     If None, out-of-range flag is skipped.

    Returns:
        Report dict — see module docstring.
    """
    body, sources_section = _split_body_and_sources(markdown or "")

    paragraphs = _paragraphs_of(body)
    claim_paragraphs = [p for p in paragraphs if _is_claim_paragraph(p)]

    cited_paras = 0
    no_source_paras = 0
    uncited_excerpts: list[str] = []

    for p in claim_paragraphs:
        has_citation = bool(_CITATION_RE.search(p))
        has_no_source = bool(_NO_SOURCE_RE.search(p))
        if has_citation:
            cited_paras += 1
        elif has_no_source:
            no_source_paras += 1
        else:
            if len(uncited_excerpts) < _MAX_UNCITED_REPORTED:
                uncited_excerpts.append(_excerpt(p))

    all_citations = _extract_citations(body)
    unique_sorted = sorted(set(all_citations))

    out_of_range: list[int] = []
    if num_sources is not None and num_sources > 0:
        out_of_range = sorted({n for n in unique_sorted if n > num_sources or n < 1})

    # Sources section completeness — does the Sources block mention every cited N?
    sources_present = sources_section is not None
    sources_complete = True
    if sources_present and sources_section is not None:
        source_ns_in_sources = sorted(set(_extract_citations(sources_section)))
        missing_in_sources = [n for n in unique_sorted if n not in source_ns_in_sources]
        sources_complete = not missing_in_sources

    validated = (not uncited_excerpts) and (not out_of_range)

    return {
        "validated":                       validated,
        "citation_count":                  len(all_citations),
        "unique_citations":                unique_sorted,
        "cited_paragraphs":                cited_paras,
        "uncited_claim_paragraphs":        uncited_excerpts,
        "explicit_no_source_paragraphs":   no_source_paras,
        "out_of_range_citations":          out_of_range,
        "sources_section_present":         sources_present,
        "sources_section_complete":        sources_complete,
    }
