# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Post-generation document validator.

Runs regex-based checks against generated BRD / Tech Spec / XSD / Canvas /
Product Kit output. Catches common LLM failure modes (HTTP codes instead
of UPI error codes, vague obligation language, placeholder text, missing FR
numbering, etc.) BEFORE the artefact is shown to the user.

Usage:
    from app.agents.document_validator import validate

    issues = validate(content, doc_type="brd")
    # issues = [
    #     {"severity": "error",   "rule": "http_codes",   "message": "..."},
    #     {"severity": "warning", "rule": "placeholder",  "message": "..."},
    # ]
"""
import logging
import re
from typing import Literal

from app.agents.blueprints import get as _get_blueprint

logger = logging.getLogger(__name__)

Severity = Literal["error", "warning"]

Issue = dict  # {"severity": "error"|"warning", "rule": str, "message": str, "evidence": str}


# ─── Regex patterns ──────────────────────────────────────────────────────────

# HTTP status codes appearing as transaction-error codes (forbidden in domain
# docs whose ecosystem declares its own error-code alphabet).
# Intentionally exclude 200/201 — those are generally fine when talking about
# successful HTTP responses in integration docs.
_HTTP_ERROR_CODE_RE = re.compile(
    r"\b(400|401|403|404|409|422|500|502|503|504)\b"
)


# The active domain's error-code shape (for positive presence checks).
# Resolved per call, not at import — same reasoning as
# `services/evaluation/deterministic._valid_error_code_pattern`: the process is
# long-lived and `DOMAIN_PACK` comes from the environment, so caching the
# compiled pattern at import time would pin the first domain a worker ever saw.
# None means the domain declares no code shape, and the presence check SKIPS
# rather than falling back to another domain's alphabet.
def _domain_error_code_pattern():
    from app.core.domain.contract import error_code_pattern_of
    from app.core.domain.registry import get_active_pack

    return error_code_pattern_of(get_active_pack())


def _obligation_example() -> str:
    """'(e.g. 'NPCI shall', 'PSP Bank must')' built from the active pack's
    participants instead of restating UPI's actors."""
    from app.core.domain.contract import participants_of
    from app.core.domain.registry import get_active_pack

    participants = participants_of(get_active_pack())
    authority = next((p.label for p in participants if p.is_authority), None)
    other = next((p.label for p in participants if not p.is_authority), None)
    if authority and other:
        return f"(e.g. '{authority} shall', '{other} must')"
    return "(e.g. an explicit actor + shall/must)"

# Functional requirement numbering.
_FR_RE = re.compile(r"\bFR-\d{2,}\b")

# Common placeholder strings that must NEVER appear in final output.
# NOTE: "XXX" is intentionally NOT a substring placeholder — it's the canonical
# NPCI convention for masking sensitive values (account numbers, card numbers,
# OTPs) in XML payload samples e.g. `<Detail name="ACNUM" value="XXXXXXXX5678"/>`.
# Substring-matching "XXX" produces a false positive on every masked sample.
# A standalone "XXX" placeholder (whole word, not part of a longer mask) is
# still flagged — see _placeholder_issues.
_PLACEHOLDERS = ("TBD", "TODO", "<placeholder>", "lorem ipsum", "Lorem Ipsum")
_STANDALONE_XXX_RE = re.compile(r"(?<![A-Za-z0-9_])XXX(?![A-Za-z0-9_])")

# Vague obligation language — swap for explicit subject + shall/must.
_VAGUE_OBLIGATION_RE = re.compile(
    r"\b(the system will|it (should|would|could) probably|ideally|in some cases)\b",
    re.IGNORECASE,
)

# Invented API names — UPI canonical is Req<Pascal> / Resp<Pascal>. SCREAMING_
# SNAKE_CASE patterns like REQ_CORP_CIRCLE_ENROLL are non-canonical and must
# be flagged so the LLM revises them. Match REQ_ or RESP_ followed by uppercase
# letters and underscores. Excludes legitimate identifiers like REQ_TIMEOUT
# (which is also non-canonical for an API name but might be a constant) by
# requiring at least 2 underscore-separated words after the prefix.
_INVENTED_API_RE = re.compile(
    r"\b(?:REQ|RESP)_[A-Z][A-Z0-9]+_[A-Z][A-Z0-9_]+\b"
)

# Currency patterns.
_BAD_CURRENCY_RE = re.compile(r"\bRs\.?\s*\d")  # "Rs 100", "Rs. 100"


# ─── Universal checks ────────────────────────────────────────────────────────

