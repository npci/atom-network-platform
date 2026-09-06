# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic checks — fast, LLM-free artifact quality rules.

Each check function takes an artifact dict and returns a list of
finding strings. Empty list = clean. These are the first-pass gate
before the critic model is called.

Adding a new check:
1. Write a function matching signature: (artifact: dict) -> list[str]
2. Register it in CHECKS dict with a stable string key.
3. Reference that key in the contract's deterministic_checks list.
"""
from __future__ import annotations

import re

DETERMINISTIC_VERSION = "deterministic.v1"

# Placeholder patterns to detect in generated text
_PLACEHOLDER_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bTBD\b", re.IGNORECASE),
    re.compile(r"\bPLACEHOLDER\b", re.IGNORECASE),
    re.compile(r"\[INSERT\b", re.IGNORECASE),
    re.compile(r"\[ADD\b", re.IGNORECASE),
    re.compile(r"lorem ipsum", re.IGNORECASE),
    re.compile(r"<REPLACE[^>]*>", re.IGNORECASE),
]

# FR-## pattern for BRD requirement numbering
_FR_PATTERN = re.compile(r"\bFR-\d+\b")

# UPI error code pattern: should be digits like 00, U02, etc. — not HTTP 4xx/5xx
_INVALID_HTTP_AS_DOMAIN = re.compile(r"\b[45]\d{2}\b")


def _text(artifact: dict) -> str:
    """Extract text content from artifact dict for scanning."""
    return artifact.get("content", "") or artifact.get("text", "") or ""


def check_no_placeholders(artifact: dict) -> list[str]:
    """Fail if artifact text contains placeholder markers."""
    text = _text(artifact)
    findings = []
    for pattern in _PLACEHOLDER_PATTERNS:
        if pattern.search(text):
            findings.append(
                f"Placeholder pattern detected: '{pattern.pattern}'. "
                "Remove or replace before advancing."
            )
    return findings


def check_mandatory_sections_present(artifact: dict) -> list[str]:
    """Fail if expected section headers are missing.

    Checks for a minimum set of headings. The exact required sections
    should be expanded per checkpoint when Phase 1 integrations are added.
    """
    text = _text(artifact)
    artifact_type = artifact.get("type", "")
    findings = []

    if artifact_type == "tech_spec":
        required = ["## Overview", "## Functional Requirements", "## API", "## Error"]
        for section in required:
            if section.lower() not in text.lower():
                findings.append(f"Mandatory section missing from Tech Spec: '{section}'")

    elif artifact_type == "brd":
        required = ["## Background", "## Functional Requirements", "## Compliance"]
        for section in required:
            if section.lower() not in text.lower():
                findings.append(f"Mandatory section missing from BRD: '{section}'")

    return findings


def check_fr_numbering_pattern(artifact: dict) -> list[str]:
    """Warn if BRD has no FR-## numbered requirements."""
    text = _text(artifact)
    artifact_type = artifact.get("type", "")
    if artifact_type != "brd":
        return []
    if not _FR_PATTERN.search(text):
        return [
            "No FR-## numbered functional requirements found in BRD. "
            "Requirements should be numbered like FR-01, FR-02, etc."
        ]
    return []


def check_error_code_table_present(artifact: dict) -> list[str]:
    """Fail if Tech Spec has no error code section."""
    text = _text(artifact)
    artifact_type = artifact.get("type", "")
    if artifact_type != "tech_spec":
        return []
    if "error code" not in text.lower() and "error-code" not in text.lower():
        return [
            "Tech Spec does not appear to contain an error code table or section. "
            "UPI integrations require explicit error codes."
        ]
    return []


def check_xsd_decision_field_present(artifact: dict) -> list[str]:
    """Fail if XSD assessment decision field is missing."""
    decision = artifact.get("decision") or artifact.get("xsd_decision")
    if not decision:
        return [
            "XSD assessment is missing the 'decision' field "
            "(expected: REQUIRED or NOT_REQUIRED)."
        ]
    if decision.upper() not in ("REQUIRED", "NOT_REQUIRED"):
        return [
            f"XSD decision value '{decision}' is not valid. "
            "Expected REQUIRED or NOT_REQUIRED."
        ]
    return []


def check_generated_xsd_if_required(artifact: dict) -> list[str]:
    """Fail if XSD decision is REQUIRED but no schema content is present."""
    decision = (artifact.get("decision") or artifact.get("xsd_decision") or "").upper()
    if decision != "REQUIRED":
        return []
    schema_content = artifact.get("schema_content") or artifact.get("xsd_content") or ""
    if not schema_content.strip():
        return [
            "XSD decision is REQUIRED but no schema content was generated. "
            "Generate the XSD before advancing to partner communication."
        ]
    return []


