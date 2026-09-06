# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ADR contradiction check (Slice 11).

Plan §7.2: "Cross-check against retrieved ADRs — if the new design contradicts
a prior ADR, flag explicitly."

This module provides three pure functions:

  - `prompt_suffix()`        → text to append to a design-agent system prompt
                               instructing it to cross-check retrieved context
                               against the new design and emit a
                               `## Design Review Concerns` section on conflict.

  - `detect_potential_adr_references(text)` → list of ADR-ish markers found
                               (e.g. "ADR-042", "Decision:"). Used by callers
                               to decide whether the suffix is worth appending
                               (skip the prompt cost when no ADRs are in play).

  - `extract_concerns_section(markdown)` → text of a `## Design Review
                               Concerns` section from agent output, or "" if
                               absent. Callers surface this to reviewers /
                               the ValidationPanel.

All three are pure — no LLM, no I/O. Wired into brd.py + tech_spec.py via
prompt suffix at generation time; the extractor can be run post-hoc on
accumulated output.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import re


# Patterns a retrieved chunk might use to mark a past decision.
# Case-insensitive. Numbered ADRs match "ADR-042" / "ADR 042" / "ADR042";
# decision lines match "Decision:" at line start.
_ADR_PATTERN = re.compile(
    r"\b(?:ADR[-\s]?\d+|Decision:\s*\w+|Architecture Decision Record)\b",
    re.IGNORECASE,
)

# Header for the Concerns section the agent is instructed to emit.
_CONCERNS_HEADER_RE = re.compile(
    r"^##+\s+Design\s+Review\s+Concerns\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Level-prefix regex at the start of a line (used dynamically below).
_HEADER_LEVEL_RE = re.compile(r"^(#+)\s+", re.MULTILINE)


PROMPT_SUFFIX = load_prompt("agents/adr_checker/prompt_suffix.md")


def prompt_suffix() -> str:
    """Return the suffix to append to a design-agent system prompt."""
    return PROMPT_SUFFIX


def detect_potential_adr_references(text: str) -> list[str]:
    """Find ADR-ish markers in the retrieved context.

    Returns a deduplicated, order-preserving list of matches as they appear
    in the input. A non-empty return is a signal that cross-checking is
    worth the prompt cost; an empty return means the caller can skip the
    suffix.
    """
    if not text:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for m in _ADR_PATTERN.finditer(text):
        key = m.group(0).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(m.group(0).strip())
    return ordered


def extract_concerns_section(markdown: str) -> str:
    """Return the text of a `## Design Review Concerns` section, or "" if absent.

    Returns the body text INCLUDING the header line, so callers can surface
    the raw markdown as-is. Section ends at the next header of the SAME
    level or shallower (e.g. `##` Concerns ends at the next `##` or `#`;
    nested `###` sub-sections are included in the output).
    """
    if not markdown:
        return ""
    m = _CONCERNS_HEADER_RE.search(markdown)
    if not m:
        return ""

    # Determine the level (number of leading #) of the Concerns header.
    header_line = m.group(0)
    level_match = _HEADER_LEVEL_RE.match(header_line)
    level = len(level_match.group(1)) if level_match else 2

    # End-of-section: next header at same-or-shallower level.
    end_re = re.compile(r"^#{1," + str(level) + r"}\s+", re.MULTILINE)

    remainder = markdown[m.end():]
    end_match = end_re.search(remainder)
    if end_match:
        body = remainder[: end_match.start()]
    else:
        body = remainder
    return (m.group(0) + body).strip()
