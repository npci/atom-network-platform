# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the observability shim (Slice 28).

Pure: no LLM calls, no network. The shim's responsibilities are small —
build a trace from kwargs, resolve purpose from agent name, emit a JSON
log line when the flag is on, fail-open on any internal error.
"""
from __future__ import annotations

import json
import logging

import pytest

from app.core.config import settings
from app.core.observability import (
    LlmCallTrace,
    agent_purpose_label,
    estimate_prompt_chars,
    record_llm_call,
    record_llm_call_kwargs,
)


# ──────────────────────────────────────────────────────────────────────────────
# LlmCallTrace.to_dict
# ──────────────────────────────────────────────────────────────────────────────

class TestTraceShape:

    def test_full_trace_round_trips_through_dict(self):
        t = LlmCallTrace(
            agent_name="taxonomy", purpose="routing",
            provider="claude", model="claude-haiku-4-5",
            streaming=False,
            prompt_chars=100, response_chars=50, response_chunks=0,
            elapsed_ms=420, success=True,
        )
        d = t.to_dict()
        assert d["agent_name"] == "taxonomy"
        assert d["purpose"] == "routing"
        assert d["model"] == "claude-haiku-4-5"
        assert d["elapsed_ms"] == 420
        assert d["success"] is True
        # Empty error fields dropped
        assert "error_class" not in d
        assert "error_message" not in d
        # Empty extra dict dropped
        assert "extra" not in d

    def test_failure_trace_keeps_error_fields(self):
        t = LlmCallTrace(
            agent_name="brd", purpose="reasoning",
            provider="claude", model="claude-opus-4-7",
            streaming=True,
            prompt_chars=2000, response_chars=300, response_chunks=15,
            elapsed_ms=8200, success=False,
            error_class="RateLimitError", error_message="429 too many requests",
        )
        d = t.to_dict()
        assert d["error_class"] == "RateLimitError"
        assert d["error_message"].startswith("429")

    def test_extra_dict_preserved_when_non_empty(self):
        t = LlmCallTrace(
            agent_name="x", purpose="utility",
            provider="claude", model="m",
            streaming=False,
            prompt_chars=1, response_chars=1, response_chunks=0,
            elapsed_ms=1, success=True,
            extra={"trace_id": "abc-123"},
        )
        assert t.to_dict()["extra"] == {"trace_id": "abc-123"}


# ──────────────────────────────────────────────────────────────────────────────
# agent_purpose_label
# ──────────────────────────────────────────────────────────────────────────────

class TestAgentPurposeLabel:

    def test_known_routing_agent(self):
        assert agent_purpose_label("taxonomy") == "routing"

    def test_known_reasoning_agent(self):
        assert agent_purpose_label("brd") == "reasoning"

    def test_known_utility_agent(self):
        assert agent_purpose_label("code_summarizer") == "utility"

    def test_unknown_agent_defaults_to_reasoning(self):
        # llm_router falls back to REASONING for unknown agents — observability
        # should reflect that same choice.
        assert agent_purpose_label("brand_new_agent") == "reasoning"

    def test_empty_returns_unknown(self):
        assert agent_purpose_label("") == "unknown"
        assert agent_purpose_label(None) == "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# estimate_prompt_chars
# ──────────────────────────────────────────────────────────────────────────────

class TestEstimatePromptChars:

    def test_sums_system_plus_message_bodies(self):
        n = estimate_prompt_chars(
            "system prompt", [{"role": "user", "content": "hello"}],
        )
        assert n == len("system prompt") + len("hello")

    def test_handles_missing_or_non_string_content(self):
        n = estimate_prompt_chars(
            "sys", [
                {"role": "user", "content": "ok"},
                {"role": "user"},                    # no content key
                {"role": "user", "content": None},   # None content
                {"role": "user", "content": 42},     # non-string
            ],
        )
        assert n == len("sys") + len("ok")

    def test_empty_inputs_zero(self):
        assert estimate_prompt_chars("", []) == 0
        assert estimate_prompt_chars(None, None) == 0   # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────────────
# record_llm_call — flag gating + log emission
# ──────────────────────────────────────────────────────────────────────────────

class TestRecordLlmCall:

    def test_flag_off_does_not_emit(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "use_observability_traces", False)
        with caplog.at_level(logging.INFO, logger="app.observability"):
            record_llm_call(LlmCallTrace(
                agent_name="x", purpose="routing",
                provider="claude", model="m",
                streaming=False, prompt_chars=1, response_chars=1,
                response_chunks=0, elapsed_ms=1, success=True,
            ))
        assert not [r for r in caplog.records if r.name == "app.observability"]

    def test_flag_on_emits_structured_json(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "use_observability_traces", True)
        with caplog.at_level(logging.INFO, logger="app.observability"):
            record_llm_call(LlmCallTrace(
                agent_name="taxonomy", purpose="routing",
                provider="claude", model="claude-haiku",
                streaming=False, prompt_chars=120, response_chars=80,
                response_chunks=0, elapsed_ms=350, success=True,
            ))
        emitted = [r for r in caplog.records if r.name == "app.observability"]
        assert emitted, "expected at least one observability log record"
        msg = emitted[0].getMessage()
        assert msg.startswith("llm_call ")
        # Parse the JSON payload after the "llm_call " prefix
        payload = json.loads(msg[len("llm_call "):])
        assert payload["agent_name"] == "taxonomy"
        assert payload["purpose"] == "routing"
        assert payload["model"] == "claude-haiku"
        assert payload["elapsed_ms"] == 350
        assert payload["success"] is True


# ──────────────────────────────────────────────────────────────────────────────
# record_llm_call_kwargs — convenience entry
# ──────────────────────────────────────────────────────────────────────────────

class TestRecordLlmCallKwargs:

    def test_resolves_purpose_from_agent_name(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "use_observability_traces", True)
        with caplog.at_level(logging.INFO, logger="app.observability"):
            record_llm_call_kwargs(
                agent_name="taxonomy",
                provider="claude", model="claude-haiku-4-5",
                streaming=False, prompt_chars=10, response_chars=5,
                response_chunks=0, elapsed_ms=42, success=True,
            )
        emitted = [r for r in caplog.records if r.name == "app.observability"]
        msg = emitted[0].getMessage()
        payload = json.loads(msg[len("llm_call "):])
        assert payload["purpose"] == "routing"

    def test_error_path_records_class_and_message(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "use_observability_traces", True)
        with caplog.at_level(logging.INFO, logger="app.observability"):
            record_llm_call_kwargs(
                agent_name="brd",
                provider="claude", model="opus",
                streaming=True, prompt_chars=200, response_chars=0,
                response_chunks=0, elapsed_ms=8000, success=False,
                error=RuntimeError("backend timeout"),
            )
        emitted = [r for r in caplog.records if r.name == "app.observability"]
        payload = json.loads(emitted[0].getMessage()[len("llm_call "):])
        assert payload["success"] is False
        assert payload["error_class"] == "RuntimeError"
        assert "backend timeout" in payload["error_message"]

    def test_long_error_message_truncated_to_500(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "use_observability_traces", True)
        long_msg = "x" * 5000
        with caplog.at_level(logging.INFO, logger="app.observability"):
            record_llm_call_kwargs(
                agent_name="x", provider="claude", model="m",
                streaming=False, prompt_chars=0, response_chars=0,
                response_chunks=0, elapsed_ms=0, success=False,
                error=RuntimeError(long_msg),
            )
        emitted = [r for r in caplog.records if r.name == "app.observability"]
        payload = json.loads(emitted[0].getMessage()[len("llm_call "):])
        assert len(payload["error_message"]) <= 500

    def test_unknown_agent_records_purpose_as_reasoning(self, monkeypatch, caplog):
        """Mirrors llm_router's REASONING fallback so traces stay consistent."""
        monkeypatch.setattr(settings, "use_observability_traces", True)
        with caplog.at_level(logging.INFO, logger="app.observability"):
            record_llm_call_kwargs(
                agent_name="brand_new_agent",
                provider="claude", model="m",
                streaming=False, prompt_chars=1, response_chars=1,
                response_chunks=0, elapsed_ms=1, success=True,
            )
        emitted = [r for r in caplog.records if r.name == "app.observability"]
        payload = json.loads(emitted[0].getMessage()[len("llm_call "):])
        assert payload["purpose"] == "reasoning"

    def test_flag_off_emits_nothing_via_kwargs_wrapper_too(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "use_observability_traces", False)
        with caplog.at_level(logging.INFO, logger="app.observability"):
            record_llm_call_kwargs(
                agent_name="taxonomy",
                provider="claude", model="m",
                streaming=False, prompt_chars=1, response_chars=1,
                response_chunks=0, elapsed_ms=1, success=True,
            )
        emitted = [r for r in caplog.records if r.name == "app.observability"]
        assert emitted == []


