# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Hard-fail catalog — every possible FAIL code with meaning and remediation.

A contract's hard_fail_codes list must only reference codes defined here.
This is checked at import time so misconfigured contracts are caught early.

The `code` values are WIRE CONSTANTS (contracts reference them by string,
verdicts persist them) and never vary per domain. Only the prose an operator
reads — meanings, example evidence, remediation — carries domain nouns, which
come from the active pack.
"""
from dataclasses import dataclass

from app.core.domain.registry import prompt_block

_AUTHORITY = prompt_block("authority", "the ecosystem authority")
_EVIDENCE_SOURCES = prompt_block("evidence_sources", "authoritative")


@dataclass(frozen=True)
class HardFailEntry:
    code: str
    title: str
    meaning: str
    example_evidence: str
    remediation: str


# fmt: off
_CATALOG_LIST: list[HardFailEntry] = [
    HardFailEntry(
        code="MISSING_REQUIRED_ARTIFACT",
        title="Required artifact is absent",
        meaning="A source or target artifact declared in the contract is missing entirely.",
        example_evidence="No Tech Spec document found for checkpoint brd_to_tech_spec.",
        remediation="Generate or attach the missing artifact before re-running eval.",
    ),
    HardFailEntry(
        code="EMPTY_OR_PLACEHOLDER_CONTENT",
        title="Artifact contains placeholder or empty content",
        meaning="A mandatory section is empty or contains placeholder text like TODO, TBD, or lorem ipsum.",
        example_evidence="Tech Spec section 'API Contract' contains 'TODO: fill in endpoints'.",
        remediation="Regenerate or edit the artifact to replace placeholder content.",
    ),
    HardFailEntry(
        code="MISSING_MANDATORY_SECTION",
        title="Required section is absent from artifact",
        meaning="A section that must be present according to the checkpoint contract is missing.",
        example_evidence="Tech Spec is missing the 'Error Code Table' section.",
        remediation="Add the missing section and re-run eval.",
    ),
    HardFailEntry(
        code="UNMAPPED_REQUIREMENT",
        title="Source requirement has no target coverage",
        meaning="A functional requirement in the source artifact has no corresponding design in the target artifact.",
        example_evidence="BRD requirement FR-07 (dispute resolution flow) has no Tech Spec coverage.",
        remediation="Add technical design for the unmapped requirement.",
    ),
    HardFailEntry(
        code="CONTRADICTS_APPROVED_SOURCE",
        title="Target artifact contradicts approved source",
        meaning="A claim, field, or rule in the target artifact directly contradicts the approved source artifact.",
        example_evidence="BRD says field is optional; Tech Spec marks same field as mandatory.",
        remediation="Resolve the source-of-truth conflict before advancing.",
    ),
    HardFailEntry(
        # `code` is a WIRE VALUE: eval contracts reference it by string and the
        # reference is validated at import, so it stays as-is. Only the prose,
        # which is what an operator actually reads, is genericised — the codes
        # themselves come from the active pack's `error_code_pattern`.
        code="INVALID_UPI_ERROR_PATTERN",
        title="Artifact uses an invalid or forbidden error-code pattern",
        meaning="HTTP status codes, or codes outside the shape this domain declares, are used where the ecosystem's own error codes are required.",
        example_evidence="Tech Spec uses HTTP 404 as a response code instead of one of the domain's declared error codes.",
        remediation="Replace with error codes matching the shape declared by the active domain pack.",
    ),
    HardFailEntry(
        code="XSD_DECISION_MISMATCH",
        title="XSD assessment decision conflicts with Tech Spec changes",
        meaning="The XSD assessment says schema change is not required, but the Tech Spec introduces new fields or messages.",
        example_evidence="Tech Spec adds new 'merchantCategory' field but XSD assessment is marked NOT_REQUIRED.",
        remediation="Reassess schema requirement in light of the Tech Spec changes.",
    ),
    HardFailEntry(
        code="INCOMPLETE_PRODUCT_KIT",
        title="Partner product kit is missing required document or manifest entry",
        meaning="One or more documents declared in the kit manifest are absent, or the manifest itself is incomplete.",
        example_evidence=f"The {_AUTHORITY} Circular is missing from the product kit manifest.",
        remediation="Regenerate the missing document or update the manifest.",
    ),
    HardFailEntry(
        code="UNSAFE_PARTNER_RESPONSE",
        title="Draft partner response makes unsupported promise or contradicts approved docs",
        meaning="The AI-drafted response commits to something not in approved BRD/TSD or contradicts approved policy.",
        example_evidence="Draft response commits a go-live date not mentioned in any approved document.",
        remediation="Revise the draft to stay within the scope of approved artifacts.",
    ),
    # Phase 7 — Phase A full gate coverage.
    HardFailEntry(
        code="PROMPT_TOO_SHORT",
        title="Enhanced prompt is too short to evaluate",
        meaning="The enhanced prompt does not contain enough content for the research stage to operate on.",
        example_evidence="Enhanced prompt is 12 characters long; minimum is 40.",
        remediation="Refine the enhanced prompt with concrete goals, scope, and context before advancing.",
    ),
    HardFailEntry(
        code="NO_SOURCES_FOUND",
        title="Research summary cites no sources",
        meaning="The research summary contains claims but no traceable sources, breaking grounding for downstream artifacts.",
        example_evidence="Research summary has 800 words and zero citations or source URLs.",
        remediation=f"Regenerate the research summary with cited {_EVIDENCE_SOURCES} authoritative sources.",
    ),
    HardFailEntry(
        code="UNANSWERED_CRITICAL_QUESTION",
        title="Critical clarification questions remain unanswered",
        meaning="The clarification thread still has open canvas-derived questions that block downstream artifact quality.",
        example_evidence="3 of 7 canvas-flagged questions remain in 'pending' state; clarification is not terminal.",
        remediation="Resolve or explicitly defer (with rationale) every open critical question before advancing to BRD.",
    ),
]
# fmt: on

# Build lookup dict — validated at import time
HARD_FAIL_CATALOG: dict[str, HardFailEntry] = {}
_seen_codes: set[str] = set()
for _entry in _CATALOG_LIST:
    if _entry.code in _seen_codes:
        raise ValueError(f"Duplicate hard-fail code in catalog: {_entry.code}")
    _seen_codes.add(_entry.code)
    HARD_FAIL_CATALOG[_entry.code] = _entry


def get_hard_fail(code: str) -> HardFailEntry:
    """Return catalog entry for code. Raises KeyError for unknown codes."""
    if code not in HARD_FAIL_CATALOG:
        raise KeyError(f"Unknown hard-fail code: '{code}'. Add it to hard_fail_catalog.py first.")
    return HARD_FAIL_CATALOG[code]


def all_codes() -> list[str]:
    return list(HARD_FAIL_CATALOG.keys())
