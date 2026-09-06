# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the per-agent LLM router (Slice 27).

All pure: no LLM calls, no network. The router's surface is small
enough to exhaust its branches in unit tests.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.llm_router import (
    Purpose,
    agent_purposes,
    pick_model_for,
    pick_model_for_agent,
    purpose_for_agent,
)


# ──────────────────────────────────────────────────────────────────────────────
# Purpose enum + agent map
# ──────────────────────────────────────────────────────────────────────────────

class TestPurposeEnum:

    def test_three_distinct_purposes(self):
        names = {p.name for p in Purpose}
        assert names == {"REASONING", "ROUTING", "UTILITY"}

    def test_string_enum_values_are_lowercase(self):
        for p in Purpose:
            assert p.value == p.name.lower()


class TestAgentPurposes:

    def test_returns_a_copy_not_the_internal_dict(self):
        d1 = agent_purposes()
        d1["polluted"] = Purpose.REASONING
        d2 = agent_purposes()
        assert "polluted" not in d2

    def test_reasoning_agents_mapped(self):
        d = agent_purposes()
        for a in ("brd", "tech_spec", "code_change", "deep_researcher"):
            assert d[a] == Purpose.REASONING

    def test_routing_agents_mapped(self):
        d = agent_purposes()
        for a in ("taxonomy", "query_understanding", "doc_code_linker"):
            assert d[a] == Purpose.ROUTING

    def test_utility_agents_mapped(self):
        d = agent_purposes()
        for a in ("ambiguity_detector", "context_compressor", "code_summarizer"):
            assert d[a] == Purpose.UTILITY


class TestPurposeForAgent:

    def test_known_agent_resolves(self):
        assert purpose_for_agent("brd") == Purpose.REASONING
        assert purpose_for_agent("taxonomy") == Purpose.ROUTING

    def test_case_insensitive(self):
        assert purpose_for_agent("BRD") == Purpose.REASONING
        assert purpose_for_agent("Taxonomy") == Purpose.ROUTING

    def test_unknown_agent_defaults_to_reasoning(self):
        """Safer to pay frontier-model cost on an unmapped agent than
        silently downgrade it to a cheap model."""
        assert purpose_for_agent("brand_new_agent") == Purpose.REASONING

    def test_empty_or_none_defaults_to_reasoning(self):
        assert purpose_for_agent("") == Purpose.REASONING
        assert purpose_for_agent(None) == Purpose.REASONING


# ──────────────────────────────────────────────────────────────────────────────
# pick_model_for
# ──────────────────────────────────────────────────────────────────────────────

class TestPickModelFor:

    def test_routing_off_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "use_llm_routing", False)
        assert pick_model_for(Purpose.ROUTING) is None
        assert pick_model_for(Purpose.UTILITY) is None
        assert pick_model_for(Purpose.REASONING) is None

    def test_routing_on_blank_value_returns_none(self, monkeypatch):
        """Empty string for a purpose's model is "use the default" — falls
        through to None so callers fall back to `llm.get_model()`."""
        monkeypatch.setattr(settings, "use_llm_routing", True)
        monkeypatch.setattr(settings, "routing_model_routing", "")
        assert pick_model_for(Purpose.ROUTING) is None

    def test_routing_on_with_value_returns_model(self, monkeypatch):
        monkeypatch.setattr(settings, "use_llm_routing", True)
        monkeypatch.setattr(settings, "routing_model_routing", "claude-haiku-4-5-20251001")
        assert pick_model_for(Purpose.ROUTING) == "claude-haiku-4-5-20251001"

    def test_per_purpose_independence(self, monkeypatch):
        """Routing is configured per-purpose; one being set doesn't bleed
        into another."""
        monkeypatch.setattr(settings, "use_llm_routing", True)
        monkeypatch.setattr(settings, "routing_model_routing", "claude-haiku-4-5-20251001")
        monkeypatch.setattr(settings, "routing_model_utility", "gpt-4o-mini")
        monkeypatch.setattr(settings, "routing_model_reasoning", "")

        assert pick_model_for(Purpose.ROUTING) == "claude-haiku-4-5-20251001"
        assert pick_model_for(Purpose.UTILITY) == "gpt-4o-mini"
        assert pick_model_for(Purpose.REASONING) is None    # blank — fall back to default

    def test_string_purpose_value_works(self, monkeypatch):
        """Callers serialising purpose across e.g. Celery args may pass
        a string. Both forms work."""
        monkeypatch.setattr(settings, "use_llm_routing", True)
        monkeypatch.setattr(settings, "routing_model_routing", "fast-model")
        assert pick_model_for("routing") == "fast-model"
        assert pick_model_for("ROUTING") == "fast-model"

    def test_unknown_purpose_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "use_llm_routing", True)
        monkeypatch.setattr(settings, "routing_model_routing", "x")
        assert pick_model_for("not-a-purpose") is None
        assert pick_model_for("") is None
        assert pick_model_for(None) is None


