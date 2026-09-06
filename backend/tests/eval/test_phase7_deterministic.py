# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 7 — deterministic check unit tests.

Covers clean / dirty / empty input cases for each of the six new check
functions plus the run_checks integration path that the runner uses.
"""
import pytest

from app.services.evaluation.deterministic import (
    PROMPT_MIN_LENGTH,
    CHECKS,
    check_at_least_one_source,
    check_canvas_has_required_sections,
    check_no_unanswered_canvas_questions,
    check_prompt_min_length,
    check_prompt_not_empty,
    check_research_summary_not_empty,
    run_checks,
)


def _combined(artifacts: dict[str, dict]) -> dict:
    """Mirror what run_checks does internally so we can test functions directly."""
    combined: dict = {}
    for v in artifacts.values():
        if isinstance(v, dict):
            combined.update(v)
    combined["_all"] = artifacts
    return combined


# ── check_prompt_not_empty / check_prompt_min_length ────────────────────────

class TestPromptChecks:
    def test_empty_prompt_flagged_as_empty(self):
        c = _combined({"enhanced_prompt": {"content": "   "}})
        assert len(check_prompt_not_empty(c)) == 1
        # min-length stays silent when empty so the message isn't duplicated
        assert check_prompt_min_length(c) == []

    def test_short_prompt_flagged_by_min_length(self):
        c = _combined({"enhanced_prompt": {"content": "too short"}})
        assert check_prompt_not_empty(c) == []
        findings = check_prompt_min_length(c)
        assert len(findings) == 1
        assert "minimum" in findings[0]

    def test_sufficient_prompt_clean(self):
        c = _combined({"enhanced_prompt": {"content": "x" * (PROMPT_MIN_LENGTH + 5)}})
        assert check_prompt_not_empty(c) == []
        assert check_prompt_min_length(c) == []

    def test_missing_artifact_treated_as_empty(self):
        c = _combined({})
        assert len(check_prompt_not_empty(c)) == 1


# ── check_research_summary_not_empty / check_at_least_one_source ───────────

class TestResearchChecks:
    def test_empty_summary_flagged(self):
        c = _combined({"research_summary": {"content": ""}})
        assert len(check_research_summary_not_empty(c)) == 1

    def test_summary_with_url_passes_source_check(self):
        c = _combined({"research_summary": {"content": "See https://npci.org.in for details"}})
        assert check_at_least_one_source(c) == []

    def test_summary_with_bracket_citation_passes(self):
        c = _combined({"research_summary": {"content": "Discussed extensively [1]."}})
        assert check_at_least_one_source(c) == []

    def test_summary_with_sources_list_passes(self):
        c = _combined({
            "research_summary": {"content": "body", "sources": [{"title": "the Authority Circular"}]},
        })
        assert check_at_least_one_source(c) == []

    def test_separate_sources_artifact_passes(self):
        c = _combined({
            "research_summary":  {"content": "body"},
            "research_sources":  {"sources": [{"url": "x"}]},
        })
        assert check_at_least_one_source(c) == []

    def test_summary_without_any_source_flagged(self):
        c = _combined({"research_summary": {"content": "no citations here"}})
        findings = check_at_least_one_source(c)
        assert len(findings) == 1
        assert "no sources" in findings[0].lower()


# ── check_canvas_has_required_sections ──────────────────────────────────────

class TestCanvasSections:
    def test_empty_canvas_flagged(self):
        c = _combined({"product_canvas": {"content": ""}})
        findings = check_canvas_has_required_sections(c)
        assert findings and "empty" in findings[0].lower()

    def test_canvas_text_with_all_tokens_clean(self):
        text = "## Problem\n...\n## Scope\n...\n## Stakeholders\n..."
        c = _combined({"product_canvas": {"content": text}})
        assert check_canvas_has_required_sections(c) == []

    def test_canvas_missing_scope_flagged(self):
        text = "## Problem\n...\n## Stakeholders\n..."
        c = _combined({"product_canvas": {"content": text}})
        findings = check_canvas_has_required_sections(c)
        assert any("scope" in f.lower() for f in findings)
        assert all("problem" not in f.lower() for f in findings)
        assert all("stakeholder" not in f.lower() for f in findings)

    def test_canvas_sections_dict_shape_accepted(self):
        c = _combined({
            "product_canvas": {
                "sections": {"problem_statement": "x", "scope_in": "y", "stakeholders": "z"},
            },
        })
        assert check_canvas_has_required_sections(c) == []

    def test_canvas_sections_dict_missing_token_flagged(self):
        c = _combined({
            "product_canvas": {"sections": {"problem_statement": "x", "stakeholders": "z"}},
        })
        findings = check_canvas_has_required_sections(c)
        assert any("scope" in f.lower() for f in findings)


# ── check_no_unanswered_canvas_questions ───────────────────────────────────

class TestClarificationQuestions:
    def test_all_terminal_clean(self):
        c = _combined({
            "clarification_thread": {
                "questions": [
                    {"id": "q1", "status": "answered"},
                    {"id": "q2", "status": "skipped"},
                    {"id": "q3", "status": "deferred"},
                ],
            },
        })
        assert check_no_unanswered_canvas_questions(c) == []

    def test_pending_question_flagged(self):
        c = _combined({
            "clarification_thread": {
                "questions": [
                    {"id": "q1", "status": "answered"},
                    {"id": "q2", "status": "pending"},
                ],
            },
        })
        findings = check_no_unanswered_canvas_questions(c)
        assert findings and "1 of 2" in findings[0]

    def test_alternate_items_key_accepted(self):
        c = _combined({
            "clarification_thread": {"items": [{"status": "answered"}]},
        })
        assert check_no_unanswered_canvas_questions(c) == []

    def test_no_questions_list_is_silent(self):
        c = _combined({"clarification_thread": {}})
        assert check_no_unanswered_canvas_questions(c) == []


# ── run_checks registry integration ────────────────────────────────────────

class TestPhase7RunChecksIntegration:
    @pytest.mark.parametrize("name", [
        "check_prompt_not_empty",
        "check_prompt_min_length",
        "check_research_summary_not_empty",
        "check_at_least_one_source",
        "check_canvas_has_required_sections",
        "check_no_unanswered_canvas_questions",
    ])
    def test_new_check_in_registry(self, name):
        assert name in CHECKS, f"{name} missing from CHECKS dict"

    def test_run_checks_dispatches_phase7_checks(self):
        findings = run_checks(
            ["check_prompt_not_empty", "check_prompt_min_length"],
            {"enhanced_prompt": {"content": ""}},
        )
        assert any("empty" in f.lower() for f in findings)
