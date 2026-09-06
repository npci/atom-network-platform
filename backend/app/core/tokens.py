# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Token-aware text measurement for prompt budgeting (Phase 0.2).

The prior approach was `len(text) // 4`, a chars-per-token proxy used in
[retrieval.build_context] and [observability.estimate_prompt_chars]. That
overestimates English (~3.5 chars/token on Claude) and underestimates code
(~5 chars/token), causing both silent truncation and wasteful under-fill.

This module provides a single `count_tokens(text, model)` that prefers a
real tokeniser when one is available and falls back to the legacy heuristic
when not — so callers can opt in without forcing a hard dependency:

    - For OpenAI / GPT / AiNxt models, use `tiktoken` if installed.
    - For Claude models, use the Anthropic SDK's `count_tokens` if available
      (older SDKs) or `tokenizers`-backed estimation; we cache results for
      short inputs since the SDK call is comparatively expensive.
    - Otherwise fall back to `max(1, len(text) // 4)`.

The fallback is deliberate: every other Phase-0 piece (budget check,
context packer) calls into this module, so it must NEVER raise on absent
optional dependencies. Tests should monkeypatch `count_tokens` directly
instead of fighting tokeniser availability.

Usage:
    from app.core.tokens import count_tokens, count_messages_tokens
    n = count_tokens("hello world", model="claude-sonnet-4-6")
    n = count_messages_tokens(system, messages, model=...)
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# Hard floor / sane default — even an empty string costs ≥1 token in most
# tokenisers (BOS / chat-template overhead). Keep at 1 so downstream
# division never hits zero.
_MIN_TOKENS = 1

# Char-per-token fallback ratio. Tuned to ~3.8 — closer to Claude / GPT
# English mean than the legacy 4.0, but conservative enough not to
# under-budget code. Override via `TOKEN_FALLBACK_RATIO` env var.
import os as _os
_FALLBACK_RATIO = float(_os.getenv("TOKEN_FALLBACK_RATIO", "3.8"))


def _is_openai_family(model: str | None) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(m.startswith(p) for p in ("gpt-", "o1-", "o3-", "text-embedding-", "text-davinci"))


def _is_claude_family(model: str | None) -> bool:
    if not model:
        return False
    return "claude" in model.lower()


# ── tiktoken (OpenAI / GPT) ───────────────────────────────────────────────────

@lru_cache(maxsize=8)
def _tiktoken_encoder(model: str | None):
    """Return a tiktoken encoder for `model`, or None if unavailable.

    `lru_cache` keeps one encoder per model string so we don't re-load on
    every call. None caches too, so a missing package costs one import
    attempt for the whole process.
    """
    try:
        import tiktoken  # type: ignore
    except ImportError:
        return None
    try:
        if model:
            return tiktoken.encoding_for_model(model)
    except KeyError:
        pass
    # Fallback to cl100k_base — covers all current OpenAI chat models well.
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception as e:  # pragma: no cover
        logger.debug("tiktoken get_encoding failed: %s", e)
        return None


def _count_with_tiktoken(text: str, model: str | None) -> int | None:
    """Token count via tiktoken, or None when tiktoken isn't usable."""
    enc = _tiktoken_encoder(model)
    if enc is None:
        return None
    try:
        return len(enc.encode(text or ""))
    except Exception as e:  # pragma: no cover
        logger.debug("tiktoken encode failed: %s", e)
        return None


# ── Anthropic (Claude) ────────────────────────────────────────────────────────

# Older anthropic SDKs (<0.40) shipped a sync `Anthropic().count_tokens(text)`
# helper. Newer SDKs moved this server-side, behind `client.messages.count_tokens`.
# We try both and gracefully give up if neither works without a network call —
# the budget path must remain offline-capable.

@lru_cache(maxsize=1)
def _anthropic_offline_counter():
    """Return a callable `(text) -> int` for Claude using the local
    Anthropic SDK helpers, or None if no offline counter is available.

    Order of preference:
      1. `anthropic.Anthropic().count_tokens(text)` — older SDK, fully local.
      2. None (newer SDKs require an API call we don't want to issue here).
    """
    try:
        import anthropic  # type: ignore
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(api_key="")  # api_key unused for local count
        # SDK exposes a sync `count_tokens` that works without auth.
        # Probe with a tiny string to confirm it actually works locally.
        if hasattr(client, "count_tokens"):
            try:
                _ = client.count_tokens("x")  # type: ignore[attr-defined]
                return client.count_tokens  # type: ignore[attr-defined]
            except Exception:
                return None
    except Exception:
        return None
    return None


def _count_with_anthropic(text: str) -> int | None:
    fn = _anthropic_offline_counter()
    if fn is None:
        return None
    try:
        return int(fn(text or ""))
    except Exception as e:  # pragma: no cover
        logger.debug("anthropic count_tokens failed: %s", e)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

# Cache small inputs because builders often repeat the same boilerplate
# (e.g. file-tree headers, citation footer). Bigger inputs aren't cached
# to avoid pinning prompts in memory.
@lru_cache(maxsize=4096)
def _count_cached(text: str, model: str | None) -> int:
    return _count_uncached(text, model)


