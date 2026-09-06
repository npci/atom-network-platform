# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Synthetic natural-language code summarizer (Slice 4).

Produces a short NL paragraph describing what a code symbol *does*, intended
as the third embedding view alongside signature + body. Plan §4.2.3 calls this
"the single highest-leverage trick for cross-modal retrieval" — PO prompts use
product language, code uses identifier language, the NL summary mediates.

Design:
  - Async because `app.core.llm.call_llm` is async. Sync callers (the current
    synchronous ingestion pipeline) wrap via `asyncio.run()`.
  - Fail-closed: any exception or empty LLM response returns `""`. Caller
    skips emitting the nl_summary view row for that symbol — body + signature
    views still land. A missing summary is *better* than a broken summary.
  - Input truncated to keep token cost bounded. Most symbols are short; long
    classes get a head-truncation.

Model selection (3-layer override, most-specific first):
  1. CODE_SUMMARIZER_MODEL env var — wins outright if set
  2. ROUTING_MODEL_UTILITY env var (via Slice 27a router) — applies to
     utility-purpose agents in general
  3. AINXT_MODEL / CLAUDE_MODEL / OPENAI_MODEL — provider default

Prompt philosophy: purpose, not implementation. If someone asks "how does
rate limiting work?", we want the summary to say "enforces per-tenant
request quotas using a Redis sliding window", not "increments a counter and
compares to limit."
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import logging
import os
import re

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 3000     # head-truncation guard
MAX_SUMMARY_TOKENS = 250   # ~80 words

# Per-agent override (declared on Settings as `code_summarizer_model`).
# When set, wins over Slice 27a routing for this one agent. Useful when
# an operator wants to A/B different models (e.g. claude-haiku vs
# gpt-5.2-mini) for code summarisation specifically without changing
# the routing-model for every other utility agent.
#
# Order of precedence the env var goes through:
#   1. backend/.env CODE_SUMMARIZER_MODEL=<id>
#   2. shell environment CODE_SUMMARIZER_MODEL=<id>
# Both are read by pydantic-settings into `settings.code_summarizer_model`.
def _code_summarizer_model_override() -> str | None:
    try:
        from app.core.config import settings
        val = (settings.code_summarizer_model or "").strip()
    except Exception:
        # Defensive: if config import fails (extremely rare; would mean a
        # broader Settings issue), fall back to a direct env read so the
        # summariser still has a way to honour the override.
        val = (os.getenv("CODE_SUMMARIZER_MODEL") or "").strip()
    return val or None


# Output validation — reject responses that look like model-error text or
# contain refusal markers. Returning these into the embedding store would
# pollute retrieval with garbage vectors. Fail-closed (empty string) means
# the nl_summary view row is skipped; body + signature views still land.
_REFUSAL_MARKERS = (
    "i cannot",
    "i can't",
    "i'm sorry",
    "i am sorry",
    "as an ai",
    "i don't have",
    "i do not have",
    "[error",
    "<error",
    "sorry, i can",
    "i'm unable",
    "unable to provide",
    "no information",
)


def _validate_summary(text: str, *, min_chars: int = 20, max_chars: int = 1500) -> str:
    """Return the summary if it looks valid, else ''. Fail-closed.

    Rejects:
      - empty / whitespace-only
      - too short (< min_chars) — model probably truncated or refused
      - too long (> max_chars) — model ignored the 80-word cap and rambled;
        the embedding view would be dominated by noise
      - refusal phrases ("I cannot", "as an AI", etc.)
      - bracketed-error patterns ([Error: ...], <Error>...</Error>)
      - repetitive output (same line ≥ 5 times — model got into a loop)
    """
    if not text:
        return ""
    s = text.strip()
    if not s:
        return ""

    if len(s) < min_chars:
        logger.debug("summary rejected: too short (%d chars): %r", len(s), s[:80])
        return ""
    if len(s) > max_chars:
        logger.debug("summary rejected: too long (%d chars)", len(s))
        return ""

    low = s.lower()
    for marker in _REFUSAL_MARKERS:
        if marker in low[:200]:    # only check the head — body may legitimately mention these phrases
            logger.debug("summary rejected: refusal marker %r in head", marker)
            return ""

    # Repetition guard — same non-blank line repeated 5+ times signals a loop
    line_counts: dict[str, int] = {}
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        line_counts[line] = line_counts.get(line, 0) + 1
        if line_counts[line] >= 5:
            logger.debug("summary rejected: line repeated %d times: %r", line_counts[line], line[:80])
            return ""

    return s

_SYSTEM_PROMPT = load_prompt("rag/code_summarizer/system_prompt.md")