def _http_code_issues(content: str) -> list[Issue]:
    out: list[Issue] = []
    for m in _HTTP_ERROR_CODE_RE.finditer(content):
        # Skip if the number looks like a year, a port, a paragraph count,
        # or clearly not an error code — heuristic: require "error" or
        # "code" within ±40 chars.
        start, end = max(0, m.start() - 40), m.end() + 40
        context = content[start:end].lower()
        if "error" in context or "code" in context or "response" in context:
            pattern = _domain_error_code_pattern()
            hint = (f" This domain's codes match {pattern.pattern}."
                    if pattern is not None else "")
            out.append({
                "severity": "error",
                "rule": "http_codes",
                "message": (f"HTTP status code '{m.group()}' used where a domain "
                            f"error code is expected.{hint}"),
                "evidence": content[start:end].strip(),
            })
    return out


def _placeholder_issues(content: str) -> list[Issue]:
    out: list[Issue] = []
    for ph in _PLACEHOLDERS:
        if ph in content:
            # Find first occurrence for evidence
            idx = content.find(ph)
            start, end = max(0, idx - 30), idx + len(ph) + 30
            out.append({
                "severity": "error",
                "rule": "placeholder",
                "message": f"Output contains placeholder '{ph}'. Replace with actual content or an explicit Assumption.",
                "evidence": content[start:end].strip(),
            })
    # Standalone "XXX" check — only flags whole-word XXX, not masked values
    # like "XXXXXXXX5678" or "ACNUM_XXX_VALUE".
    m = _STANDALONE_XXX_RE.search(content)
    if m:
        start, end = max(0, m.start() - 30), m.end() + 30
        out.append({
            "severity": "error",
            "rule": "placeholder",
            "message": "Output contains standalone placeholder 'XXX'. Replace with actual content or an explicit Assumption. (Masked values like 'XXXXXXXX5678' are allowed.)",
            "evidence": content[start:end].strip(),
        })
    return out


def _vague_language_issues(content: str) -> list[Issue]:
    out: list[Issue] = []
    for m in _VAGUE_OBLIGATION_RE.finditer(content):
        start, end = max(0, m.start() - 30), m.end() + 30
        out.append({
            "severity": "warning",
            "rule": "vague_obligation",
            "message": (f"Vague obligation language: '{m.group()}'. Use active "
                        f"subject + shall/must {_obligation_example()}."),
            "evidence": content[start:end].strip(),
        })
    return out


def _currency_issues(content: str) -> list[Issue]:
    out: list[Issue] = []
    for m in _BAD_CURRENCY_RE.finditer(content):
        start, end = max(0, m.start() - 20), m.end() + 20
        out.append({
            "severity": "warning",
            "rule": "currency_format",
            "message": f"Use 'INR' (not '{m.group().strip()}') for amounts outside customer-facing display mockups.",
            "evidence": content[start:end].strip(),
        })
    return out


def canonicalize_api_names(content: str) -> str:
    """Convert SCREAMING_SNAKE UPI API placeholders (REQ_FOO_BAR / RESP_FOO_BAR) to
    canonical Req/Resp PascalCase (ReqFooBar / RespFooBar). Deterministic fix for the
    naming FORMAT that ``_invented_api_issues`` flags — the writer occasionally emits
    the wrong form despite the prompt rule. Only REQ_/RESP_-prefixed names are touched,
    so error codes / enum constants (e.g. LIMIT_BREACH) are left alone."""
    def _repl(m) -> str:
        prefix, rest = m.group().split("_", 1)
        head = "Req" if prefix == "REQ" else "Resp"
        return head + "".join(p.capitalize() for p in rest.split("_"))
    return _INVENTED_API_RE.sub(_repl, content or "")


def _invented_api_issues(content: str) -> list[Issue]:
    """Flag SCREAMING_SNAKE_CASE API placeholders (REQ_FOO_BAR style). The
    canonical shape (Req<Pascal>/Resp<Pascal>) is shared across our reference
    domains; how to FIX the name is domain wording, supplied by the pack's
    `canonical_api_name_note` block. Cap at 5 distinct findings to avoid
    drowning the reviewer when the LLM uses one wrong name 30 times."""
    from app.core.domain.registry import prompt_block

    fix_note = prompt_block(
        "canonical_api_name_note",
        "Canonical form is 'ReqXxxYyy' / 'RespXxxYyy' (PascalCase after "
        "Req/Resp). Replace with the actual API being extended, or rename "
        "to canonical form.",
    )
    out: list[Issue] = []
    seen: set[str] = set()
    for m in _INVENTED_API_RE.finditer(content):
        name = m.group()
        if name in seen:
            continue
        seen.add(name)
        start, end = max(0, m.start() - 30), m.end() + 30
        out.append({
            "severity": "error",
            "rule": "invented_api_name",
            "message": f"Non-canonical API placeholder '{name}' used. {fix_note}",
            "evidence": content[start:end].strip(),
        })
        if len(seen) >= 5:
            break
    return out


