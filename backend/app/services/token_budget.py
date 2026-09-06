# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Input-token budgeting for BRD / TSD / Product-Kit prompt assembly.

Claude Sonnet supports 200k input tokens but (a) cost scales linearly and
(b) quality + latency both degrade past ~50-100k. Without budgeting, long
multi-turn BRD conversations with full RAG + proposals + research + canvas
+ history compound silently until a 413 or truncation error surfaces.

This module gives callers a cheap char-based token estimate and a priority
trimmer that preserves the most authoritative parts of a prompt.

Priority (highest to lowest — last to be trimmed first):
  1. system             — never trimmed
  2. new_user_message   — never trimmed
  3. clarification      — PM-authored answers are authoritative
  4. proposals          — corpus-grounded ground truth
  5. rag_context        — source-tagged evidence (trim from the bottom)
  6. brd_content        — for TSD gen; summarisable
  7. research_report    — can be abbreviated
  8. canvas_content     — can be abbreviated
  9. conversation_history — oldest turns trimmed first

Char-based estimate: 1 token ≈ 4 characters. This is a deliberate heuristic,
not an API call — budgeting decisions don't need exact counts and we avoid
paying an extra round-trip.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 1 token ≈ 4 chars. Slightly conservative (actual Claude is closer to 3.5-4).
_CHARS_PER_TOKEN = 4.0

# Default input budget. Well below Claude 200k to leave room for:
#   - max_tokens output (up to 8192)
#   - system prompt overhead
#   - safety margin for under-counting
DEFAULT_MAX_INPUT_TOKENS = 150_000

# Per-section hard caps — applied BEFORE the priority trimmer as a cheap pre-filter.
# These are char counts, not tokens.
_SECTION_CAPS: dict[str, int] = {
    "research_report": 12_000,
    "canvas_content":  6_000,
    # The TSD is authored against the full BRD (accuracy upgrade W0.1). 16k chars
    # (~4k tokens) still clipped large BRDs — raised so a normal full BRD passes
    # untouched; the cap only guards against a pathological/uploaded outlier.
    "brd_content":     60_000,
    "rag_context":     32_000,
    "proposals":       12_000,
}

# Trim order (low priority -> high). Walked left-to-right until we fit.
_TRIM_ORDER: list[str] = [
    "conversation_history",
    "canvas_content",
    "research_report",
    "brd_content",
    "rag_context",
    "proposals",
]


def count_tokens(text: str | None) -> int:
    """Estimated token count for a string (char-based heuristic)."""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN)


def count_parts(parts: dict[str, str]) -> dict[str, int]:
    """Return per-part token counts + total under the special key '_total'."""
    counts = {k: count_tokens(v) for k, v in parts.items()}
    counts["_total"] = sum(counts.values())
    return counts


def _apply_section_caps(parts: dict[str, str]) -> dict[str, str]:
    """Enforce per-section hard char caps before entering the priority trimmer."""
    out: dict[str, str] = {}
    for key, value in parts.items():
        cap = _SECTION_CAPS.get(key)
        if cap is not None and value and len(value) > cap:
            logger.info("token_budget: section-cap trim '%s' %d → %d chars", key, len(value), cap)
            out[key] = value[:cap] + "\n...[truncated: section cap]"
        else:
            out[key] = value or ""
    return out


def budget_context(
    parts: dict[str, str],
    *,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    hard_floor_per_section: int = 500,  # chars — never trim a section below this
) -> tuple[dict[str, str], dict]:
    """Trim prompt parts so the combined token count fits the budget.

    Args:
        parts: dict of named prompt fragments. Unknown keys are passed through
            untouched (treated as never-trim). Priorities are defined by
            _TRIM_ORDER; parts not in that list are treated as higher priority
            than the last entry and are never trimmed by this function.
        max_input_tokens: target total input tokens after trimming.
        hard_floor_per_section: a section is never reduced below this many
            chars (except conversation_history which may be dropped entirely).

    Returns:
        (trimmed_parts, report) where report contains:
            - before: per-section tokens + total
            - after:  per-section tokens + total
            - trims:  list of {"section", "before_chars", "after_chars"}
    """
    # Pre-filter with hard caps so absurdly large inputs don't dominate
    parts = _apply_section_caps(parts)

    before_counts = count_parts(parts)
    report: dict = {"before": before_counts, "trims": [], "after": None}

    total = before_counts["_total"]
    if total <= max_input_tokens:
        report["after"] = dict(before_counts)
        return parts, report

    overage = total - max_input_tokens
    logger.warning(
        "token_budget: prompt exceeds %d tokens by %d — trimming in priority order",
        max_input_tokens, overage,
    )

    trimmed = dict(parts)
    remaining_overage_chars = int(overage * _CHARS_PER_TOKEN)

    for section in _TRIM_ORDER:
        if remaining_overage_chars <= 0:
            break
        current = trimmed.get(section) or ""
        if not current:
            continue

        # conversation_history can be dropped entirely — oldest turns first.
        # We treat it as one blob here for simplicity; callers with richer
        # turn-level data can pre-summarise before passing in.
        floor = 0 if section == "conversation_history" else hard_floor_per_section
        if len(current) <= floor:
            continue

        available_to_trim = len(current) - floor
        cut = min(available_to_trim, remaining_overage_chars)
        new_value = current[: len(current) - cut]
        if new_value and len(new_value) > 0:
            new_value += "\n...[trimmed by token budget]"
        trimmed[section] = new_value
        remaining_overage_chars -= cut
        report["trims"].append({
            "section":      section,
            "before_chars": len(current),
            "after_chars":  len(new_value),
        })
        logger.info(
            "token_budget: trimmed '%s' %d → %d chars", section, len(current), len(new_value),
        )

    after_counts = count_parts(trimmed)
    report["after"] = after_counts

    if after_counts["_total"] > max_input_tokens:
        logger.warning(
            "token_budget: still over budget after all trims (%d > %d tokens) — "
            "callers should consider reducing max_tokens or summarising further",
            after_counts["_total"], max_input_tokens,
        )

    return trimmed, report