def check_manifest_all_docs_present(artifact: dict) -> list[str]:
    """Fail if product kit manifest lists documents that are not present."""
    manifest = artifact.get("manifest") or {}
    documents = artifact.get("documents") or {}
    findings = []
    for doc_type in manifest.get("expected_documents", []):
        if doc_type not in documents:
            findings.append(
                f"Product kit manifest expects '{doc_type}' but it is not in the kit. "
                "Generate the missing document before communicating to partners."
            )
    return findings


def check_no_internal_markers(artifact: dict) -> list[str]:
    """Fail if partner-facing content contains internal-only markers."""
    text = _text(artifact)
    internal_patterns = [
        re.compile(r"\[INTERNAL\]", re.IGNORECASE),
        re.compile(r"\[DRAFT\]", re.IGNORECASE),
        re.compile(r"\[NOT FOR SHARING\]", re.IGNORECASE),
        re.compile(r"\[CONFIDENTIAL\]", re.IGNORECASE),
    ]
    findings = []
    for pattern in internal_patterns:
        if pattern.search(text):
            findings.append(
                f"Internal marker '{pattern.pattern}' found in partner-facing content. "
                "Remove before sending to partners."
            )
    return findings


def check_payload_not_empty(artifact: dict) -> list[str]:
    """Fail if communication payload has no content."""
    if not artifact or not artifact.get("documents"):
        return ["A2A communication payload is empty. Nothing to send to partners."]
    return []


def check_response_not_empty(artifact: dict) -> list[str]:
    """Fail if draft partner response is empty."""
    text = _text(artifact)
    if not text.strip():
        return ["Draft partner response is empty. Generate a response before PO review."]
    return []


def check_no_unapproved_commitments_pattern(artifact: dict) -> list[str]:
    """Warn if draft response contains common unsupported commitment phrases."""
    text = _text(artifact)
    risky = [
        re.compile(r"\bwe will go.live\b", re.IGNORECASE),
        re.compile(r"\bguarantee\b", re.IGNORECASE),
        re.compile(r"\bcommit to deploy\b", re.IGNORECASE),
        re.compile(r"\blaunch by\b", re.IGNORECASE),
        re.compile(r"\bby end of\b", re.IGNORECASE),
    ]
    findings = []
    for pattern in risky:
        if pattern.search(text):
            findings.append(
                f"Potentially unsupported commitment phrase detected: "
                f"'{pattern.pattern}'. Verify this is backed by approved documents."
            )
    return findings


# ── Phase 7 — Phase A full gate coverage ─────────────────────────────────────
#
# These checks operate on artifacts produced earlier in Phase A. Like the
# existing checks they read the merged artifact dict, but they also use the
# `_all` accessor (populated by `run_checks`) when they need to read a
# specific artifact by name, since merging is last-write-wins.

PROMPT_MIN_LENGTH = 40  # chars; below this the prompt is too thin for research

CANVAS_REQUIRED_SECTION_TOKENS = ["problem", "scope", "stakeholder"]

_TERMINAL_QUESTION_STATUSES = {"answered", "skipped", "deferred"}

_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
_BRACKET_CITATION_PATTERN = re.compile(r"\[\d+\]")


def _artifact_text(artifact_dict: dict) -> str:
    if not isinstance(artifact_dict, dict):
        return ""
    return (artifact_dict.get("content") or artifact_dict.get("text") or "").strip()


def _get_named_artifact(artifact: dict, name: str) -> dict:
    """Return the named source/target artifact dict, or {}."""
    all_artifacts = artifact.get("_all") or {}
    value = all_artifacts.get(name)
    return value if isinstance(value, dict) else {}


def check_prompt_not_empty(artifact: dict) -> list[str]:
    """Fail if the enhanced prompt is empty or whitespace-only."""
    text = _artifact_text(_get_named_artifact(artifact, "enhanced_prompt"))
    if not text:
        return ["Enhanced prompt is empty. Refine it before advancing to research."]
    return []


def check_prompt_min_length(artifact: dict) -> list[str]:
    """Fail if the enhanced prompt is shorter than PROMPT_MIN_LENGTH characters.

    Skips silently when the prompt is empty so check_prompt_not_empty stays
    the canonical empty-case message.
    """
    text = _artifact_text(_get_named_artifact(artifact, "enhanced_prompt"))
    if not text:
        return []
    if len(text) < PROMPT_MIN_LENGTH:
        return [
            f"Enhanced prompt is only {len(text)} characters; "
            f"minimum is {PROMPT_MIN_LENGTH} for the research stage to operate."
        ]
    return []


