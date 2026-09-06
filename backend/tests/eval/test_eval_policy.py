# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1 judge policy tests."""
from app.services.evaluation.checkpoints import VerdictValue
from app.services.evaluation.judge import (
    extract_warn_codes,
    is_hard_fail_finding,
    judge_advisory,
)


class TestJudgePolicy:
    def test_hard_fail_finding_produces_fail(self):
        decision = judge_advisory(
            deterministic_findings=["Mandatory section missing from Tech Spec: '## API'"],
            contract_hard_fail_codes=["MISSING_MANDATORY_SECTION", "UNMAPPED_REQUIREMENT"],
        )
        assert decision.verdict == VerdictValue.FAIL
        assert decision.passed is False
        assert decision.hard_fail_codes == ["MISSING_MANDATORY_SECTION"]

    def test_infra_findings_are_warn_not_fail(self):
        decision = judge_advisory(
            deterministic_findings=["CHECK_ERROR: 'check_no_placeholders' raised RuntimeError: boom"],
            contract_hard_fail_codes=["EMPTY_OR_PLACEHOLDER_CONTENT"],
        )
        assert decision.verdict == VerdictValue.WARN
        assert decision.passed is True
        assert decision.hard_fail_codes == []
        assert "CHECK_EXECUTION_ERROR" in decision.warn_codes

    def test_empty_findings_produce_pass(self):
        decision = judge_advisory(
            deterministic_findings=[],
            contract_hard_fail_codes=["MISSING_REQUIRED_ARTIFACT"],
        )
        assert decision.verdict == VerdictValue.PASS
        assert decision.passed is True
        assert decision.reasons

    def test_is_hard_fail_requires_contract_codes(self):
        finding = "Mandatory section missing from Tech Spec: '## Error'"
        assert is_hard_fail_finding(finding, ["MISSING_MANDATORY_SECTION"]) is True
        assert is_hard_fail_finding(finding, []) is False

    def test_warn_code_extraction(self):
        warnings = extract_warn_codes(
            [
                "CHECK_NOT_FOUND: 'x'",
                "CHECK_ERROR: 'y' raised ValueError: bad",
            ]
        )
        assert warnings == ["CHECK_REGISTRY_MISMATCH", "CHECK_EXECUTION_ERROR"]
