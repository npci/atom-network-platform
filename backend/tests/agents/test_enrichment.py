# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 10 — enrichment schema, validator, and one-shot generator.

Pure — no real LLM. `call_llm` is monkeypatched for generator tests.
Validator tests are pure-dict-in, pure-dict-out.
"""
from __future__ import annotations

import json

import pytest

from app.agents import enrichment, enrichment_schema


# ──────────────────────────────────────────────────────────────────────────────
# Schema / validator — pure
# ──────────────────────────────────────────────────────────────────────────────

_FULL_STORY = {
    "title":                "Rate-limit the network retry endpoint for enterprise tier",
    "as_a":                 "Enterprise customer's billing system",
    "i_want":               "Predictable retry behavior under burst load",
    "so_that":              "One tenant cannot affect other tenants",
    "context_summary":      "Today the /retry endpoint accepts unbounded requests. Enterprise tenants are tagged via X-Tenant-Tier. ADR-042 standardised a Redis sliding-window limiter.",
    "acceptance_criteria":  ["Enterprise tier capped at 100 req/min/tenant", "429 Retry-After returned on cap breach", "Metrics emitted for allowed/rejected"],
    "non_functional":       ["Limiter check adds ≤5ms p99", "Backward compatible for non-enterprise traffic"],
    "open_questions":       ["Per API key or per tenant?"],
    "affected_components":  [{"repo": "common-infra", "files": ["src/ratelimit/RateLimiter.java"]}],
    "citations":            ["confluence://Payments/RetryDesign"],
}


def test_fully_populated_story_validates():
    report = enrichment_schema.validate(_FULL_STORY)
    assert report["schema_valid"] is True
    assert report["missing_required"] == []
    assert report["missing_recommended"] == []
    assert report["field_completeness"] == 1.0
    assert report["keyword_coverage"] == 1.0   # no keywords requested


def test_missing_required_fails_schema_valid():
    story = dict(_FULL_STORY)
    del story["so_that"]
    report = enrichment_schema.validate(story)
    assert report["schema_valid"] is False
    assert "so_that" in report["missing_required"]
    assert report["field_completeness"] < 1.0


def test_short_context_summary_fails_required():
    story = dict(_FULL_STORY)
    story["context_summary"] = "too short"      # below MIN_CONTEXT_SUMMARY_CHARS
    report = enrichment_schema.validate(story)
    assert "context_summary" in report["missing_required"]


def test_empty_acceptance_criteria_fails():
    story = dict(_FULL_STORY)
    story["acceptance_criteria"] = []
    report = enrichment_schema.validate(story)
    assert "acceptance_criteria" in report["missing_required"]


def test_missing_recommended_still_schema_valid():
    """Schema validity depends only on REQUIRED fields — missing recommended
    reduces completeness but doesn't make the schema invalid."""
    story = {k: v for k, v in _FULL_STORY.items() if k not in ("non_functional", "open_questions")}
    report = enrichment_schema.validate(story)
    assert report["schema_valid"] is True
    assert "non_functional" in report["missing_recommended"]
    assert "open_questions" in report["missing_recommended"]


def test_keyword_coverage_partial():
    report = enrichment_schema.validate(
        _FULL_STORY,
        required_keywords=["enterprise", "Redis", "IPv6"],  # IPv6 not in story
    )
    assert report["keyword_coverage"] == pytest.approx(2 / 3, abs=0.01)
    assert report["missing_keywords"] == ["IPv6"]


def test_keyword_coverage_all_matched():
    report = enrichment_schema.validate(
        _FULL_STORY,
        required_keywords=["rate", "tenant", "retry"],
    )
    assert report["keyword_coverage"] == 1.0
    assert report["missing_keywords"] == []


def test_non_dict_input_returns_zeros():
    report = enrichment_schema.validate("not a dict")
    assert report["schema_valid"] is False
    assert report["field_completeness"] == 0.0
    assert set(report["missing_required"]) == set(enrichment_schema.REQUIRED_FIELDS)


def test_keyword_coverage_is_1_when_no_keywords_requested():
    report = enrichment_schema.validate({"title": "x"})
    assert report["keyword_coverage"] == 1.0


# ──────────────────────────────────────────────────────────────────────────────
# generate_enriched_story — mocked LLM
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generator_returns_parsed_dict(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2500, **kwargs):
        return json.dumps(_FULL_STORY)

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    out = await enrichment.generate_enriched_story("some PO prompt")
    assert out["title"] == _FULL_STORY["title"]
    assert out["as_a"] == _FULL_STORY["as_a"]
    assert len(out["acceptance_criteria"]) == 3


@pytest.mark.asyncio
async def test_generator_drops_unexpected_fields(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2500, **kwargs):
        # LLM emits valid schema + extra junk field
        payload = dict(_FULL_STORY)
        payload["hallucinated_field"] = "should be dropped"
        return json.dumps(payload)

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    out = await enrichment.generate_enriched_story("x")
    assert "hallucinated_field" not in out
    assert "title" in out


@pytest.mark.asyncio
async def test_generator_llm_exception_returns_empty(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2500, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    assert await enrichment.generate_enriched_story("x") == {}


@pytest.mark.asyncio
async def test_generator_non_json_returns_empty(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2500, **kwargs):
        return "I'm an LLM and I forgot to return JSON"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    assert await enrichment.generate_enriched_story("x") == {}


@pytest.mark.asyncio
async def test_generator_empty_input_short_circuits(monkeypatch):
    called = {"n": 0}

    async def fake_call_llm(system, messages, max_tokens=2500, **kwargs):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    assert await enrichment.generate_enriched_story("") == {}
    assert await enrichment.generate_enriched_story("   ") == {}
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_generator_non_dict_response_returns_empty(monkeypatch):
    async def fake_call_llm(system, messages, max_tokens=2500, **kwargs):
        return json.dumps(["list", "not", "dict"])

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    assert await enrichment.generate_enriched_story("x") == {}