def _count_uncached(text: str, model: str | None) -> int:
    if not text:
        return _MIN_TOKENS
    n: int | None = None
    if _is_openai_family(model):
        n = _count_with_tiktoken(text, model)
    elif _is_claude_family(model):
        n = _count_with_anthropic(text)
        if n is None:
            # Claude shares cl100k-ish tokenisation closely enough to use
            # tiktoken as a fallback. Slightly inaccurate but better than
            # the chars/4 heuristic.
            n = _count_with_tiktoken(text, model="gpt-4")
    else:
        # Unknown / unset model — try tiktoken first, then char heuristic.
        n = _count_with_tiktoken(text, model)
    if n is None:
        n = max(_MIN_TOKENS, int(len(text) / _FALLBACK_RATIO))
    return max(_MIN_TOKENS, int(n))


def count_tokens(text: str, model: str | None = None) -> int:
    """Return the number of tokens in `text` under `model`'s tokeniser.

    Falls back to a chars-per-token heuristic when the tokeniser library
    is unavailable. Never raises. Small inputs are LRU-cached.
    """
    if not text:
        return _MIN_TOKENS
    if len(text) <= 400:
        return _count_cached(text, model)
    return _count_uncached(text, model)


def count_messages_tokens(
    system: str | list[dict] | None,
    messages: list[dict] | None,
    model: str | None = None,
) -> int:
    """Total tokens across system prompt + every message body.

    Accepts either a plain `system` string OR Anthropic-style segmented
    system blocks (a list of `{type, text, ...}` dicts) so prompt-cached
    callers (Phase 6.2) work without changing this signature.
    """
    total = 0
    if isinstance(system, str):
        total += count_tokens(system, model=model)
    elif isinstance(system, list):
        for seg in system:
            if isinstance(seg, dict):
                txt = seg.get("text") or ""
                if isinstance(txt, str):
                    total += count_tokens(txt, model=model)
    for m in messages or []:
        body = m.get("content") if isinstance(m, dict) else None
        if isinstance(body, str):
            total += count_tokens(body, model=model)
        elif isinstance(body, list):
            # Anthropic content-block messages: [{type:"text", text:"..."}]
            for blk in body:
                if isinstance(blk, dict):
                    txt = blk.get("text") or ""
                    if isinstance(txt, str):
                        total += count_tokens(txt, model=model)
    return total


# ── Model context windows (used by the Phase-0.3 budget check) ────────────────

# Conservative defaults. When a model isn't in the map we fall back to 200k
# (Claude family default) which is still safer than no check at all.
# Update this map as new models ship; absent entries are not an error.
_MODEL_CONTEXT_WINDOW: dict[str, int] = {
    # Claude family
    "claude-3-5-haiku":          200_000,
    "claude-3-5-sonnet":         200_000,
    "claude-3-7-sonnet":         200_000,
    "claude-haiku-4-5":          200_000,
    "claude-sonnet-4-5":         200_000,
    # The 1M-context models. Every value below was read from the Models API
    # (GET /v1/models/{id} -> max_input_tokens) on 2026-09-05, not inferred from
    # a sibling model — the previous "same tier/fork assumption" note on
    # claude-sonnet-5 is now confirmed rather than assumed.
    #
    # claude-opus-4-8 is agentic_reviewer_model's DEFAULT (config.py), and its
    # absence here meant the app's own default budgeted every review at the
    # 200k fallback — a fifth of the real window — silently truncating context
    # while logging "unknown model" on every call.
    "claude-sonnet-4-6":         1_000_000,
    "claude-sonnet-5":           1_000_000,
    "claude-opus-4-6":           1_000_000,
    "claude-opus-4-7":           1_000_000,
    "claude-opus-4-8":           1_000_000,
    "claude-fable-5":            1_000_000,
    # OpenAI family
    "gpt-4o":                    128_000,
    "gpt-4o-mini":               128_000,
    "gpt-4":                     8_192,
    "gpt-4-turbo":               128_000,
    "o1":                        200_000,
    "o1-mini":                   128_000,
    "o3-mini":                   200_000,
    # Gemini family
    "gemini-3.5-flash":           1_000_000,
    "gemini-3.1-flash-lite":      1_000_000,
    "gemini-3-flash-preview":     1_000_000,
    "gemini-2.5-flash":           1_000_000,
    "gemini-2.0-flash":           1_000_000,
}

_DEFAULT_CONTEXT_WINDOW = 200_000


def model_context_window(model: str | None) -> int:
    """Return the max input+output tokens for `model`, or a safe default.

    Logs a warning when an unrecognised model falls back to the default,
    so operators can add it to _MODEL_CONTEXT_WINDOW.
    """
    if not model:
        return _DEFAULT_CONTEXT_WINDOW
    key = model.strip()
    if key in _MODEL_CONTEXT_WINDOW:
        return _MODEL_CONTEXT_WINDOW[key]
    # Allow date-suffixed model IDs like "claude-sonnet-4-6-20260101" by
    # stripping a trailing -<digits> segment.
    parts = key.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and parts[0] in _MODEL_CONTEXT_WINDOW:
        return _MODEL_CONTEXT_WINDOW[parts[0]]
    logger.warning(
        "model_context_window: unknown model %r — falling back to %d. "
        "Add it to _MODEL_CONTEXT_WINDOW in app/core/tokens.py for accurate budgeting.",
        model, _DEFAULT_CONTEXT_WINDOW,
    )
    return _DEFAULT_CONTEXT_WINDOW
