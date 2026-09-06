# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SDLC-A22 — TSD-derived test AUTHORING, and the test-of-the-tests it needs.

The review's complaint about the old state was that "the feature test gate is
satisfied by the existence of any test file, regardless of what it tests." A
coverage gate answers that — but only if the coverage number means something.
Before this pass it did not:

  * `_tests_clause()` told the agent to emit `// tsd-ref: <section>` markers.
  * The agent was never shown WHICH assertions the extractor had found.
  * A marker only counted when it matched an assertion's `id`/`tsd_section`
    EXACTLY.

So the agent was graded against a list it could not see, using exact string
matching. Measured coverage reflected luck. Enabling enforcement on that number
would have blocked correct work — the fastest way to get a gate switched off for
good.

These tests therefore focus on the two properties that make the number
trustworthy, which is what "test-of-the-tests" means here:

  1. The prompt block actually hands over the exact marker to copy.
  2. Coverage matching tolerates cosmetic differences but not semantic ones.
"""
from __future__ import annotations

import pytest

from app.agents.tsd_test_generator import (
    _normalise_ref,
    assertion_coverage,
    assertions_block,
    referenced_tsd_refs,
)


def _assertion(idx: int, section: str, *, kind: str = "api_contract", **kw) -> dict:
    base = {
        "id": f"{kind}:{idx}",
        "kind": kind,
        "tsd_section": section,
        "title": kw.get("title", f"assertion {idx}"),
        "description": kw.get("description", ""),
        "endpoint": kw.get("endpoint", ""),
        "expected_status": kw.get("expected_status"),
        "expected_field_or_code": kw.get("expected_field_or_code", ""),
        "pass_criteria": kw.get("pass_criteria", ""),
    }
    return base


class TestAuthoringBlockHandsOverWhatTheGateGrades:
    """The missing link this pass adds. Without it the agent guesses."""

    def test_block_contains_the_exact_marker_for_each_assertion(self):
        assertions = [
            _assertion(1, "4.2 Error Codes"),
            _assertion(2, "5.1 State Transitions", kind="state_transition"),
        ]
        block = assertions_block(assertions)
        assert "// tsd-ref: 4.2 Error Codes" in block
        assert "// tsd-ref: 5.1 State Transitions" in block

    def test_marker_from_the_block_is_recognised_as_coverage(self):
        """The round-trip that matters: whatever the block tells the agent to
        write must be what the checker counts. If these two ever drift, coverage
        silently reads zero — the exact failure this pass fixes."""
        assertions = [_assertion(1, "4.2 Error Codes")]
        block = assertions_block(assertions)

        marker_line = next(ln.strip() for ln in block.splitlines()
                           if "tsd-ref:" in ln)
        # Simulate the agent copying the marker verbatim above a test method.
        test_src = f"{marker_line}\n@Test void rejectsBadCode() {{}}"

        cov = assertion_coverage(assertions, [test_src])
        assert cov["covered"] == 1
        assert cov["coverage_ratio"] == 1.0

    def test_block_includes_pass_criteria_and_context(self):
        assertions = [_assertion(
            1, "4.2 Error Codes", title="Reject unknown MCC",
            pass_criteria="HTTP 400 with code E_MCC_UNKNOWN",
            endpoint="POST /v2/collect", expected_status=400,
            expected_field_or_code="E_MCC_UNKNOWN")]
        block = assertions_block(assertions)
        assert "Reject unknown MCC" in block
        assert "HTTP 400 with code E_MCC_UNKNOWN" in block
        assert "POST /v2/collect" in block

    def test_empty_assertions_render_nothing(self):
        """So the caller can concatenate unconditionally and a run with no
        approved TSD gets a byte-identical prompt."""
        assert assertions_block([]) == ""
        assert assertions_block(None) == ""

    def test_assertions_with_no_marker_are_skipped_not_rendered_blank(self):
        broken = [{"id": "", "tsd_section": "", "title": "no marker possible"}]
        assert assertions_block(broken) == ""

    def test_block_warns_against_inventing_out_of_scope_tests(self):
        """An agent that pads coverage with unrelated tests makes the metric
        worse, not better — the block must say so explicitly."""
        block = assertions_block([_assertion(1, "4.2 Error Codes")])
        assert "do NOT invent" in block

    def test_render_is_bounded(self):
        many = [_assertion(i, f"section {i}") for i in range(60)]
        block = assertions_block(many, max_render=5)
        assert block.count("tsd-ref:") == 5


class TestMarkerMatchingIsRobustEnoughToEnforce:
    """Exact matching made a trailing period a coverage failure. An enforcing
    gate must not block correct work over formatting."""

    @pytest.mark.parametrize("marker", [
        "4.2 Error Codes",          # exact
        "4.2 error codes",          # case
        "  4.2 Error Codes  ",      # surrounding whitespace
        "§4.2 Error Codes",         # section symbol
        "4.2 Error Codes.",         # trailing period
        "4.2  Error   Codes",       # collapsed internal whitespace
    ])
    def test_cosmetic_variations_still_count_as_coverage(self, marker):
        assertions = [_assertion(1, "4.2 Error Codes")]
        cov = assertion_coverage(assertions, [f"// tsd-ref: {marker}\n@Test void t() {{}}"])
        assert cov["covered"] == 1, f"marker {marker!r} was wrongly treated as uncovered"

    def test_a_different_section_does_not_count(self):
        """Robustness must not become permissiveness — citing the wrong section
        is a real miss and must stay one."""
        assertions = [_assertion(1, "4.2 Error Codes")]
        cov = assertion_coverage(assertions, ["// tsd-ref: 9.9 Something Else"])
        assert cov["covered"] == 0

    def test_partial_section_name_does_not_count(self):
        assertions = [_assertion(1, "4.2 Error Codes")]
        cov = assertion_coverage(assertions, ["// tsd-ref: Error"])
        assert cov["covered"] == 0

    def test_assertion_id_may_be_cited_instead_of_the_section(self):
        assertions = [_assertion(1, "4.2 Error Codes")]
        cov = assertion_coverage(assertions, ["// tsd-ref: api_contract:1"])
        assert cov["covered"] == 1

    @pytest.mark.parametrize("comment", ["#", "//", "--", "*"])
    def test_marker_is_comment_prefix_agnostic(self, comment):
        """Java, Python, SQL and block-comment test sources all carry markers."""
        assertions = [_assertion(1, "4.2 Error Codes")]
        cov = assertion_coverage(assertions, [f"{comment} tsd-ref: 4.2 Error Codes"])
        assert cov["covered"] == 1


class TestNormaliseRef:
    def test_strips_leading_section_noise_and_trailing_punctuation(self):
        assert _normalise_ref("  §4.2 Error Codes. ") == "4.2 error codes"

    def test_preserves_semantic_content(self):
        assert _normalise_ref("4.2") != _normalise_ref("4.3")
        assert _normalise_ref("Error Codes") != _normalise_ref("Error Code")

    def test_empty_and_noise_only_normalise_to_empty(self):
        assert _normalise_ref("") == ""
        assert _normalise_ref("   ") == ""
        assert _normalise_ref("§§ ") == ""


class TestCoverageAccounting:
    def test_partial_coverage_ratio(self):
        assertions = [_assertion(i, f"sec {i}") for i in (1, 2, 3, 4)]
        cov = assertion_coverage(assertions, ["// tsd-ref: sec 1\n// tsd-ref: sec 3"])
        assert cov["total"] == 4
        assert cov["covered"] == 2
        assert cov["coverage_ratio"] == 0.5
        assert {a["tsd_section"] for a in cov["uncovered"]} == {"sec 2", "sec 4"}

    def test_no_assertions_is_fully_covered(self):
        """Nothing to cover is not a gap. The separate feature_test_gate is what
        requires that SOME test exist for a behavioural change."""
        cov = assertion_coverage([], ["anything"])
        assert cov == {"total": 0, "covered": 0, "coverage_ratio": 1.0, "uncovered": []}

    def test_no_test_sources_is_zero_coverage(self):
        cov = assertion_coverage([_assertion(1, "sec 1")], [])
        assert cov["covered"] == 0
        assert cov["coverage_ratio"] == 0.0

    def test_none_test_sources_is_safe(self):
        assert assertion_coverage([_assertion(1, "s")], None)["covered"] == 0

    def test_duplicate_markers_do_not_inflate_coverage(self):
        assertions = [_assertion(1, "sec 1"), _assertion(2, "sec 2")]
        cov = assertion_coverage(
            assertions, ["// tsd-ref: sec 1", "// tsd-ref: sec 1", "// tsd-ref: sec 1"])
        assert cov["covered"] == 1
        assert cov["coverage_ratio"] == 0.5


class TestReferencedTsdRefs:
    def test_collects_across_multiple_sources(self):
        refs = referenced_tsd_refs(["// tsd-ref: A", "# tsd-ref: B"])
        assert refs == {"a", "b"}

    def test_ignores_sources_without_markers(self):
        assert referenced_tsd_refs(["@Test void nothingHere() {}"]) == set()

    def test_tolerates_none_and_empty_entries(self):
        assert referenced_tsd_refs([None, "", "// tsd-ref: X"]) == {"x"}