# ──────────────────────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────────────────────

class TestDefaults:

    def test_observability_on_by_default(self):
        # Flipped ON: one JSONL row per LLM call is the cheapest diagnostic
        # signal and lands in the (mounted, rotating) llm_calls.jsonl. Langfuse
        # forwarding stays separately gated, so this default makes no network call.
        assert settings.use_observability_traces is True

    def test_langfuse_off_by_default(self):
        assert settings.use_langfuse is False
        assert settings.langfuse_public_key == ""
        assert settings.langfuse_secret_key == ""


# ──────────────────────────────────────────────────────────────────────────────
# Sub-slice 28a — Langfuse forwarding (lazy / fail-open)
# ──────────────────────────────────────────────────────────────────────────────

class TestLangfuseForwarding:

    def test_langfuse_off_means_no_forward(self, monkeypatch):
        from app.core import observability as obs
        obs._reset_langfuse_client_for_tests()
        monkeypatch.setattr(settings, "use_observability_traces", True)
        monkeypatch.setattr(settings, "use_langfuse", False)

        called = {"n": 0}
        def fake_forward(trace): called["n"] += 1
        monkeypatch.setattr(obs, "_forward_to_langfuse", fake_forward)

        obs.record_llm_call(LlmCallTrace(
            agent_name="taxonomy", purpose="routing",
            provider="claude", model="m",
            streaming=False, prompt_chars=1, response_chars=1,
            response_chunks=0, elapsed_ms=1, success=True,
        ))
        assert called["n"] == 0

    def test_langfuse_on_invokes_forward(self, monkeypatch):
        from app.core import observability as obs
        obs._reset_langfuse_client_for_tests()
        monkeypatch.setattr(settings, "use_observability_traces", True)
        monkeypatch.setattr(settings, "use_langfuse", True)

        called = {"n": 0, "trace": None}
        def fake_forward(trace):
            called["n"] += 1
            called["trace"] = trace
        monkeypatch.setattr(obs, "_forward_to_langfuse", fake_forward)

        obs.record_llm_call(LlmCallTrace(
            agent_name="taxonomy", purpose="routing",
            provider="claude", model="m",
            streaming=False, prompt_chars=1, response_chars=1,
            response_chunks=0, elapsed_ms=1, success=True,
        ))
        assert called["n"] == 1
        assert called["trace"].agent_name == "taxonomy"

    def test_forward_exception_swallowed(self, monkeypatch, caplog):
        from app.core import observability as obs
        obs._reset_langfuse_client_for_tests()
        monkeypatch.setattr(settings, "use_observability_traces", True)
        monkeypatch.setattr(settings, "use_langfuse", True)

        def boom(trace):
            raise RuntimeError("langfuse net down")
        monkeypatch.setattr(obs, "_forward_to_langfuse", boom)

        # Must not raise.
        obs.record_llm_call(LlmCallTrace(
            agent_name="x", purpose="utility",
            provider="claude", model="m",
            streaming=False, prompt_chars=1, response_chars=1,
            response_chunks=0, elapsed_ms=1, success=True,
        ))

    def test_get_client_no_creds_returns_none(self, monkeypatch):
        from app.core import observability as obs
        obs._reset_langfuse_client_for_tests()
        monkeypatch.setattr(settings, "langfuse_host", "https://x")
        monkeypatch.setattr(settings, "langfuse_public_key", "")
        monkeypatch.setattr(settings, "langfuse_secret_key", "")
        assert obs._get_langfuse_client() is None

    def test_get_client_caches_failure(self, monkeypatch):
        """Once init fails, subsequent calls don't retry."""
        from app.core import observability as obs
        obs._reset_langfuse_client_for_tests()
        monkeypatch.setattr(settings, "langfuse_host", "")  # empty → fail
        monkeypatch.setattr(settings, "langfuse_public_key", "")
        monkeypatch.setattr(settings, "langfuse_secret_key", "")
        assert obs._get_langfuse_client() is None
        # Second call still returns None without trying to import
        assert obs._get_langfuse_client() is None