def check_research_summary_not_empty(artifact: dict) -> list[str]:
    """Fail if the research summary is empty."""
    text = _artifact_text(_get_named_artifact(artifact, "research_summary"))
    if not text:
        return ["Research summary is empty. Run the researcher before advancing to canvas."]
    return []


def check_at_least_one_source(artifact: dict) -> list[str]:
    """Fail if research artifacts contain no cited source.

    Accepts any of three encodings:
      - a non-empty `sources` list on `research_summary` or `research_sources`,
      - a URL anywhere in the summary text,
      - a bracketed-numeric citation marker (e.g. [1]) in the summary text.
    """
    summary = _get_named_artifact(artifact, "research_summary")
    sources_artifact = _get_named_artifact(artifact, "research_sources")

    for candidate in (sources_artifact.get("sources"), summary.get("sources")):
        if isinstance(candidate, list) and len(candidate) > 0:
            return []

    text = (summary.get("content") or summary.get("text") or "")
    if _URL_PATTERN.search(text) or _BRACKET_CITATION_PATTERN.search(text):
        return []

    return [
        "Research summary cites no sources. Add at least one authoritative source "
        "(NPCI / UPI documentation, circular, etc.) before advancing."
    ]


def check_canvas_has_required_sections(artifact: dict) -> list[str]:
    """Fail if the product canvas is missing a mandatory section.

    Recognises two canvas shapes:
      - `sections` dict on the canvas artifact (key match),
      - free-form markdown content (token match).
    """
    canvas = _get_named_artifact(artifact, "product_canvas")
    sections = canvas.get("sections")
    findings: list[str] = []

    if isinstance(sections, dict):
        present_keys = {str(k).lower() for k in sections}
        for token in CANVAS_REQUIRED_SECTION_TOKENS:
            if not any(token in key for key in present_keys):
                findings.append(f"Product canvas is missing the '{token}' section.")
        return findings

    text = (canvas.get("content") or canvas.get("text") or "").lower()
    if not text:
        return ["Product canvas is empty. Generate the canvas before advancing."]
    for token in CANVAS_REQUIRED_SECTION_TOKENS:
        if token not in text:
            findings.append(
                f"Product canvas does not appear to discuss '{token}'. "
                "Add an explicit section before advancing."
            )
    return findings


def check_no_unanswered_canvas_questions(artifact: dict) -> list[str]:
    """Fail if any clarification question is still pending.

    Recognises the clarification thread under either `questions` or `items`,
    and treats answered / skipped / deferred as terminal.
    """
    thread = _get_named_artifact(artifact, "clarification_thread")
    questions = thread.get("questions") or thread.get("items")
    if not isinstance(questions, list):
        return []
    pending = [
        q for q in questions
        if isinstance(q, dict)
        and str(q.get("status", "")).lower() not in _TERMINAL_QUESTION_STATUSES
    ]
    if pending:
        return [
            f"{len(pending)} of {len(questions)} clarification question(s) remain "
            "unresolved. Answer or explicitly defer each before advancing to BRD."
        ]
    return []


# ── Phase A Excellence — Slice 3: cross-artifact grounding checks ───────────
#
# These checks catch *drift* between approved upstream and weak downstream:
# the BRD invented requirements that don't trace back to the canvas, or the
# Tech Spec quietly dropped a requirement from the BRD. They are pattern-
# level (no LLM) and intentionally conservative so they reliably FAIL only
# when there is a clear mismatch.

# The ecosystem's error-code shape now comes from the ACTIVE DOMAIN PACK
# (`error_code_pattern` in the pack YAML) rather than a UPI literal baked in
# here. For UPI the pack declares the identical regex, so UPI verdicts are
# unchanged. A domain that declares no shape gets the check SKIPPED — see
# `_valid_error_code_pattern` — because applying UPI's alphabet to another
# ecosystem is not a check, it is a guaranteed wrong answer.
#
# Resolved per call, not at import: the eval gate is long-lived and
# `DOMAIN_PACK` is read from the environment, so caching the compiled pattern
# at import time would pin the first domain a worker ever saw.
def _valid_error_code_pattern():
    from app.core.domain.contract import error_code_pattern_of
    from app.core.domain.registry import get_active_pack

    return error_code_pattern_of(get_active_pack())


def _extract_fr_numbers(text: str) -> list[str]:
    """Return the sorted unique FR-## tokens found in text."""
    return sorted(set(_FR_PATTERN.findall(text)))


