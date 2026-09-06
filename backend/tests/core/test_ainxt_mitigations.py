# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Client-side mitigations for AiNxt /v1/messages fidelity gaps
(docs/ainxt_messages_compat.md §5): truncation synthesis + gateway-error-as-text detection."""
from app.core.llm import _is_gateway_error_text, _truncated_by_budget


# ── _truncated_by_budget: synthesize the stop_reason AiNxt strips ──────────────

def test_full_budget_consumption_is_truncation():
    assert _truncated_by_budget("end_turn", 16000, 16000)      # gateway collapsed max_tokens
    assert _truncated_by_budget("end_turn", 16001, 16000)      # defensive: >= not ==


def test_partial_budget_is_not_truncation():
    assert not _truncated_by_budget("end_turn", 15999, 16000)
    assert not _truncated_by_budget("tool_use", 500, 16000)


def test_already_reported_truncation_is_not_resynthesized():
    # direct Anthropic reports max_tokens itself — synthesis must not double-fire
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