async def synthesize(code: str, language: str, symbol_kind: str) -> str:
    """Produce an NL summary of a code symbol. Returns '' on any failure.

    Args:
        code: The source text of the symbol (the chunk's body).
        language: Language key — 'java' | 'python' | 'typescript' | etc.
        symbol_kind: 'class' | 'method' | 'function' | 'interface' | etc.

    Returns:
        Summary text (≤ ~80 words), or '' when the LLM call fails, returns
        empty, or the input is blank.
    """
    if not code or not code.strip():
        return ""

    snippet = code[:MAX_INPUT_CHARS]
    user_msg = (
        f"Language: {language}\n"
        f"Symbol kind: {symbol_kind}\n"
        f"Code:\n```\n{snippet}\n```\n\n"
        f"Summarize in ≤80 words. Purpose-first, not implementation."
    )

    try:
        # Lazy import so unit tests can patch before import cost is paid.
        from app.core.llm import call_llm
        from app.core.llm_router import pick_model_for_agent
        # Per-agent override (env CODE_SUMMARIZER_MODEL) wins over Slice 27a
        # routing. Falls through to None when both are unset, which means
        # call_llm() uses the provider default (AINXT_MODEL / CLAUDE_MODEL).
        chosen_model = _code_summarizer_model_override() or pick_model_for_agent("code_summarizer")
        result = await call_llm(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=MAX_SUMMARY_TOKENS,
            model=chosen_model,
            agent_name="code_summarizer",
        )
    except Exception as e:
        logger.warning(
            "code_summarizer: LLM call failed for %s %s — skipping: %s",
            language, symbol_kind, e,
        )
        return ""

    validated = _validate_summary(result or "")
    if not validated:
        logger.debug(
            "code_summarizer: invalid/empty response for %s %s — skipping nl_summary view",
            language, symbol_kind,
        )
        return ""
    return validated


def synthesize_sync(code: str, language: str, symbol_kind: str) -> str:
    """Sync wrapper for ingestion paths that aren't yet async.

    Creates a fresh event loop per call — fine for batch ingestion (called a
    few hundred times max per ingest). Don't call this from within an already-
    running async context; use `await synthesize(...)` directly instead.
    """
    import asyncio
    try:
        return asyncio.run(synthesize(code, language, symbol_kind))
    except RuntimeError as e:
        # e.g. "asyncio.run() cannot be called from a running event loop"
        logger.warning("code_summarizer.synthesize_sync called from running loop: %s", e)
        return ""


async def synthesize_batch(
    items: list[tuple[str, str, str]],
    concurrency: int = 16,
) -> list[str]:
    """Concurrent batched summariser.

    Args:
        items: list of (code, language, symbol_kind) tuples — one per symbol
               to summarise. Order is preserved in the output.
        concurrency: max in-flight LLM calls. 16 is a good default for
                     Anthropic / AiNxt; the gateway tolerates much more
                     in practice. Tune via env in `synthesize_batch_sync`.

    Returns:
        list[str] of the same length as `items`. Each element is the NL
        summary (or '' on failure) for the corresponding input.

    Performance: a 13 000-symbol Java repo at concurrency=16 with
    ~4 sec round-trip drops from ~14 hours sequential to ~55 minutes —
    a ~16× wall-clock speedup. Throughput limited by the gateway's
    rate ceiling, not by our event loop.
    """
    import asyncio

    if not items:
        return []

    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _one(code: str, language: str, symbol_kind: str) -> str:
        async with sem:
            return await synthesize(code, language, symbol_kind)

    coros = [_one(code, lang, kind) for (code, lang, kind) in items]
    results = await asyncio.gather(*coros, return_exceptions=False)
    return list(results)


def synthesize_batch_sync(
    items: list[tuple[str, str, str]],
    concurrency: int | None = None,
) -> list[str]:
    """Sync wrapper around `synthesize_batch` for the existing synchronous
    ingestion pipeline.

    Concurrency override precedence:
        1. function arg `concurrency` (if not None)
        2. env var `CODE_SUMMARIZER_CONCURRENCY`
        3. fallback default = 16
    """
    import asyncio
    import os

    if concurrency is None:
        try:
            concurrency = int(os.getenv("CODE_SUMMARIZER_CONCURRENCY", "16"))
        except ValueError:
            concurrency = 16

    if not items:
        return []

    logger.info(
        "code_summarizer.synthesize_batch_sync: items=%d concurrency=%d",
        len(items), concurrency,
    )
    try:
        return asyncio.run(synthesize_batch(items, concurrency=concurrency))
    except RuntimeError as e:
        logger.warning(
            "synthesize_batch_sync called from running loop (%s) — degrading to serial",
            e,
        )
        return [synthesize_sync(c, l, k) for (c, l, k) in items]