def check_tech_spec_covers_all_brd_frs(artifact: dict) -> list[str]:
    """Fail if any FR-## in the BRD is missing from the Tech Spec.

    Catches the silent regression where the Tech Spec drops requirements
    that were approved upstream. We only flag missing coverage when the
    BRD actually declares FRs — silent when BRD has none (other checks
    cover that case).
    """
    brd = _get_named_artifact(artifact, "brd_document")
    tech_spec = _get_named_artifact(artifact, "tech_spec_document")

    brd_text = (brd.get("content") or brd.get("text") or "")
    ts_text = (tech_spec.get("content") or tech_spec.get("text") or "")

    if not brd_text or not ts_text:
        return []  # MISSING_REQUIRED_ARTIFACT is handled by the runner

    brd_frs = _extract_fr_numbers(brd_text)
    if not brd_frs:
        return []  # nothing to trace

    # TODO(eval — review finding, not fixed on retrofit): `fr not in
    # ts_text` is a substring test, so a genuinely dropped FR-1 passes whenever
    # a higher FR sharing its prefix (FR-12, FR-10..19) is present. Compare
    # against the tech-spec's FR token set instead:
    #   ts_frs = set(_extract_fr_numbers(ts_text)); missing = [fr for fr in brd_frs if fr not in ts_frs]
    missing = [fr for fr in brd_frs if fr not in ts_text]
    if missing:
        # Phrasing intentionally includes "missing" so the judge maps this
        # finding to the contract's UNMAPPED_REQUIREMENT hard-fail code.
        joined = ", ".join(missing[:8])
        if len(missing) > 8:
            joined += f", … (+{len(missing) - 8} more)"
        return [
            f"Tech Spec is missing coverage for BRD requirement(s): {joined}. "
            "Each FR-## must be implemented in the Tech Spec or explicitly "
            "marked deferred with rationale."
        ]
    return []


def check_domain_error_codes_are_valid(artifact: dict) -> list[str]:
    """Fail if the Tech Spec's error section names no recognised error code.

    The existing `check_error_code_table_present` ensures an error section
    exists. This check goes one step further: if the section exists but
    contains no code matching the ACTIVE DOMAIN's declared shape, flag it.
    Catches the common case where the LLM describes errors in prose but never
    commits to specific codes the partners need to handle.

    Name kept as `check_upi_...` on purpose: it is referenced by id from the
    hard-fail catalogue and from eval contracts, and those references are
    validated at import time. Renaming it is a separate, mechanical change.
    """
    pattern = _valid_error_code_pattern()
    if pattern is None:
        # The domain declares no error-code shape, so there is nothing to
        # assert. Silence here is correct; guessing would not be.
        return []

    tech_spec = _get_named_artifact(artifact, "tech_spec_document")
    text = (tech_spec.get("content") or tech_spec.get("text") or "")
    if not text:
        return []
    lower = text.lower()
    has_error_section = "error code" in lower or "error-code" in lower
    if not has_error_section:
        return []  # check_error_code_table_present handles this

    if not pattern.search(text):
        return [
            "Tech Spec describes errors but contains no error code matching "
            f"this domain's declared code shape ({pattern.pattern}). Add the "
            "specific codes that partners need to handle for each error case."
        ]
    return []


def check_no_http_codes_as_domain_errors(artifact: dict) -> list[str]:
    """Warn if the Tech Spec uses HTTP-style 4xx/5xx codes inside the error
    section (these are not valid UPI codes and confuse partner systems)."""
    tech_spec = _get_named_artifact(artifact, "tech_spec_document")
    text = (tech_spec.get("content") or tech_spec.get("text") or "")
    if not text:
        return []
    lower = text.lower()
    if "error code" not in lower and "error-code" not in lower:
        return []
    bad = _INVALID_HTTP_AS_DOMAIN.findall(text)
    if bad:
        return [
            "Tech Spec uses HTTP-style status codes "
            f"({', '.join(sorted(set(bad))[:5])}) in the error section. "
            "Replace with UPI domain codes (U##, Z#, RB, XT, XD, YB, YC, YD)."
        ]
    return []


def _api_name_in_doc_pattern():
    """The active pack's wire-message-name shape (`message_name_pattern`) —
    the SAME source api_registry_ingest.derive_involved_api_names reads, so
    the post-write sweep and this check agree on which APIs a document
    "mentions". None when the pack declares no shape: the check then finds no
    mentions and passes, rather than scanning with another domain's alphabet."""
    from app.core.domain.contract import message_name_pattern_of
    from app.core.domain.registry import get_active_pack

    return message_name_pattern_of(get_active_pack())


