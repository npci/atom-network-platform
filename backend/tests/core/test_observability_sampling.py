# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 0.4 — unit tests for the Langfuse sample-rate gate.

The gate is implemented in `app.core.observability._should_sample_for_langfuse`.
Local INFO-level log emission is unaffected by sampling — only the
Langfuse forward is gated. Errors (success=False) are NEVER sampled out.
"""
from __future__ import annotations

import pytest

from app.core import observability as obs
from app.core.observability import LlmCallTrace, _should_sample_for_langfuse


def _trace(success: bool = True) -> LlmCallTrace:
    return LlmCallTrace(
        agent_name="t", purpose="utility", provider="claude",
        model="claude-haiku-4-5", streaming=False,
        prompt_chars=1, response_chars=1, response_chunks=0,
        elapsed_ms=1, success=success,
    )


def test_full_rate_forwards_all(monkeypatch):
    monkeypatch.setattr(obs.settings, "langfuse_sample_rate", 1.0)
    assert _should_sample_for_langfuse(_trace()) is True


def test_zero_rate_forwards_none(monkeypatch):
    monkeypatch.setattr(obs.settings, "langfuse_sample_rate", 0.0)
    assert _should_sample_for_langfuse(_trace()) is False


def test_failure_traces_bypass_sampling(monkeypatch):
    monkeypatch.setattr(obs.settings, "langfuse_sample_rate", 0.0)
    assert _should_sample_for_langfuse(_trace(success=False)) is True


def test_partial_rate_distribution(monkeypatch):
    """At rate=0.3, ~30% of 10000 traces should be sampled (allow ±5%)."""
    monkeypatch.setattr(obs.settings, "langfuse_sample_rate", 0.3)
    n = 10_000
    sampled = sum(1 for _ in range(n) if _should_sample_for_langfuse(_trace()))
    assert 0.25 * n < sampled < 0.35 * n


def test_record_llm_call_respects_sampling(monkeypatch):
    """At rate=0.0, _forward_to_langfuse must NOT be invoked for success
    traces, even when use_langfuse=True."""
    monkeypatch.setattr(obs.settings, "use_observability_traces", True)
    monkeypatch.setattr(obs.settings, "use_langfuse", True)
    monkeypatch.setattr(obs.settings, "langfuse_sample_rate", 0.0)

    calls = {"n": 0}
    monkeypatch.setattr(obs, "_forward_to_langfuse",
                        lambda trace: calls.__setitem__("n", calls["n"] + 1))

    obs.record_llm_call(_trace(success=True))
    assert calls["n"] == 0

    # An error trace should still forward.
    obs.record_llm_call(_trace(success=False))
    assert calls["n"] == 1
