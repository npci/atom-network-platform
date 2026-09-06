# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 0.2 — unit tests for `app.core.tokens`.

These tests deliberately exercise the FALLBACK paths (no tokeniser) so
they pass without `tiktoken` or the Anthropic SDK installed. When those
libraries ARE present the test suite still runs because the public API
guarantees a non-zero positive integer either way.
"""
from __future__ import annotations

import pytest

from app.core import tokens
from app.core.tokens import (
    count_messages_tokens,
    count_tokens,
    model_context_window,
)


# ── count_tokens ─────────────────────────────────────────────────────────────

def test_empty_string_returns_min_tokens():
    assert count_tokens("") == 1
    assert count_tokens(None) == 1  # type: ignore[arg-type]


def test_basic_string_positive():
    assert count_tokens("hello world") >= 1


def test_long_string_grows_monotonically():
    short = count_tokens("hello world")
    long_ = count_tokens("hello world " * 100)
    assert long_ > short


def test_known_models_use_specific_paths():
    # Paths must not raise — even for unrecognised model strings.
    assert count_tokens("x", model="claude-sonnet-4-6") >= 1
    assert count_tokens("x", model="gpt-4o") >= 1
    assert count_tokens("x", model="gpt-4o-mini") >= 1
    assert count_tokens("x", model=None) >= 1
    assert count_tokens("x", model="unknown-model-1234") >= 1


def test_caching_for_short_inputs(monkeypatch):
    """Short inputs go through `_count_cached`. Patch the uncached helper
    and confirm a second call doesn't re-invoke it."""
    calls = {"n": 0}
    real = tokens._count_uncached

    def spy(text, model):
        calls["n"] += 1
        return real(text, model)

    # Replace the cache-miss path. Need to also clear lru_cache.
    monkeypatch.setattr(tokens, "_count_uncached", spy)
    tokens._count_cached.cache_clear()

    text = "abc def ghi"
    n1 = tokens._count_cached(text, "gpt-4o")
    n2 = tokens._count_cached(text, "gpt-4o")
    assert n1 == n2
    assert calls["n"] == 1


def test_count_messages_tokens_aggregates():
    sys = "system prompt"
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    total = count_messages_tokens(sys, msgs, model=None)
    assert total >= count_tokens(sys) + count_tokens("hello") + count_tokens("hi") - 1


def test_count_messages_tokens_with_anthropic_segments():
    """Phase 6.2 — segmented system prompt as list[{type,text,...}]."""
    sys = [
        {"type": "text", "text": "rules block"},
        {"type": "text", "text": "rag context", "cache_control": {"type": "ephemeral"}},
    ]
    msgs = [{"role": "user", "content": "q"}]
    total = count_messages_tokens(sys, msgs, model=None)
    expected = (
        count_tokens("rules block")
        + count_tokens("rag context")
        + count_tokens("q")
    )
    assert total >= expected - 1  # tokenisation may merge whitespace


def test_count_messages_with_anthropic_block_messages():
    msgs = [
        {"role": "user",
         "content": [{"type": "text", "text": "hello"},
                     {"type": "text", "text": " world"}]},
    ]
    n = count_messages_tokens(None, msgs, model=None)
    assert n >= 1


# ── model_context_window ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "model,expected_min",
    [
        ("claude-sonnet-4-6",  500_000),  # 1M-context fork
        ("claude-haiku-4-5",   100_000),
        ("gpt-4o",             100_000),
        ("gpt-4",              4_000),
    ],
)
def test_known_model_windows(model, expected_min):
    assert model_context_window(model) >= expected_min


def test_unknown_model_falls_back_to_default():
    assert model_context_window("totally-made-up-model") == 200_000


def test_date_suffixed_model_strips_suffix():
    # "claude-sonnet-4-6-20260101" should resolve via the base name.
    assert model_context_window("claude-sonnet-4-6-20260101") == \
           model_context_window("claude-sonnet-4-6")


def test_none_model_returns_default():
    assert model_context_window(None) == 200_000
    assert model_context_window("") == 200_000


# ── fallback ratio is configurable ───────────────────────────────────────────

def test_fallback_ratio_respected_when_no_tokeniser(monkeypatch):
    """When no tokeniser is available, fall back to chars / FALLBACK_RATIO."""
    monkeypatch.setattr(tokens, "_count_with_tiktoken", lambda *a, **kw: None)
    monkeypatch.setattr(tokens, "_count_with_anthropic", lambda *a, **kw: None)
    tokens._count_cached.cache_clear()

    text = "x" * 380  # short enough for cache but distinctive
    n = count_tokens(text, model="totally-unknown")
    expected = max(1, int(len(text) / tokens._FALLBACK_RATIO))
    assert n == expected
