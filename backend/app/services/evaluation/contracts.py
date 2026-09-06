# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""First-wave checkpoint contract definitions.

Each contract answers: what artifacts are required, what rubric dimensions
matter, which hard-fail codes apply, and what the initial policy mode is.

Rules:
- All contracts start advisory until the team has calibration evidence.
- Hard-fail codes must exist in hard_fail_catalog.py.
- Rubric dimension IDs must be stable (used as keys in verdict scores dict).
- Do not add runtime logic here — contracts are pure data.
"""
from app.core.domain.contract import participants_of
from app.core.domain.registry import get_active_pack, prompt_block

from .checkpoints import CheckpointId, PolicyMode
from .schemas import CheckpointContract, RubricDimension

RUBRIC_VERSION = "eval-harness.phase0.v1"

# ── Domain nouns for rubric PROSE ────────────────────────────────────────────
# Dimension IDs and hard-fail codes are wire/stable values and never change per
# pack; the criteria text the judge and the operator read is domain wording, so
# it comes from the active pack (import-time — contracts are module constants).
_AUTHORITY = prompt_block("authority", "the ecosystem authority")
_DOMAIN = prompt_block("domain_name", "").strip()
_DOMAIN_ADJ = f"{_DOMAIN} " if _DOMAIN else ""
_EVIDENCE_SOURCES = prompt_block("evidence_sources", "authoritative")
_STAKEHOLDERS = ", ".join(
    p.label for p in participants_of(get_active_pack())
) or "every declared participant role"

# ── brd_to_tech_spec ─────────────────────────────────────────────────────────

BRD_TO_TECH_SPEC = CheckpointContract(
    checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
    display_name="BRD → Tech Spec",
    description=(
        "Confirms the Tech Spec implements all approved BRD requirements. "
        "Catches drift, missing coverage, and weak testability before engineering starts."
    ),
    from_stage="brd",
    to_stage="tech_spec",
    policy_mode=PolicyMode.ADVISORY,
    rubric_version=RUBRIC_VERSION,
    required_source_artifacts=["brd_document"],
    required_target_artifacts=["tech_spec_document"],
    rubric_dimensions=[
        RubricDimension(
            id="requirement_coverage",
            name="Requirement Coverage",
            description="Every BRD functional requirement has a corresponding technical design.",
            weight=0.35,
            minimum_score=0.7,
        ),
        RubricDimension(
            id="cross_document_consistency",
            name="Cross-Document Consistency",
            description="No field, rule, or flow contradicts the approved BRD.",
            weight=0.25,
            minimum_score=0.75,
        ),
        RubricDimension(
            id="technical_completeness",
            name="Technical Completeness",
            description="API contracts, state machines, error codes, and data models are present.",
            weight=0.25,
            minimum_score=0.65,
        ),
        RubricDimension(
            id="testability",
            name="Testability",
            description="Requirements have explicit acceptance criteria or verifiable conditions.",
            weight=0.15,
            minimum_score=0.6,
        ),
    ],
    deterministic_checks=[
        "check_mandatory_sections_present",
        "check_no_placeholders",
        "check_fr_numbering_pattern",
        "check_error_code_table_present",
        # Phase A Excellence — Slice 3: cross-artifact grounding
        "check_tech_spec_covers_all_brd_frs",
        "check_domain_error_codes_are_valid",
        "check_no_http_codes_as_domain_errors",
        # API Registry — registry-covered wire APIs must use registry-rendered specs
        "check_tsd_api_specs_registry_backed",
    ],
    hard_fail_codes=[
        "MISSING_REQUIRED_ARTIFACT",
        "EMPTY_OR_PLACEHOLDER_CONTENT",
        "MISSING_MANDATORY_SECTION",
        "UNMAPPED_REQUIREMENT",
        "CONTRADICTS_APPROVED_SOURCE",
        "INVALID_UPI_ERROR_PATTERN",
    ],
    warn_codes=[],
    retry_allowed=True,
    override_allowed_roles=["product_owner", "tech_lead"],
)

# ── tech_spec_to_xsd ─────────────────────────────────────────────────────────

TECH_SPEC_TO_XSD = CheckpointContract(
    checkpoint_id=CheckpointId.TECH_SPEC_TO_XSD,
    display_name="Tech Spec → XSD Assessment/Generation",
    description=(
        "Confirms the XSD assessment decision is consistent with the Tech Spec, "
        "and that any generated schema correctly reflects the spec's fields and messages."
    ),
    from_stage="tech_spec",
    to_stage="xsd",
    policy_mode=PolicyMode.ADVISORY,
    rubric_version=RUBRIC_VERSION,
    required_source_artifacts=["tech_spec_document"],
    required_target_artifacts=["xsd_assessment_decision"],
    rubric_dimensions=[
        RubricDimension(
            id="decision_consistency",
            name="Decision Consistency",
            description="XSD REQUIRED/NOT_REQUIRED decision matches Tech Spec changes.",
            weight=0.4,
            minimum_score=0.8,
        ),
        RubricDimension(
            id="schema_completeness",
            name="Schema Completeness",
            description="Generated XSD (if required) covers all new fields and messages in the spec.",
            weight=0.35,
            minimum_score=0.7,
        ),
        RubricDimension(
            id="partner_integration_readiness",
            name="Partner Integration Readiness",
            description="Schema changes are annotated with backward compatibility notes.",
            weight=0.25,
            minimum_score=0.6,
        ),
    ],
    deterministic_checks=[
        "check_xsd_decision_field_present",
        "check_generated_xsd_if_required",
        "check_no_placeholders",
    ],
    hard_fail_codes=[
        "MISSING_REQUIRED_ARTIFACT",
        "XSD_DECISION_MISMATCH",
        "EMPTY_OR_PLACEHOLDER_CONTENT",
    ],
    warn_codes=[],
    retry_allowed=False,
    override_allowed_roles=["product_owner", "tech_lead"],
)

# ── product_kit_to_phase_c_communication ─────────────────────────────────────

PRODUCT_KIT_TO_PHASE_C = CheckpointContract(
    checkpoint_id=CheckpointId.PRODUCT_KIT_TO_PHASE_C,
    display_name="Product Kit → Phase C Communication",
    description=(
        "Preflight check before the product kit is sent to partners via A2A. "
        "Catches incomplete bundles, missing manifest entries, and internal placeholders "
        "before partners receive them."
    ),
    from_stage="product_kit",
    to_stage="phase_c_communication",
    policy_mode=PolicyMode.ADVISORY,
    rubric_version=RUBRIC_VERSION,
    required_source_artifacts=["product_kit_manifest", "product_kit_documents"],
    required_target_artifacts=["a2a_communication_payload"],
    rubric_dimensions=[
        RubricDimension(
            id="bundle_completeness",
            name="Bundle Completeness",
            description="All documents declared in the manifest are present and non-empty.",
            weight=0.4,
            minimum_score=0.9,
        ),
        RubricDimension(
            id="external_readiness",
            name="External Readiness",
            description="No internal notes, placeholders, or draft markers in partner-facing content.",
            weight=0.35,
            minimum_score=0.85,
        ),
        RubricDimension(
            id="alignment_with_approved_docs",
            name="Alignment With Approved Docs",
            description="Kit content is consistent with approved BRD and Tech Spec summaries.",
            weight=0.25,
            minimum_score=0.7,
        ),
    ],
    deterministic_checks=[
        "check_manifest_all_docs_present",
        "check_no_placeholders",
        "check_no_internal_markers",
        "check_payload_not_empty",
    ],
    hard_fail_codes=[
        "MISSING_REQUIRED_ARTIFACT",
        "INCOMPLETE_PRODUCT_KIT",
        "EMPTY_OR_PLACEHOLDER_CONTENT",
        "CONTRADICTS_APPROVED_SOURCE",
    ],
    warn_codes=[],
    retry_allowed=False,
    override_allowed_roles=["product_owner"],
)

# ── phase_c_query_to_po_response ─────────────────────────────────────────────

PHASE_C_QUERY_TO_PO_RESPONSE = CheckpointContract(
    checkpoint_id=CheckpointId.PHASE_C_QUERY_TO_PO_RESPONSE,
    display_name="Partner Query → AI Draft → PO Response",
    description=(
        "Confirms the AI-drafted partner response is grounded in approved artifacts "
        "and does not make unsupported promises before the PO reviews it."
    ),
    from_stage="partner_query",
    to_stage="po_draft_response",
    policy_mode=PolicyMode.ADVISORY,
    rubric_version=RUBRIC_VERSION,
    required_source_artifacts=["partner_query", "approved_brd_summary", "approved_tsd_summary"],
    required_target_artifacts=["draft_response"],
    rubric_dimensions=[
        RubricDimension(
            id="query_relevance",
            name="Query Relevance",
            description="The response directly addresses what was asked.",
            weight=0.25,
            minimum_score=0.7,
        ),
        RubricDimension(
            id="grounding",
            name="Grounding",
            description="Every claim in the response is traceable to an approved source artifact.",
            weight=0.4,
            minimum_score=0.75,
        ),
        RubricDimension(
            id="policy_safety",
            name="Policy Safety",
            description="Response makes no commitments not in approved docs and raises no compliance risk.",
            weight=0.35,
            minimum_score=0.8,
        ),
    ],
    deterministic_checks=[
        "check_response_not_empty",
        "check_no_unapproved_commitments_pattern",
    ],
    hard_fail_codes=[
        "MISSING_REQUIRED_ARTIFACT",
        "UNSAFE_PARTNER_RESPONSE",
        "EMPTY_OR_PLACEHOLDER_CONTENT",
    ],
    warn_codes=[],
    retry_allowed=False,
    override_allowed_roles=["product_owner"],
)


# ── Phase 7 — full Phase A gate coverage ────────────────────────────────────
#
# The five contracts below add a checkpoint to every remaining Phase A
# transition. All start in ADVISORY so verdicts accumulate without blocking
# any change request. Promotion to soft_gate / hard_gate is an operational
# action via the Admin > Eval Policy page, not a code change.
#
# Naming convention (kept consistent with the first wave): a checkpoint
# named X_TO_Y evaluates the artifact produced going X -> Y and is enforced
# as the user leaves Y. So `prompt_to_research` evaluates the research
# summary and gates research -> canvas.

INITIAL_TO_PROMPT_ENHANCED = CheckpointContract(
    checkpoint_id=CheckpointId.INITIAL_TO_PROMPT_ENHANCED,
    display_name="Initial Prompt → Enhanced Prompt",
    description=(
        "Confirms the enhanced prompt is substantive enough for the research stage. "
        "Catches empty, placeholder, or trivially-short prompts before they propagate."
    ),
    from_stage="prompt_enhancement",
    to_stage="research",
    policy_mode=PolicyMode.ADVISORY,
    rubric_version=RUBRIC_VERSION,
    required_source_artifacts=["initial_prompt"],
    required_target_artifacts=["enhanced_prompt"],
    rubric_dimensions=[
        RubricDimension(
            id="clarity",
            name="Clarity",
            description="Enhanced prompt clearly states the change goal in unambiguous language.",
            weight=0.4,
            minimum_score=0.65,
        ),
        RubricDimension(
            id="completeness",
            name="Completeness",
            description="Enhanced prompt captures scope, target users, and any constraints implied by the initial prompt.",
            weight=0.4,
            minimum_score=0.6,
        ),
        RubricDimension(
            id="actionability",
            name="Actionability",
            description="Enhanced prompt is concrete enough for a researcher to start gathering evidence.",
            weight=0.2,
            minimum_score=0.6,
        ),
    ],
    deterministic_checks=[
        "check_prompt_not_empty",
        "check_prompt_min_length",
        "check_no_placeholders",
    ],
    hard_fail_codes=[
        "MISSING_REQUIRED_ARTIFACT",
        "EMPTY_OR_PLACEHOLDER_CONTENT",
        "PROMPT_TOO_SHORT",
    ],
    warn_codes=[],
    retry_allowed=True,
    override_allowed_roles=["product_owner"],
)

PROMPT_TO_RESEARCH = CheckpointContract(
    checkpoint_id=CheckpointId.PROMPT_TO_RESEARCH,
    display_name="Enhanced Prompt → Research Summary",
    description=(
        "Confirms the research summary covers the themes raised by the enhanced prompt "
        "and is grounded in cited sources. Catches drift, ungrounded claims, and missing "
        "evidence before the product canvas is built on top of it."
    ),
    from_stage="research",
    to_stage="canvas",
    policy_mode=PolicyMode.ADVISORY,
    rubric_version=RUBRIC_VERSION,
    required_source_artifacts=["enhanced_prompt"],
    required_target_artifacts=["research_summary"],
    rubric_dimensions=[
        RubricDimension(
            id="coverage",
            name="Coverage",
            description="Every theme raised by the enhanced prompt has a corresponding section or finding in the summary.",
            weight=0.35,
            minimum_score=0.7,
        ),
        RubricDimension(
            id="source_grounding",
            name="Source Grounding",
            description="Non-trivial claims in the summary cite an explicit source.",
            weight=0.30,
            minimum_score=0.7,
        ),
        RubricDimension(
            id="source_quality",
            name="Source Quality",
            description=(f"Cited sources are {_EVIDENCE_SOURCES} authoritative "
                         "or first-party documentation rather than ad-hoc commentary."),
            weight=0.20,
            minimum_score=0.6,
        ),
        RubricDimension(
            id="actionability",
            name="Actionability",
            description="Summary surfaces the open decisions the product owner needs to make next.",
            weight=0.15,
            minimum_score=0.6,
        ),
    ],
    deterministic_checks=[
        "check_research_summary_not_empty",
        "check_at_least_one_source",
        "check_no_placeholders",
    ],
    hard_fail_codes=[
        "MISSING_REQUIRED_ARTIFACT",
        "EMPTY_OR_PLACEHOLDER_CONTENT",
        "NO_SOURCES_FOUND",
    ],
    warn_codes=[],
    retry_allowed=True,
    override_allowed_roles=["product_owner"],
)

RESEARCH_TO_CANVAS = CheckpointContract(
    checkpoint_id=CheckpointId.RESEARCH_TO_CANVAS,
    display_name="Research Summary → Product Canvas",
    description=(
        "Confirms the product canvas turns the research summary into an explicit "
        "problem statement, scope, and stakeholder map. Catches vague or contradictory "
        "canvases before clarification questions are derived."
    ),
    from_stage="canvas",
    to_stage="clarification",
    policy_mode=PolicyMode.ADVISORY,
    rubric_version=RUBRIC_VERSION,
    required_source_artifacts=["research_summary"],
    required_target_artifacts=["product_canvas"],
    rubric_dimensions=[
        RubricDimension(
            id="problem_clarity",
            name="Problem Clarity",
            description="Canvas states the problem in a single unambiguous paragraph.",
            weight=0.30,
            minimum_score=0.7,
        ),
        RubricDimension(
            id="scope_definition",
            name="Scope Definition",
            description="Canvas lists explicit in-scope and out-of-scope items.",
            weight=0.25,
            minimum_score=0.7,
        ),
        RubricDimension(
            id="stakeholder_coverage",
            name="Stakeholder Coverage",
            description=(f"Canvas names every stakeholder group affected by the "
                         f"change ({_STAKEHOLDERS})."),
            weight=0.20,
            minimum_score=0.6,
        ),
        RubricDimension(
            id="alignment_with_research",
            name="Alignment with Research",
            description="No claim in the canvas contradicts the research summary.",
            weight=0.25,
            minimum_score=0.7,
        ),
    ],
    deterministic_checks=[
        "check_canvas_has_required_sections",
        "check_no_placeholders",
    ],
    hard_fail_codes=[
        "MISSING_REQUIRED_ARTIFACT",
        "MISSING_MANDATORY_SECTION",
        "EMPTY_OR_PLACEHOLDER_CONTENT",
        "CONTRADICTS_APPROVED_SOURCE",
    ],
    warn_codes=[],
    retry_allowed=True,
    override_allowed_roles=["product_owner"],
)

CANVAS_TO_CLARIFICATION = CheckpointContract(
    checkpoint_id=CheckpointId.CANVAS_TO_CLARIFICATION,
    display_name="Product Canvas → Clarification Q&A",
    description=(
        "Confirms every canvas-derived question has been answered or explicitly deferred "
        "with rationale before the BRD is drafted. Catches unresolved scope ambiguity "
        "that would otherwise leak into the BRD."
    ),
    from_stage="clarification",
    to_stage="brd",
    policy_mode=PolicyMode.ADVISORY,
    rubric_version=RUBRIC_VERSION,
    required_source_artifacts=["product_canvas"],
    required_target_artifacts=["clarification_thread"],
    rubric_dimensions=[
        RubricDimension(
            id="open_question_resolution",
            name="Open Question Resolution",
            description="Every canvas-derived question has either an answer or an explicit 'deferred' status with rationale.",
            weight=0.40,
            minimum_score=0.8,
        ),
        RubricDimension(
            id="answer_specificity",
            name="Answer Specificity",
            description="Answers are concrete; 'TBD by team' or empty answers are not acceptable.",
            weight=0.30,
            minimum_score=0.7,
        ),
        RubricDimension(
            id="traceability",
            name="Traceability",
            description="Each answer ties back to a canvas item, a stakeholder, or a research source.",
            weight=0.30,
            minimum_score=0.7,
        ),
    ],
    deterministic_checks=[
        "check_no_unanswered_canvas_questions",
        "check_no_placeholders",
    ],
    hard_fail_codes=[
        "MISSING_REQUIRED_ARTIFACT",
        "EMPTY_OR_PLACEHOLDER_CONTENT",
        "UNANSWERED_CRITICAL_QUESTION",
    ],
    warn_codes=[],
    retry_allowed=False,
    override_allowed_roles=["product_owner"],
)

CLARIFICATION_TO_BRD = CheckpointContract(
    checkpoint_id=CheckpointId.CLARIFICATION_TO_BRD,
    display_name="Clarification Q&A → BRD",
    description=(
        "Confirms the BRD reflects the resolved clarification answers and uses the "
        f"{_DOMAIN_ADJ}domain conventions the Tech Spec will rely on. Catches missing FRs, "
        "missing error-code tables, and BRD-clarification drift before engineering "
        "starts on the Tech Spec."
    ),
    from_stage="brd",
    to_stage="tech_spec",
    policy_mode=PolicyMode.ADVISORY,
    rubric_version=RUBRIC_VERSION,
    required_source_artifacts=["product_canvas", "clarification_thread"],
    required_target_artifacts=["brd_document"],
    rubric_dimensions=[
        RubricDimension(
            id="requirement_completeness",
            name="Requirement Completeness",
            description="Every resolved clarification answer is expressed as an FR-## in the BRD.",
            weight=0.30,
            minimum_score=0.75,
        ),
        RubricDimension(
            id="testability",
            name="Testability",
            description="Each FR has an acceptance criterion or measurable condition.",
            weight=0.25,
            minimum_score=0.65,
        ),
        RubricDimension(
            id="cross_doc_consistency",
            name="Cross-Document Consistency",
            description="No claim in the BRD contradicts the canvas or clarification thread.",
            weight=0.25,
            minimum_score=0.75,
        ),
        RubricDimension(
            # ID is a stable verdict-score key — only the display prose is
            # domain wording.
            id="upi_domain_correctness",
            name=f"{_DOMAIN_ADJ}Domain Correctness".strip(),
            description=(f"Error codes, message names, and flows follow "
                         f"{_AUTHORITY} conventions rather than generic "
                         "HTTP/web patterns."),
            weight=0.20,
            minimum_score=0.7,
        ),
    ],
    deterministic_checks=[
        "check_mandatory_sections_present",
        "check_fr_numbering_pattern",
        "check_error_code_table_present",
        "check_no_placeholders",
    ],
    hard_fail_codes=[
        "MISSING_REQUIRED_ARTIFACT",
        "EMPTY_OR_PLACEHOLDER_CONTENT",
        "MISSING_MANDATORY_SECTION",
        "UNMAPPED_REQUIREMENT",
        "INVALID_UPI_ERROR_PATTERN",
    ],
    warn_codes=[],
    retry_allowed=True,
    override_allowed_roles=["product_owner", "tech_lead"],
)


# ── Registry ─────────────────────────────────────────────────────────────────

_ALL_CONTRACTS: list[CheckpointContract] = [
    BRD_TO_TECH_SPEC,
    TECH_SPEC_TO_XSD,
    PRODUCT_KIT_TO_PHASE_C,
    PHASE_C_QUERY_TO_PO_RESPONSE,
    # Phase 7 additions
    INITIAL_TO_PROMPT_ENHANCED,
    PROMPT_TO_RESEARCH,
    RESEARCH_TO_CANVAS,
    CANVAS_TO_CLARIFICATION,
    CLARIFICATION_TO_BRD,
]

_CONTRACT_REGISTRY: dict[CheckpointId, CheckpointContract] = {}
for _c in _ALL_CONTRACTS:
    if _c.checkpoint_id in _CONTRACT_REGISTRY:
        raise ValueError(f"Duplicate contract registered for checkpoint: {_c.checkpoint_id}")
    _CONTRACT_REGISTRY[_c.checkpoint_id] = _c


def get_contract(checkpoint_id: CheckpointId | str) -> CheckpointContract:
    """Return the contract for a checkpoint. Raises KeyError for unknown IDs."""
    try:
        key = CheckpointId(checkpoint_id) if isinstance(checkpoint_id, str) else checkpoint_id
    except ValueError:
        raise KeyError(f"No contract registered for checkpoint: '{checkpoint_id}'")
    if key not in _CONTRACT_REGISTRY:
        raise KeyError(f"No contract registered for checkpoint: '{checkpoint_id}'")
    return _CONTRACT_REGISTRY[key]


def all_contracts() -> list[CheckpointContract]:
    return list(_CONTRACT_REGISTRY.values())


def all_checkpoint_ids() -> list[CheckpointId]:
    return list(_CONTRACT_REGISTRY.keys())
