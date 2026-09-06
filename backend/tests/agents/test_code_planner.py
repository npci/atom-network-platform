# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 12 — code plan schema + one-shot planner.

Pure — no real LLM. `call_llm` is monkeypatched. All validator tests are
pure-dict-in, pure-dict-out.
"""
from __future__ import annotations

import json

import pytest

from app.agents import code_plan_schema, code_planner


# ──────────────────────────────────────────────────────────────────────────────
# Canonical valid plan (plan §7.3 shape)
# ──────────────────────────────────────────────────────────────────────────────

_VALID_PLAN = {
    "files": [
        {
            "path":              "src/ratelimit/TieredRateLimiter.java",
            "action":            "create",
            "intent":            "New subclass of RateLimiter that consults TenantTier before applying limits",
            "repo":              "common-infra",
            "signatures_to_add": [
                "public class TieredRateLimiter extends RateLimiter",
                "public boolean acquire(TenantContext ctx, int permits)",
            ],
            "callers_impacted":  ["PaymentRetryController.retry"],
        },
        {
            "path":   "src/retry/PaymentRetryController.java",
            "action": "modify",
            "intent": "Inject TieredRateLimiter; return 429 with Retry-After when rejected",
            "repo":   "payment-service",
        },
    ],
    "tests": [
        {
            "path":   "test/ratelimit/TieredRateLimiterTest.java",
            "action": "create",
            "cases":  [
                "enterprise_under_limit_allowed",
                "enterprise_at_limit_returns_429",
                "non_enterprise_unlimited",
            ],
        }
    ],
    "notes": "Behind feature flag TIERED_RATE_LIMIT; roll out 10%→50%→100%.",
}


# ──────────────────────────────────────────────────────────────────────────────
# validate() — happy path
# ──────────────────────────────────────────────────────────────────────────────

def test_fully_valid_plan_passes():
    report = code_plan_schema.validate(_VALID_PLAN)
    assert report["schema_valid"] is True
    assert report["file_count"] == 2
    assert report["test_count"] == 1
    assert report["issues"] == []
    assert report["missing_required"] == []


def test_minimal_valid_plan_passes():
    """Only required keys on files; tests omitted."""
    plan = {
        "files": [
            {"path": "x.java", "action": "create",
             "intent": "Add new X to satisfy spec section 3.2"},
        ],
    }
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is True
    assert report["file_count"] == 1
    assert report["test_count"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# validate() — top-level errors
# ──────────────────────────────────────────────────────────────────────────────

def test_non_dict_input_fails():
    report = code_plan_schema.validate("not a dict")
    assert report["schema_valid"] is False
    assert report["missing_required"] == ["files"]
    assert any("not a dict" in m for m in report["issues"])


def test_missing_files_key_fails():
    report = code_plan_schema.validate({"tests": []})
    assert report["schema_valid"] is False
    assert "files" in report["missing_required"]


def test_empty_files_list_fails():
    report = code_plan_schema.validate({"files": []})
    assert report["schema_valid"] is False
    assert any("at least one entry" in m for m in report["issues"])


def test_files_not_a_list_fails():
    report = code_plan_schema.validate({"files": "just a string"})
    assert report["schema_valid"] is False
    assert any("must be a list" in m for m in report["issues"])


# ──────────────────────────────────────────────────────────────────────────────
# validate() — per-file errors
# ──────────────────────────────────────────────────────────────────────────────

def test_missing_path_in_file_fails():
    plan = {"files": [{"action": "create", "intent": "some reasonable intent here"}]}
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("missing required key `path`" in m for m in report["issues"])


def test_invalid_action_fails():
    plan = {"files": [{"path": "x", "action": "delete", "intent": "remove dead code from x.java"}]}
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("must be one of" in m for m in report["issues"])


def test_short_intent_fails():
    plan = {"files": [{"path": "x.java", "action": "create", "intent": "short"}]}
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("`intent` must be a non-empty string" in m for m in report["issues"])


def test_signatures_must_be_list_when_present():
    plan = {
        "files": [
            {"path": "x.java", "action": "create",
             "intent": "OK intent here at least twelve chars",
             "signatures_to_add": "not a list"},
        ],
    }
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("`signatures_to_add` must be a list" in m for m in report["issues"])


# ──────────────────────────────────────────────────────────────────────────────
# validate() — per-test errors
# ──────────────────────────────────────────────────────────────────────────────

def test_test_missing_cases_fails():
    plan = {
        "files": [{"path": "x.java", "action": "create", "intent": "OK intent here at least twelve chars"}],
        "tests": [{"path": "xt.java", "action": "create"}],
    }
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("`cases` must be a non-empty list" in m for m in report["issues"]) or \
           any("missing required key `cases`" in m for m in report["issues"])


def test_test_empty_cases_fails():
    plan = {
        "files": [{"path": "x.java", "action": "create", "intent": "OK intent here at least twelve chars"}],
        "tests": [{"path": "xt.java", "action": "create", "cases": []}],
    }
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("`cases` must be a non-empty list" in m for m in report["issues"])


# ──────────────────────────────────────────────────────────────────────────────
# generate_plan() — mocked LLM
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generator_returns_parsed_plan(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=3000, **kwargs):
        return json.dumps(_VALID_PLAN)

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    out = await code_planner.generate_plan("tech spec text here")
    assert "files" in out
    assert len(out["files"]) == 2
    report = code_plan_schema.validate(out)
    assert report["schema_valid"] is True


@pytest.mark.asyncio
async def test_generator_drops_unknown_top_level_keys(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=3000, **kwargs):
        payload = dict(_VALID_PLAN)
        payload["hallucinated_field"] = "should be dropped"
        return json.dumps(payload)

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    out = await code_planner.generate_plan("tech spec")
    assert "hallucinated_field" not in out
    assert "files" in out


@pytest.mark.asyncio
async def test_generator_llm_exception_returns_empty(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=3000, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    assert await code_planner.generate_plan("tech spec") == {}


@pytest.mark.asyncio
async def test_generator_non_json_returns_empty(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=3000, **kwargs):
        return "Here's a helpful plan: create rate limiter then add tests."

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    assert await code_planner.generate_plan("tech spec") == {}


@pytest.mark.asyncio
async def test_generator_empty_input_short_circuits(monkeypatch):
    called = {"n": 0}

    async def fake_call_llm(system, messages, max_tokens=3000, **kwargs):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    assert await code_planner.generate_plan("") == {}
    assert await code_planner.generate_plan("   ") == {}
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_generator_brd_included_in_prompt(monkeypatch):
    """When BRD is provided, it should appear in the user payload."""
    captured = {}

    async def fake_call_llm(system, messages, max_tokens=3000, **kwargs):
        captured["user"] = messages[-1]["content"]
        return json.dumps(_VALID_PLAN)

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    await code_planner.generate_plan(
        tech_spec="tech spec body",
        brd="approved BRD context here",
    )
    assert "tech spec body" in captured["user"]
    assert "approved BRD context here" in captured["user"]


@pytest.mark.asyncio
async def test_generator_brd_truncated_when_long(monkeypatch):
    """BRDs > 4000 chars get truncated with an ellipsis marker."""
    captured = {}

    async def fake_call_llm(system, messages, max_tokens=3000, **kwargs):
        captured["user"] = messages[-1]["content"]
        return json.dumps(_VALID_PLAN)

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    long_brd = "x" * 5000
    await code_planner.generate_plan("tech spec", brd=long_brd)
    assert "..." in captured["user"]
    # The full 5000-char BRD shouldn't appear verbatim
    assert "x" * 5000 not in captured["user"]