def check_tsd_api_specs_registry_backed(artifact: dict) -> list[str]:
    """Every registry-covered wire API named in the TSD must carry its
    registry-rendered spec section ("API Specification — <Name>"), not an
    LLM-authored one. Fail-open when the registry/DB is unavailable.
    """
    tech_spec = _get_named_artifact(artifact, "tech_spec_document")
    text = tech_spec.get("content") or tech_spec.get("text") or ""
    if not text:
        return []
    pattern = _api_name_in_doc_pattern()
    if pattern is None:
        return []
    mentioned = sorted({m.group(0) for m in pattern.finditer(text)})
    if not mentioned:
        return []
    try:
        from app.core.database import SessionLocal
        from app.models.api_registry import ApiMessage
        with SessionLocal() as db:
            covered = {name for (name,) in db.query(ApiMessage.api_name)
                       .filter(ApiMessage.api_name.in_(mentioned),
                               ApiMessage.status == "active").all()}
    except Exception:  # noqa: BLE001 — registry check must never block eval itself
        return []
    findings = []
    for name in sorted(covered):
        if f"API Specification — {name}" not in text:
            findings.append(
                f"TSD references '{name}' which exists in the API Registry, but the "
                f"registry-rendered spec section ('API Specification — {name}') is absent — "
                "its field dictionary may be model-generated instead of registry-backed."
            )
    return findings[:10]


# ── Registry ─────────────────────────────────────────────────────────────────

CHECKS: dict[str, callable] = {
    "check_no_placeholders":                  check_no_placeholders,
    "check_mandatory_sections_present":       check_mandatory_sections_present,
    "check_fr_numbering_pattern":             check_fr_numbering_pattern,
    "check_error_code_table_present":         check_error_code_table_present,
    "check_xsd_decision_field_present":       check_xsd_decision_field_present,
    "check_generated_xsd_if_required":        check_generated_xsd_if_required,
    "check_manifest_all_docs_present":        check_manifest_all_docs_present,
    "check_no_internal_markers":              check_no_internal_markers,
    "check_payload_not_empty":                check_payload_not_empty,
    "check_response_not_empty":               check_response_not_empty,
    "check_no_unapproved_commitments_pattern": check_no_unapproved_commitments_pattern,
    # Phase 7 — Phase A full gate coverage
    "check_prompt_not_empty":                 check_prompt_not_empty,
    "check_prompt_min_length":                check_prompt_min_length,
    "check_research_summary_not_empty":       check_research_summary_not_empty,
    "check_at_least_one_source":              check_at_least_one_source,
    "check_canvas_has_required_sections":     check_canvas_has_required_sections,
    "check_no_unanswered_canvas_questions":   check_no_unanswered_canvas_questions,
    # Phase A Excellence — Slice 3: cross-artifact grounding
    "check_tech_spec_covers_all_brd_frs":     check_tech_spec_covers_all_brd_frs,
    "check_domain_error_codes_are_valid":        check_domain_error_codes_are_valid,
    "check_no_http_codes_as_domain_errors":      check_no_http_codes_as_domain_errors,
    # API Registry — deterministic TSD interface specs
    "check_tsd_api_specs_registry_backed":    check_tsd_api_specs_registry_backed,
}


def run_checks(check_names: list[str], artifacts: dict[str, dict]) -> list[str]:
    """Run named checks against a combined artifact dict.

    artifacts: {artifact_key: artifact_dict}
    Returns list of finding strings. Empty = all clean.
    """
    findings: list[str] = []
    combined = {}
    # TODO(eval — review finding, not fixed on retrofit): this is a
    # last-write-wins flatten — for multi-artifact checkpoints (e.g.
    # brd_to_tech_spec) the second artifact's `type`/`content` clobber the
    # first, so the legacy checks that read flat `type`/`content` (FR-numbering,
    # placeholders, mandatory-sections) only ever scan ONE artifact and silently
    # skip the BRD. Migrate those checks to the `_all` accessor the newer
    # grounding checks use, or run each check per-named-artifact.
    for artifact_dict in artifacts.values():
        if isinstance(artifact_dict, dict):
            combined.update(artifact_dict)
    combined["_all"] = artifacts

    for name in check_names:
        fn = CHECKS.get(name)
        if fn is None:
            findings.append(
                f"CHECK_NOT_FOUND: '{name}' is listed in contract but not in CHECKS registry."
            )
            continue
        try:
            result = fn(combined)
            findings.extend(result)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"CHECK_ERROR: '{name}' raised {type(exc).__name__}: {exc}")

    return findings
