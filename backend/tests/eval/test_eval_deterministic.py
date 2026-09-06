# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1 deterministic-check coverage for eval harness."""
from app.services.evaluation.deterministic import run_checks


class TestDeterministicChecks:
    def test_unknown_check_name_returns_registry_finding(self):
        findings = run_checks(
            ["not_a_real_check"],
            {"doc": {"type": "tech_spec", "content": "ok"}},
        )
        assert any(finding.startswith("CHECK_NOT_FOUND:") for finding in findings)

    def test_placeholder_pattern_is_detected(self):
        findings = run_checks(
            ["check_no_placeholders"],
            {"doc": {"type": "tech_spec", "content": "TODO: fill this section"}},
        )
        assert any("Placeholder pattern detected" in finding for finding in findings)

    def test_missing_tech_spec_sections_are_detected(self):
        findings = run_checks(
            ["check_mandatory_sections_present"],
            {"doc": {"type": "tech_spec", "content": "## Overview\nOnly one section"}},
        )
        assert any("Mandatory section missing from Tech Spec" in finding for finding in findings)

    def test_xsd_required_without_schema_fails(self):
        findings = run_checks(
            ["check_generated_xsd_if_required"],
            {"xsd": {"decision": "REQUIRED", "schema_content": ""}},
        )
        assert findings
        assert "XSD decision is REQUIRED" in findings[0]

    def test_manifest_mismatch_is_detected(self):
        findings = run_checks(
            ["check_manifest_all_docs_present"],
            {
                "payload": {
                    "manifest": {"expected_documents": ["faq", "circular"]},
                    "documents": {"faq": {"content": "ok"}},
                }
            },
        )
        assert any("expects 'circular'" in finding for finding in findings)

    def test_non_empty_payload_passes_payload_check(self):
        findings = run_checks(
            ["check_payload_not_empty"],
            {"payload": {"documents": {"faq": {"content": "text"}}}},
        )
        assert findings == []
