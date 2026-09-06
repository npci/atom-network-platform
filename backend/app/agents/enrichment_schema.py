# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""EnrichedStory schema + validator (Slice 10).

Schema mirrors plan §7.1's enrichment agent output exactly. The validator is
pure: takes a dict, returns a coverage report. No LLM, no I/O.

Required fields:
  - title           (str, non-empty)
  - as_a            (str, non-empty)
  - i_want          (str, non-empty)
  - so_that         (str, non-empty)
  - context_summary (str, ≥30 chars)
  - acceptance_criteria (list[str], ≥1)

Recommended fields (not strictly required; presence boosts `field_completeness`):
  - non_functional       (list[str])
  - open_questions       (list[str])
  - affected_components  (list[dict] with repo, files — dict shape tolerated loosely)
  - citations            (list[str])

Scoring:
  - schema_valid: True iff all REQUIRED fields are present and non-empty.
  - field_completeness: (# non-empty required + recommended) / 10.
  - keyword_coverage: (when `required_keywords` supplied) fraction found anywhere
    in the story text (case-insensitive).
"""
from __future__ import annotations

from typing import Any


REQUIRED_FIELDS = ("title", "as_a", "i_want", "so_that", "context_summary",
                   "acceptance_criteria")
RECOMMENDED_FIELDS = ("non_functional", "open_questions", "affected_components",
                      "citations")
ALL_FIELDS = REQUIRED_FIELDS + RECOMMENDED_FIELDS

# Minimum content-summary length (otherwise just noise)
MIN_CONTEXT_SUMMARY_CHARS = 30


# ──────────────────────────────────────────────────────────────────────────────
# Field-level presence checks
# ──────────────────────────────────────────────────────────────────────────────

def _is_populated_str(value: Any, min_chars: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= min_chars


def _is_populated_list(value: Any) -> bool:
    if not isinstance(value, list) or len(value) == 0:
        return False
    # Require at least one non-empty element.
    return any(
        (isinstance(v, str) and v.strip()) or (isinstance(v, dict) and v)
        for v in value
    )


def _field_populated(story: dict, field: str) -> bool:
    value = story.get(field)
    if field in ("title", "as_a", "i_want", "so_that"):
        return _is_populated_str(value)
    if field == "context_summary":
        return _is_populated_str(value, min_chars=MIN_CONTEXT_SUMMARY_CHARS)
    # list-valued fields (acceptance_criteria, non_functional, open_questions,
    # affected_components, citations)
    return _is_populated_list(value)


# ──────────────────────────────────────────────────────────────────────────────
# Keyword coverage (against a gold-supplied required_keywords list)
# ──────────────────────────────────────────────────────────────────────────────

def _flatten_story_text(story: dict) -> str:
    """Flatten the story into one lowercase string for keyword matching."""
    parts: list[str] = []
    for field in ALL_FIELDS:
        value = story.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, str):
                    parts.append(v)
                elif isinstance(v, dict):
                    for subv in v.values():
                        if isinstance(subv, str):
                            parts.append(subv)
                        elif isinstance(subv, list):
                            parts.extend(s for s in subv if isinstance(s, str))
    return " ".join(parts).lower()


# ──────────────────────────────────────────────────────────────────────────────
# Public entry
# ──────────────────────────────────────────────────────────────────────────────

def validate(story: dict, *, required_keywords: list[str] | None = None) -> dict:
    """Validate an EnrichedStory dict. Returns a coverage report.

    Args:
        story: Dict with the EnrichedStory shape.
        required_keywords: Optional list of keywords that should appear somewhere
            in the story. Used by the eval harness against a gold-labeled set.

    Returns report:
        schema_valid         (bool)  all REQUIRED_FIELDS populated
        field_completeness   (float) populated(ALL_FIELDS) / len(ALL_FIELDS)
        missing_required     (list[str])
        missing_recommended  (list[str])
        keyword_coverage     (float) fraction of required_keywords found (0-1);
                                     1.0 when required_keywords is None or empty
        missing_keywords     (list[str])
    """
    if not isinstance(story, dict):
        return {
            "schema_valid":        False,
            "field_completeness":  0.0,
            "missing_required":    list(REQUIRED_FIELDS),
            "missing_recommended": list(RECOMMENDED_FIELDS),
            "keyword_coverage":    1.0 if not required_keywords else 0.0,
            "missing_keywords":    list(required_keywords or []),
        }

    missing_required    = [f for f in REQUIRED_FIELDS    if not _field_populated(story, f)]
    missing_recommended = [f for f in RECOMMENDED_FIELDS if not _field_populated(story, f)]
    populated_count = len([f for f in ALL_FIELDS if _field_populated(story, f)])

    # Keyword coverage
    if required_keywords:
        text = _flatten_story_text(story)
        missing_keywords = [kw for kw in required_keywords if kw.lower() not in text]
        kw_coverage = 1.0 - (len(missing_keywords) / len(required_keywords))
    else:
        missing_keywords = []
        kw_coverage = 1.0

    return {
        "schema_valid":        not missing_required,
        "field_completeness":  round(populated_count / len(ALL_FIELDS), 4),
        "missing_required":    missing_required,
        "missing_recommended": missing_recommended,
        "keyword_coverage":    round(kw_coverage, 4),
        "missing_keywords":    missing_keywords,
    }
