# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-document-type section blueprints — the domain-facing CONTENT.

The structural types (`Section`, `Blueprint`) moved to
`app.core.domain.types`; see that module for the schema and for why the
`app.docgen.document_guides` schema is deliberately not unified with it.

What remains here is document CONTENT guidance. It used to be written in one
ecosystem's vocabulary — FRs had to begin "NPCI shall" / "PSP Bank must",
diagrams showed "PSPs / NPCI / Issuer banks", API names were "ReqTransfer, RespTransfer" —
which meant those terms reached the model whatever pack was active. The
instructions are now role-based; the concrete participant, API and code names
come from the active pack's domain knowledge. The types it uses stay in core.

Each blueprint describes the canonical section structure for a document type.
It's used in three places:

  1. System prompts: the LLM receives an authoritative section list to
     follow (appended to the human-readable instructions).
  2. Validator: post-generation checks verify every required section
     appears (and, for BRD, the blueprint helps enforce min FR count).
  3. UI (future): we can surface the expected structure upfront so users
     see what the system is about to produce.
"""
# Re-exported so existing `from app.agents.blueprints import Blueprint` call
# sites keep working; the definitions now live in core.
from app.core.domain.types import Blueprint, Section

__all__ = ["Blueprint", "Section", "get", "section_headings",
           "required_section_headings", "format_for_prompt"]


# ──────────────────────────────────────────────────────────────────────────────
# BRD — Business Requirement Document
# ──────────────────────────────────────────────────────────────────────────────

BRD_BLUEPRINT: Blueprint = {
    "title":    "Business Requirement Document",
    "doc_type": "brd",
    "sections": [
        {"key": "executive_summary", "heading": "1. Executive Summary",
         "instructions": "2-3 paragraphs. What is being proposed, why now, what success looks like.",
         "min_paragraphs": 2, "required": True},
        {"key": "background_context", "heading": "2. Background & Context",
         "instructions": "Current state, the problem, strategic rationale. Cite research findings.",
         "min_paragraphs": 2, "required": True},
        {"key": "objectives", "heading": "3. Objectives",
         "instructions": "Bullet list of 4-6 SMART objectives specific to this domain.",
         "required": True},
        {"key": "scope", "heading": "4. Scope",
         "instructions": "Sub-sections 4.1 In Scope, 4.2 Out of Scope, 4.3 Assumptions.",
         "required": True},
        {"key": "stakeholders", "heading": "5. Stakeholders",
         "instructions": "Table: Stakeholder | Role | Interest / Concern.",
         "include_table": True, "required": True},
        {"key": "functional_requirements", "heading": "6. Functional Requirements",
         "instructions": "At least 6 numbered requirements: FR-01, FR-02, ... Each begins with 'The system shall' or '<Role> shall/must', using this domain's role names. End with Priority (High/Med/Low).",
         "numbered_list": True, "required": True},
        {"key": "nfrs", "heading": "7. Non-Functional Requirements",
         "instructions": "Sub-sections for Performance, Security, Availability, Scalability, Accessibility.",
         "required": True},
        {"key": "tech_architecture", "heading": "8. Technical Architecture Overview",
         "instructions": "High-level proposed solution, integration points with the existing infrastructure, SDK/API boundaries.",
         "min_paragraphs": 2, "required": True},
        {"key": "security_privacy", "heading": "9. Security & Data Privacy",
         "instructions": "Regulatory and data-protection compliance, data flows, encryption, fraud risk mitigations.",
         "min_paragraphs": 2, "required": True},
        {"key": "compliance", "heading": "10. Regulatory & Compliance Requirements",
         "instructions": "Specific regulator circulars, authority mandates, certification requirements.",
         "required": True},
        {"key": "implementation_plan", "heading": "11. Implementation Plan",
         "instructions": "Table: Phase | Activities | Timeline (weeks) | Dependencies.",
         "include_table": True, "required": True},
        {"key": "risks", "heading": "12. Risks & Mitigations",
         "instructions": "Table: Risk | Likelihood | Impact | Mitigation.",
         "include_table": True, "required": True},
        {"key": "success_metrics", "heading": "13. Success Metrics & KPIs",
         "instructions": "Table mirroring or expanding the Product Canvas metrics.",
         "include_table": True, "required": True},
        {"key": "approval_requirements", "heading": "14. Approval Requirements",
         "instructions": "This BRD requires sign-off from: Product Manager, Tech Lead, InfoSec Reviewer, Risk Reviewer.",
         "required": True},
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# Tech Spec
# ──────────────────────────────────────────────────────────────────────────────

TECH_SPEC_BLUEPRINT: Blueprint = {
    "title":    "Technical Specification Document",
    "doc_type": "tech_spec",
    "sections": [
        {"key": "doc_control", "heading": "1. Document Control",
         "instructions": "Table: Version | Date | Author | Changes.",
         "include_table": True, "required": True},
        {"key": "overview", "heading": "2. Overview",
         "instructions": "Purpose, audience, linked BRD reference.",
         "required": True},
        {"key": "system_context", "heading": "3. System Context & Architecture",
         "instructions": "ASCII diagram showing this domain's participants and the authority. Integration points.",
         "min_paragraphs": 2, "required": True},
        {"key": "api_specs", "heading": "4. API Specifications",
         "instructions": "Per API: request fields, response fields (with type, mandatory, dLength, description). Use the domain's exact canonical API names.",
         "include_table": True, "required": True},
        {"key": "data_model", "heading": "5. Data Model",
         "instructions": "New/changed tables, columns, indexes. Migration strategy.",
         "include_table": True, "required": True},
        {"key": "state_transitions", "heading": "6. State Transitions",
         "instructions": "Transaction states, valid transitions, terminal states.",
         "required": True},
        {"key": "security", "heading": "7. Security Design",
         "instructions": "Encryption (in-transit, at-rest), key management, cert pinning, fraud hooks.",
         "min_paragraphs": 2, "required": True},
        {"key": "performance", "heading": "8. Performance & Scalability",
         "instructions": "TPS targets, p50/p95/p99 latency SLAs, caching, indexing, load-test scope.",
         "required": True},
        {"key": "error_handling", "heading": "9. Error Handling & Resilience",
         "instructions": "Retry/back-off, circuit breakers, timeouts, DLQ. Error-code table with classification and responsible entity.",
         "include_table": True, "required": True},
        {"key": "deployment", "heading": "10. Deployment Architecture",
         "instructions": "Infra (containers/VMs), dev/UAT/prod matrix, CI/CD changes, rollback strategy.",
         "required": True},
        {"key": "testing", "heading": "11. Testing Strategy",
         "instructions": "Sub-sections 11.1 Unit, 11.2 Integration, 11.3 Performance, 11.4 Security, 11.5 Certification.",
         "required": True},
        {"key": "impl_plan", "heading": "12. Implementation Plan",
         "instructions": "Table: Sprint | Tasks | Owner Role | Dependencies | Exit Criteria.",
         "include_table": True, "required": True},
        {"key": "open_items", "heading": "13. Open Items & Risks",
         "instructions": "Table: Item | Owner | Due Date | Impact.",
         "include_table": True, "required": True},
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# Canvas — Build Framework V1.2 (10 sections, first row full-width)
# ──────────────────────────────────────────────────────────────────────────────

CANVAS_BLUEPRINT: Blueprint = {
    "title":    "Product Canvas",
    "doc_type": "canvas",
    "sections": [
        {"key": "feature", "heading": "1. Feature",
         "instructions": "One short paragraph, plain language, no jargon.",
         "min_paragraphs": 1, "required": True},
        {"key": "need", "heading": "2. Need",
         "instructions": "Bullets for: Why build, Differentiation, UX delta, Cannibalization, If not built.",
         "required": True},
        {"key": "market_view", "heading": "3. Market View",
         "instructions": "Bullets for: Ecosystem response, Ecosystem efforts, Regulatory view.",
         "required": True},
        {"key": "scalability", "heading": "4. Scalability",
         "instructions": "Bullets for: Market anchors, Impact opportunity.",
         "required": True},
        {"key": "validation", "heading": "5. Validation",
         "instructions": "Bullets for: MVP creation/operating, Data for insights.",
         "required": True},
        {"key": "product_operating", "heading": "6. Product Operating",
         "instructions": "Bullets for: 3 Success KPIs, Grievance redressal, Day 0 automation, SGF impact, FRM impact, Existing txn/infra impact.",
         "required": True},
        {"key": "product_comms", "heading": "7. Product Comms (external + internal)",
         "instructions": "Bullets for: Demo, Video, PM explanation, FAQs + LLM, Circular, Product doc.",
         "required": True},
        {"key": "pricing", "heading": "8. Pricing",
         "instructions": "Bullets for: 3-year revenue view, Market ability to pay, Market willingness.",
         "required": True},
        {"key": "risks", "heading": "9. Potential Risks",
         "instructions": "Bullets for: Fraud, Infosec, Legal, Data privacy, 2nd-order effects.",
         "required": True},
        {"key": "compliance", "heading": "10. Compliance",
         "instructions": "Bullets for: Existing guideline changes, New additions, Must-have compliances.",
         "required": True},
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# XSD
# ──────────────────────────────────────────────────────────────────────────────

XSD_BLUEPRINT: Blueprint = {
    "title":    "XSD Schema Changes",
    "doc_type": "xsd",
    "sections": [
        {"key": "changes_summary", "heading": "XSD Changes Summary",
         "instructions": "Brief description of all changes made.",
         "required": True},
        {"key": "diff_legend", "heading": "Diff Annotation Legend",
         "instructions": "Explain [NEW] / [MODIFIED] / [DEPRECATED] inline markers.",
         "required": True},
        {"key": "updated_schemas", "heading": "Updated XSD File(s)",
         "instructions": "For each affected schema: filename + complete <xs:schema> block with inline diff annotations.",
         "required": True},
        {"key": "migration_notes", "heading": "Migration Notes",
         "instructions": "Deploy steps, version bump strategy, backward-compat shim.",
         "required": True},
        {"key": "partner_impact", "heading": "Partner Impact",
         "instructions": "Which partner types need to update and re-certify.",
         "required": True},
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────────

BLUEPRINTS: dict[str, Blueprint] = {
    "brd":       BRD_BLUEPRINT,
    "tech_spec": TECH_SPEC_BLUEPRINT,
    "canvas":    CANVAS_BLUEPRINT,
    "xsd":       XSD_BLUEPRINT,
}


def get(doc_type: str) -> Blueprint | None:
    """Return the blueprint for a doc_type, or None if unknown."""
    return BLUEPRINTS.get(doc_type.lower())


def section_headings(doc_type: str) -> list[str]:
    """Return the ordered list of section headings for a doc type."""
    bp = get(doc_type)
    if not bp:
        return []
    return [s["heading"] for s in bp["sections"]]


def required_section_headings(doc_type: str) -> list[str]:
    """Return only the headings of required sections."""
    bp = get(doc_type)
    if not bp:
        return []
    return [s["heading"] for s in bp["sections"] if s.get("required", True)]


def format_for_prompt(doc_type: str) -> str:
    """Render the blueprint as a compact section list for injection into the system prompt.

    Example output:
        ## Required sections (emit in this order, using these exact headings):
          - 1. Executive Summary  (2-3 paragraphs)
          - 2. Background & Context  (2-3 paragraphs)
          ...
    """
    bp = get(doc_type)
    if not bp:
        return ""
    lines = ["## Required sections (emit in this order, using these exact headings):"]
    for s in bp["sections"]:
        req_marker = "" if s.get("required", True) else "  [optional]"
        hints: list[str] = []
        if s.get("min_paragraphs"):
            hints.append(f"≥{s['min_paragraphs']} paragraphs")
        if s.get("include_table"):
            hints.append("must contain a table")
        if s.get("numbered_list"):
            hints.append("must contain FR-## numbered list")
        hint_text = f"  ({'; '.join(hints)})" if hints else ""
        lines.append(f"  - {s['heading']}{hint_text}{req_marker}")
        if s.get("instructions"):
            lines.append(f"      → {s['instructions']}")
    return "\n".join(lines)