# ──────────────────────────────────────────────────────────────────────────────
# pick_model_for_agent
# ──────────────────────────────────────────────────────────────────────────────

class TestPickModelForAgent:

    def test_routing_off_always_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "use_llm_routing", False)
        # Even configured per-purpose models are ignored when routing is off.
        monkeypatch.setattr(settings, "routing_model_routing", "haiku")
        assert pick_model_for_agent("taxonomy") is None
        assert pick_model_for_agent("brd") is None

    def test_routing_on_routes_taxonomy_to_routing_model(self, monkeypatch):
        monkeypatch.setattr(settings, "use_llm_routing", True)
        monkeypatch.setattr(settings, "routing_model_routing", "haiku-4-5")
        monkeypatch.setattr(settings, "routing_model_reasoning", "opus-4-7")
        assert pick_model_for_agent("taxonomy") == "haiku-4-5"

    def test_routing_on_routes_brd_to_reasoning_model(self, monkeypatch):
        monkeypatch.setattr(settings, "use_llm_routing", True)
        monkeypatch.setattr(settings, "routing_model_routing", "haiku")
        monkeypatch.setattr(settings, "routing_model_reasoning", "opus")
        assert pick_model_for_agent("brd") == "opus"

    def test_unknown_agent_uses_reasoning_purpose(self, monkeypatch):
        monkeypatch.setattr(settings, "use_llm_routing", True)
        monkeypatch.setattr(settings, "routing_model_reasoning", "opus")
        assert pick_model_for_agent("brand_new_agent") == "opus"

    def test_utility_agent_uses_utility_model(self, monkeypatch):
        monkeypatch.setattr(settings, "use_llm_routing", True)
        monkeypatch.setattr(settings, "routing_model_utility", "gpt-4o-mini")
        assert pick_model_for_agent("code_summarizer") == "gpt-4o-mini"
        assert pick_model_for_agent("ambiguity_detector") == "gpt-4o-mini"


# ──────────────────────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────────────────────

class TestDefaults:

    def test_routing_off_by_default(self):
        assert settings.use_llm_routing is False

    def test_per_purpose_declared_defaults(self, monkeypatch):
        """The DECLARED defaults, not whatever the local .env happens to set.

        This used to read the global `settings` singleton and assert all three
        were "". That passes only where a .env blanks them — as backend/.env
        does — and fails anywhere else, which is exactly what happened the first
        time CI ran the suite: routing resolved to the real declared default and
        the assertion blew up. A test that encodes the contents of an untracked
        env file is testing the machine, not the code.

        Constructing Settings with the routing vars cleared, and with the .env
        file ignored, pins the values actually written in config.py.
        """
        from app.core.config import Settings

        for var in ("ROUTING_MODEL_REASONING", "ROUTING_MODEL_ROUTING",
                    "ROUTING_MODEL_UTILITY"):
            monkeypatch.delenv(var, raising=False)

        s = Settings(_env_file=None)
        # Reasoning is intentionally blank: empty means "fall through to the
        # global default", i.e. the frontier model.
        assert s.routing_model_reasoning == ""
        # The cheap buckets name a small model explicitly.
        assert s.routing_model_routing == "claude-haiku-4-5-20251001"
        assert s.routing_model_utility == "claude-haiku-4-5-20251001"