def _universal(content: str) -> list[Issue]:
    return (
        _http_code_issues(content)
        + _placeholder_issues(content)
        + _vague_language_issues(content)
        + _currency_issues(content)
        + _invented_api_issues(content)
    )


def _candidate_blueprints(doc_type: str) -> list[tuple[str, dict]]:
    """Every blueprint that could legitimately describe `doc_type`.

    There are TWO document schemas in this codebase and both are live:

      * `app.agents.blueprints`        — drives the markdown/agents generator
      * `app.docgen.document_guides`   — drives the LangGraph .docx pipeline

    For BRD they share ZERO section headings ("1. Executive Summary" … vs
    "i. Current State" …), so validating a document produced by one against the
    blueprint of the other reported 12 of 14 required sections missing on every
    single docgen-generated BRD. See docs/genericization/04-target-architecture.md §8.3.
    """
    out: list[tuple[str, dict]] = []
    bp = _get_blueprint(doc_type)
    if bp:
        out.append(("agents", bp))
    try:
        # Lazy: keeps `agents` importable without pulling the docgen graph in,
        # which matters for the test harness  .
        from app.docgen.document_guides import get_document_blueprint
        dbp = get_document_blueprint(doc_type)
        if dbp and dbp.get("sections"):
            out.append(("docgen", dbp))
    except Exception:  # noqa: BLE001 — a missing/!importable docgen must not
        pass          # break validation of agent-generated documents.
    return out


def _missing_required_headings(content_lc: str, bp: dict) -> list[str]:
    missing: list[str] = []
    for s in bp["sections"]:
        if not s.get("required", True):
            continue
        heading = s.get("heading") or ""
        if not heading:
            continue
        # Match either the full "1. Executive Summary" or its label text after
        # the number (also handles the docgen "i." / "A." prefixes).
        label = re.sub(r"^([0-9]+|[ivxlc]+|[A-Z])[.)]\s*", "", heading, flags=re.IGNORECASE).strip()
        if heading.lower() not in content_lc and (not label or label.lower() not in content_lc):
            missing.append(heading)
    return missing


def _blueprint_section_issues(content: str, doc_type: str) -> list[Issue]:
    """Verify every *required* section heading appears, against whichever
    blueprint the document actually follows.

    Previously this checked one schema unconditionally, which made the warning
    permanently wrong for the other pipeline's output — noise that trains
    reviewers to ignore validation. Scoring both and reporting the closest match
    cannot produce a false positive, and still flags a document that follows
    NEITHER schema (which is the real failure this rule exists to catch).

    This is CONTAINMENT, not a resolution: two document schemas coexisting is a
    design question for the pack contract, not something a validator should
    paper over permanently.
    """
    candidates = _candidate_blueprints(doc_type)
    if not candidates:
        return []

    lc = content.lower()
    scored = [(source, bp, _missing_required_headings(lc, bp)) for source, bp in candidates]
    # Fewest missing == the schema this document was most likely generated from.
    source, _bp, missing = min(scored, key=lambda t: len(t[2]))
    if not missing:
        return []

    suffix = "" if len(scored) == 1 else f" (closest match: {source} schema)"
    return [{
        "severity": "warning",
        "rule": "missing_blueprint_sections",
        "message": (f"Required section(s) missing per {doc_type} blueprint{suffix}: "
                    f"{', '.join(missing)}"),
        "evidence": "",
    }]


# ─── Per-doc-type checks ─────────────────────────────────────────────────────

