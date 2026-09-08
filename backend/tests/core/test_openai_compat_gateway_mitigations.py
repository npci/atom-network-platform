# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Client-side mitigations for an OpenAI/Anthropic-compatible gateway.

A gateway in front of a provider is allowed to be lossy in ways the provider is
not, and two of those losses are silent rather than loud:

  1. It may collapse or drop `max_tokens` and then report `stop_reason:
     "end_turn"` on a response that was in fact cut off at the budget. A caller
     that trusts `stop_reason` treats a truncated answer as a complete one.
     `_truncated_by_budget` re-derives the truncation from token usage instead.

  2. It may return HTTP 200 with the upstream error rendered as assistant TEXT.
     Nothing raises, so the error becomes model output and flows downstream as
     content. `_is_gateway_error_text` recognises those bodies.

Both are client-side compensation, not provider-specific behaviour: any
compatible gateway can exhibit either, and a direct provider API exhibits
neither, which is why the tests below assert the mitigations stay inert when the
upstream already reports truncation itself.
"""
from app.core.llm import _is_gateway_error_text, _truncated_by_budget


# ── _truncated_by_budget: synthesize the stop_reason a gateway strips ─────────

def test_full_budget_consumption_is_truncation():
    assert _truncated_by_budget("end_turn", 16000, 16000)      # gateway collapsed max_tokens
    assert _truncated_by_budget("end_turn", 16001, 16000)      # defensive: >= not ==


def test_partial_budget_is_not_truncation():
    assert not _truncated_by_budget("end_turn", 15999, 16000)
    assert not _truncated_by_budget("tool_use", 500, 16000)


def test_already_reported_truncation_is_not_resynthesized():
    # a direct provider API reports max_tokens itself — synthesis must not double-fire
    assert not _truncated_by_budget("max_tokens", 16000, 16000)


def test_missing_usage_never_fires():
    assert not _truncated_by_budget("end_turn", None, 16000)
    assert not _truncated_by_budget("end_turn", 0, 16000)


# ── _is_gateway_error_text: 200-with-error-as-content detection ───────────────

def test_llm_error_prefix_detected():
    assert _is_gateway_error_text("[LLM error: upstream timed out after 60s]")
    assert _is_gateway_error_text("  [LLM error: 429 from api.openai.com]  ")


def test_legacy_placeholder_detected():
    assert _is_gateway_error_text("Error generating response")


def test_real_content_passes():
    assert not _is_gateway_error_text('[{"severity":"blocker","why":"npe"}]')
    assert not _is_gateway_error_text("The change looks correct; [LLM error handling] is fine.")
    assert not _is_gateway_error_text("")
