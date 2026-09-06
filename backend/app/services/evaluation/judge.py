# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Judge policy for advisory verdict selection.

Phase 1 keeps judging deterministic-only and model-free. The runner can pass
critic findings later (Phase 2) without changing endpoint integrations.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.evaluation.checkpoints import VerdictValue

JUDGE_MODEL = "rule-based.judge.v1"


@dataclass(slots=True)
class JudgeDecision:
    verdict: VerdictValue
    passed: bool
    confidence: float
    hard_fail_codes: list[str]
    warn_codes: list[str]
    reasons: list[str]
    judge_model: str = JUDGE_MODEL


def judge_advisory(
    *,
    deterministic_findings: list[str],
    contract_hard_fail_codes: list[str],
    critic_findings: list[str] | None = None,
) -> JudgeDecision:
    """Combine deterministic (and optional critic) findings into one verdict."""
    critic_was_run = critic_findings is not None
    critic = critic_findings or []
    findings = [*deterministic_findings, *critic]
    hard_findings = [
        finding
        for finding in findings
        if is_hard_fail_finding(finding, contract_hard_fail_codes)
    ]

    if hard_findings:
        return JudgeDecision(
            verdict=VerdictValue.FAIL,
            passed=False,
            confidence=0.95,
            hard_fail_codes=extract_hard_fail_codes(hard_findings, contract_hard_fail_codes),
            warn_codes=[],
            reasons=findings,
        )

    if findings:
        return JudgeDecision(
            verdict=VerdictValue.WARN,
            passed=True,
            confidence=0.6,
            hard_fail_codes=[],
            warn_codes=extract_warn_codes(findings),
            reasons=findings + ["Advisory mode: critic model not yet enabled (Phase 2)."],
        )

    pass_reason = (
        "All deterministic and critic checks passed."
        if critic_was_run
        else "All deterministic checks passed; critic did not run."
    )
    return JudgeDecision(
        verdict=VerdictValue.PASS,
        passed=True,
        confidence=0.75,
        hard_fail_codes=[],
        warn_codes=[],
        reasons=[pass_reason],
    )


def is_hard_fail_finding(finding: str, contract_hard_fail_codes: list[str]) -> bool:
    """Return True if a finding should produce FAIL for this contract."""
    # TODO(eval — review finding, not fixed on retrofit): deciding
    # FAIL by substring keyword-match on free-text findings is fragile in two
    # ways flagged in review:
    #   (1) DOWNGRADE: contract hard-fail findings whose prose lacks a keyword
    #       (e.g. "remain unresolved" for UNANSWERED_CRITICAL_QUESTION,
    #       "cites no sources" for NO_SOURCES_FOUND) silently route to WARN —
    #       the gate is weaker than the contracts advertise.
    #   (2) AMPLIFY/INJECTION: critic findings are verbatim LLM output, so an
    #       artifact that steers the critic into a string merely containing
    #       "missing"/"invalid"/"required" forces a hard FAIL regardless of the
    #       numeric score. Drive hard-fail off structured (code, score<minimum)
    #       signals instead of scanning prose.
    if finding.startswith("CHECK_NOT_FOUND") or finding.startswith("CHECK_ERROR"):
        return False
    if not contract_hard_fail_codes:
        return False

    finding_lower = finding.lower()
    hard_keywords = [
        "missing",
        "placeholder",
        "empty",
        "not found",
        "invalid",
        "required",
        "absent",
    ]
    return any(keyword in finding_lower for keyword in hard_keywords)


def extract_hard_fail_codes(hard_findings: list[str], contract_codes: list[str]) -> list[str]:
    """Map hard findings to contract-scoped hard-fail codes.

    Keep mapping explainable: the verdict code should describe the evidence,
    not merely echo the first contract code. Unknown patterns still fall back
    conservatively to the first code allowed by the contract.
    """
    if not hard_findings or not contract_codes:
        return []

    allowed = set(contract_codes)
    mapped: list[str] = []

    def add(code: str) -> None:
        if code in allowed and code not in mapped:
            mapped.append(code)

    for finding in hard_findings:
        text = finding.lower()
        if "required artifact" in text or "artifact(s) missing" in text:
            add("MISSING_REQUIRED_ARTIFACT")
        if "placeholder" in text or "empty" in text or "todo" in text or "tbd" in text:
            add("EMPTY_OR_PLACEHOLDER_CONTENT")
        if "enhanced prompt" in text and ("minimum" in text or "too short" in text):
            add("PROMPT_TOO_SHORT")
        if "no sources" in text or "cites no sources" in text or "authoritative source" in text:
            add("NO_SOURCES_FOUND")
        if "mandatory section" in text or "error code table" in text or "error code section" in text:
            add("MISSING_MANDATORY_SECTION")
        if "missing coverage" in text or "unmapped" in text:
            add("UNMAPPED_REQUIREMENT")
        if "contradict" in text:
            add("CONTRADICTS_APPROVED_SOURCE")
        if "invalid" in text and ("network" in text or "error" in text):
            add("INVALID_UPI_ERROR_PATTERN")
        if "xsd" in text and ("decision" in text or "schema" in text):
            add("XSD_DECISION_MISMATCH")
        if "product kit" in text or "kit manifest" in text:
            add("INCOMPLETE_PRODUCT_KIT")
        if "partner response" in text or "unsupported promise" in text:
            add("UNSAFE_PARTNER_RESPONSE")
        if "clarification" in text and ("unanswered" in text or "pending" in text or "remain" in text):
            add("UNANSWERED_CRITICAL_QUESTION")

    return mapped or contract_codes[:1]


def extract_warn_codes(findings: list[str]) -> list[str]:
    codes: list[str] = []
    if any(finding.startswith("CHECK_NOT_FOUND") for finding in findings):
        codes.append("CHECK_REGISTRY_MISMATCH")
    if any(finding.startswith("CHECK_ERROR") for finding in findings):
        codes.append("CHECK_EXECUTION_ERROR")
    return codes