def _validate_brd(content: str) -> list[Issue]:
    issues = _universal(content)
    frs = set(_FR_RE.findall(content))

    if len(frs) < 6:
        issues.append({
            "severity": "error",
            "rule": "fr_count",
            "message": f"BRD requires at least 6 functional requirements — found {len(frs)}.",
            "evidence": ", ".join(sorted(frs)) or "(none)",
        })

    # Legacy REQ-F style still appearing is a warning (older prompts)
    if re.search(r"\bREQ-F\d", content):
        issues.append({
            "severity": "warning",
            "rule": "legacy_fr_style",
            "message": "Legacy 'REQ-F##' numbering detected; standard is 'FR-##'.",
            "evidence": "",
        })

    # Legacy regex-based blueprint check disabled — the docgen pipeline uses a
    # different blueprint (i. Current State / ii. Limitations / etc.) than this
    # validator's hard-coded list (1. Executive Summary / 2. Background / etc.),
    # so the check fired false positives for every BRD generated through the
    # new pipeline. Section completeness is now governed by the planner against
    # the live blueprint.
    return issues


def _validate_tech_spec(content: str) -> list[Issue]:
    issues = _universal(content)
    # Legacy regex-based blueprint check disabled (see _validate_brd note).

    # Positive presence check against the ACTIVE domain's declared code shape.
    # A domain that declares no shape gets no warning — silence beats asserting
    # another ecosystem's alphabet. Rule key kept for API/UI stability.
    pattern = _domain_error_code_pattern()
    if pattern is not None and not pattern.search(content):
        issues.append({
            "severity": "warning",
            "rule": "missing_upi_error_codes",
            "message": ("Tech Spec has no error codes matching this domain's "
                        f"declared code shape ({pattern.pattern})."),
            "evidence": "",
        })

    # "td_bd" or "TD/BD" classification should appear in error tables
    if "error" in content.lower() and not re.search(r"\b(TD|BD)\b", content):
        issues.append({
            "severity": "warning",
            "rule": "missing_td_bd",
            "message": "Error section has no TD/BD (Technical/Business Decline) classification.",
            "evidence": "",
        })

    return issues


def _validate_xsd(content: str) -> list[Issue]:
    issues = _universal(content)

    # XSD should mention xs:schema and typed amount fields when amounts are mentioned
    if "xs:schema" not in content and "<xs:" not in content:
        issues.append({
            "severity": "warning",
            "rule": "no_xsd_content",
            "message": "Expected at least one xs:schema or xs:element block in the XSD output.",
            "evidence": "",
        })

    if re.search(r"amount", content, re.IGNORECASE) and "xs:string" in content.lower():
        # Not definitive — just a hint
        issues.append({
            "severity": "warning",
            "rule": "amount_as_string",
            "message": "Amount fields should use xs:decimal with restrictions, not xs:string.",
            "evidence": "",
        })

    # Legacy regex-based blueprint check disabled (see _validate_brd note).
    return issues


def _validate_canvas(content: str) -> list[Issue]:
    issues = _universal(content)

    # Canvas should have 10 numbered sections
    headings = re.findall(r"^## \d+\.", content, re.MULTILINE)
    if len(headings) < 10:
        issues.append({
            "severity": "warning",
            "rule": "canvas_section_count",
            "message": f"Product Canvas should have 10 numbered sections — found {len(headings)}.",
            "evidence": "",
        })

    # Legacy regex-based blueprint check disabled (see _validate_brd note).

    return issues


def _validate_product_kit(content: str) -> list[Issue]:
    # Product Kit contains many doc types; just run universal rules.
    return _universal(content)


_VALIDATORS = {
    "brd":          _validate_brd,
    "tech_spec":    _validate_tech_spec,
    "xsd":          _validate_xsd,
    "canvas":       _validate_canvas,
    "product_kit":  _validate_product_kit,
}


# ─── Public API ──────────────────────────────────────────────────────────────

def validate(content: str, doc_type: str) -> list[Issue]:
    """Validate generated content. Returns list of issues (empty = clean)."""
    if not content or not content.strip():
        return [{"severity": "error", "rule": "empty", "message": "Generated content is empty.", "evidence": ""}]

    validator = _VALIDATORS.get(doc_type.lower())
    if validator is None:
        logger.debug("No specific validator for doc_type=%s — running universal checks only", doc_type)
        return _universal(content)

    issues = validator(content)
    errors = sum(1 for i in issues if i["severity"] == "error")
    warnings = sum(1 for i in issues if i["severity"] == "warning")
    logger.info("Validator [%s]: %d errors, %d warnings", doc_type, errors, warnings)
    return issues


def summarize(issues: list[Issue]) -> dict:
    """Bundle issues into a short summary for UI / logs."""
    return {
        "error_count": sum(1 for i in issues if i["severity"] == "error"),
        "warning_count": sum(1 for i in issues if i["severity"] == "warning"),
        "has_errors": any(i["severity"] == "error" for i in issues),
        "issues": issues,
    }
