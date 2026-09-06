# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""LLM cost calculator — per-model price table + USD computation.

Used by `core.observability.record_llm_call_kwargs` to attach a `cost_usd`
field to every `LlmCallTrace`. Input is the raw token counts the provider
returned (input / output / optional Anthropic prompt-cache hits and
writes); output is a USD float.

Default rates are USD per million tokens for the model families this platform
exercises today. Operators can override or extend at runtime via the
`LLM_COST_OVERRIDES_JSON` env var (a JSON object of the same shape).

Unknown model → returns `None` and emits a single one-time WARN. The
trace still ships with `cost_usd=None`; dashboards aggregate around the
gap. This is intentional — failing closed (raising) on an unknown
model would silently nuke logging on a model upgrade.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# USD per million tokens. Lookup is by exact model id; aliases collapse
# down to the same family below. Numbers track public Anthropic / OpenAI
# pricing as of January 2026.
MODEL_COST_PER_MTOK: dict[str, dict[str, float]] = {
    # Anthropic Claude family
    "claude-opus-4-8":            {"input": 15.0, "output": 75.0,
                                   "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4-7":            {"input": 15.0, "output": 75.0,
                                   "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4-6":            {"input": 15.0, "output": 75.0,
                                   "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6":          {"input":  3.0, "output": 15.0,
                                   "cache_read": 0.30, "cache_write":  3.75},
    # List rates; Anthropic bills introductory $2/$10 (cache scaled likewise) through
    # 2026-08-31 — so cost_usd OVERCOUNTS ~33% until then. Override via
    # LLM_COST_OVERRIDES_JSON if exact-bill tracking matters before September.
    "claude-sonnet-5":            {"input":  3.0, "output": 15.0,
                                   "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4-5":           {"input":  1.0, "output":  5.0,
                                   "cache_read": 0.10, "cache_write":  1.25},
    "claude-haiku-4-5-20251001":  {"input":  1.0, "output":  5.0,
                                   "cache_read": 0.10, "cache_write":  1.25},

    # OpenAI
    "gpt-4o":                     {"input":  2.5, "output": 10.0},
    "gpt-4o-mini":                {"input":  0.15, "output": 0.60},
    "gpt-5.2-mini":               {"input":  0.15, "output": 0.60},

    # Google Gemini (paid-tier reference pricing; free-tier test keys may bill as 0)
    "gemini-3.5-flash":           {"input":  1.50, "output": 9.00,
                                   "cache_read": 0.15},
    "gemini-3.1-flash-lite":      {"input":  0.25, "output": 1.50,
                                   "cache_read": 0.025},
    "gemini-2.0-flash":           {"input":  0.10, "output": 0.40,
                                   "cache_read": 0.025},

    # Local — free
    "ollama/phi3:mini":           {"input": 0.0, "output": 0.0},
    "phi3:mini":                  {"input": 0.0, "output": 0.0},
    "gpt-oss:120b-cloud":         {"input": 0.0, "output": 0.0},
}


_warned_unknown: set[str] = set()
_overrides_loaded = False


def _load_overrides() -> None:
    """Merge `LLM_COST_OVERRIDES_JSON` env over the default table once."""
    global _overrides_loaded
    if _overrides_loaded:
        return
    _overrides_loaded = True
    raw = (os.getenv("LLM_COST_OVERRIDES_JSON") or "").strip()
    if not raw:
        return
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("LLM_COST_OVERRIDES_JSON did not parse: %s — using defaults", exc)
        return
    if not isinstance(overrides, dict):
        logger.warning("LLM_COST_OVERRIDES_JSON is not an object — using defaults")
        return
    for model, rates in overrides.items():
        if not isinstance(rates, dict):
            continue
        MODEL_COST_PER_MTOK.setdefault(model, {}).update(rates)
    logger.info("LLM cost overrides loaded for %d model(s)", len(overrides))


def compute_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Optional[float]:
    """Return USD cost for one LLM call, or None if model is unpriced.

    Cache rates default to the regular input rate when not specified
    explicitly — undercount the discount rather than over-count.
    """
    _load_overrides()
    if not model:
        return None
    rates = MODEL_COST_PER_MTOK.get(model)
    if rates is None:
        if model not in _warned_unknown:
            logger.warning(
                "LLM cost: no price entry for model=%s — cost will be "
                "logged as null. Add it to MODEL_COST_PER_MTOK or set "
                "LLM_COST_OVERRIDES_JSON to fix.", model,
            )
            _warned_unknown.add(model)
        return None

    in_rate    = rates.get("input",  0.0)
    out_rate   = rates.get("output", 0.0)
    cr_rate    = rates.get("cache_read",  in_rate)
    cw_rate    = rates.get("cache_write", in_rate)

    cost = (
        (input_tokens       * in_rate)
        + (output_tokens    * out_rate)
        + (cache_read_tokens  * cr_rate)
        + (cache_write_tokens * cw_rate)
    ) / 1_000_000.0

    # Round to 6 decimals — fractions of a cent matter for the high-volume
    # dashboards but storing more precision than the API rate provides
    # is theatre.
    return round(cost, 6)
