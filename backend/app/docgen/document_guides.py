# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Document anatomy guides and blueprint helpers."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.core.domain.registry import prompt_block as _prompt_block

# ── Domain vocabulary for blueprint headings/instructions ────────────────────
# Resolved from the active pack at import time (the registry's documented
# pattern — see app/core/domain/registry.py). Section KEYS stay fixed; only the
# domain-facing labels and example lists vary per pack. Every consumer of these
# sections (docgen/document_validator.py, the tier filter below, the writer
# pipeline) keys on `section_key`, and the eval mandatory-sections check and
# the excel-engine TSD splitter (tests/api/test_engine_scope_context.py) pin
# OTHER headings, which are deliberately left untouched here.
#
# The "servicing" role is the third Envisaged-Changes stakeholder: the party
# that authenticates/fulfils what the initiating participant requests — UPI's
# Issuer Bank, a library network's Lending Library.
_SERVICING_ROLE = _prompt_block(
    "docgen_envisaged_changes_role_label", "Servicing Participant"
)
_SERVICING_ROLE_RESPONSIBILITIES = _prompt_block(
    "docgen_envisaged_changes_role_responsibilities",
    "validating and fulfilling the requests routed to it",
)
_SERVICING_TXN_PHRASE = _prompt_block(
    "docgen_servicing_transaction_phrase", "the transactions it services"
)
_RISK_SCENARIO_EXAMPLES = _prompt_block(
    "docgen_risk_scenario_examples",
    "collusion, replay/duplicate, unauthorised operations, social engineering, velocity abuse",
)
_AGGREGATE_EXAMPLES = _prompt_block(
    "docgen_business_aggregate_examples",
    "the central business object this feature creates or modifies",
)
_SLA_FLOW_EXAMPLES = _prompt_block(
    "docgen_sla_flow_examples",
    "enrollment, transaction processing, status lookup, dispute lookup",
)


CIRCULAR_BLUEPRINT = {
    "title": "Circular",
    "subtitle": "Formal regulatory directive",
    "doc_type": "Circular",
    "tone": "Formal, directive, terse",
    "audience": "Member and participant organisations",
    "include_cover_page": False,
    "include_toc": False,
    "sections": [
        {
            "section_key": "letterhead_reference",
            "heading": "Letterhead & Reference Block",
            "level": 1,
            "render_style": "circular_reference",
            "content_instructions": (
                "Prepare the issuing organization header details. Include only the minimum official "
                "identity elements needed for traceability."
            ),
            "prompt_instruction": (
                "Include the issuing organization name, a formal circular reference number in the format "
                "[ORG]/[DEPT]/OC No. [NNN]/[YYYY-YYYY], and the issue date flush-right. "
                "This header is mandatory and must appear before all body text."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "addressee_line",
            "heading": "Addressee Line",
            "level": 1,
            "render_style": "circular_addressee",
            "content_instructions": "State the complete recipient categories bound by this circular.",
            "prompt_instruction": (
                "State the recipient categories precisely and comprehensively. Bold the addressee list. "
                "Use inclusive language ('All X, Y and Z')."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "subject_line",
            "heading": "Subject Line",
            "level": 1,
            "render_style": "circular_subject",
            "content_instructions": (
                "Write a one-line subject that clearly names the directive, the feature/artifact, and the scope."
            ),
            "prompt_instruction": (
                "Write a subject line that names the action, the specific feature or artifact, and the system scope. "
                "Under 20 words. Formal sentence case."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "context_paragraph",
            "heading": "Context Paragraph",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Explain the current state, the ecosystem gap, and why the issuer is issuing this directive. "
                "Keep it factual and vendor-neutral."
            ),
            "prompt_instruction": (
                "Describe the current state, identify the limitation or opportunity, and briefly state what the "
                "issuer has decided to do in response. Single paragraph, 3-5 sentences."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "decision_scope",
            "heading": "Decision & Scope Statement",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "State the decision declaratively and name the technical artifacts, modules, schemas, or APIs affected."
            ),
            "prompt_instruction": (
                "Start with '[Organization] has decided to...'. Then name the specific artifacts being changed so "
                "engineering teams can identify scope immediately."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "participant_obligations",
            "heading": "Participant Impact & Obligations",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "List the participant categories and their concrete implementation or compliance obligations."
            ),
            "prompt_instruction": (
                "For each affected participant category, state specific obligations. Use 'must' for mandatory items "
                "and 'are advised to' for recommended items."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "dissemination_instruction",
            "heading": "Dissemination Instruction",
            "level": 1,
            "render_style": "circular_dissemination",
            "content_instructions": "Include the standard one-line internal dissemination instruction.",
            "prompt_instruction": (
                "Include the standard dissemination instruction: "
                "'Please disseminate the information contained herein to the officials concerned.'"
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "signature_block",
            "heading": "Signature Block",
            "level": 1,
            "render_style": "circular_signature",
            "content_instructions": "Render the approving authority block.",
            "prompt_instruction": (
                "Close with 'Yours Sincerely,' followed by 'SD/-', then the authorizing official's name, designation, "
                "and department on separate lines."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
    ],
}

PRODUCT_NOTE_BLUEPRINT = {
    "title": "Product Note",
    "subtitle": "Comprehensive product reference",
    "doc_type": "Product Note",
    "tone": "Explanatory, thorough, structured",
    "audience": "Participant organisations, solution providers, internal product & tech teams",
    "include_cover_page": True,
    "include_toc": True,
    "sections": [
        {
            "section_key": "document_overview",
            "heading": "Document Overview",
            "level": 1,
            "render_style": "body",
            "content_instructions": "Write purpose, audience, and scope as three short sub-sections.",
            "prompt_instruction": "Write Purpose, Audience, and Scope sub-sections in short prose. No bullets unless needed for stakeholder names.",
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "background",
            "heading": "Background",
            "level": 1,
            "render_style": "body",
            "content_instructions": "Cover current state, limitations/challenges, and why this solution in a product-oriented way.",
            "prompt_instruction": "Use prose for current state and concise bullets for limitations and benefits.",
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "feature_description",
            "heading": "Product Overview - Feature Description",
            "level": 1,
            "render_style": "body",
            "content_instructions": "Explain what the feature is, what it enables, and the security/privacy principles in plain language.",
            "prompt_instruction": "Use 2-3 clear paragraphs. Avoid XML tags or API payload detail.",
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "product_construct_setting",
            "heading": "Product Construct: Setting / Enrollment",
            "level": 1,
            "render_style": "body",
            "content_instructions": "Describe UI placement, indicative user journey, technical flow, and roles/responsibilities for the enrollment/setting process. Cover: how a user initiates and completes enrollment, what each participant does, and how consent is captured or verified.",
            "prompt_instruction": "Structure as UI Placement, Indicative Journey, and Roles & Responsibilities. Include a table with Step | Activity | Responsible. Derive the specific steps entirely from the input.",
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": True,
            "diagram_type": "activity",
            "diagram_description": "Enrollment and setting journey across key participants",
        },
        {
            "section_key": "product_construct_transaction",
            "heading": "Product Construct: Transaction Flow",
            "level": 1,
            "render_style": "body",
            "content_instructions": "Describe the transaction scope, high-level system changes, supported modes, end-to-end transaction journey, and roles/responsibilities table for the payment execution flow.",
            "prompt_instruction": "Structure as Scope, High-level Changes, Modes, and Roles & Responsibilities. Include a table with Step | Activity | Responsible. Derive flows from the input — names of participants and steps must come from the actual document content.",
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": True,
            "diagram_type": "sequence",
            "diagram_description": "Transaction flow across key participants and system components",
        },
        {
            "section_key": "policy_rules",
            "heading": "Key Considerations & Policy Rules",
            "level": 1,
            "render_style": "body",
            "content_instructions": "List optionality, consent scope, storage, key rotation, and disablement scenarios as actionable rules.",
            "prompt_instruction": "Use grouped bullets. Each bullet should state one explicit policy and its trigger or implication.",
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "salient_points",
            "heading": "Other Salient Points",
            "level": 1,
            "render_style": "body",
            "content_instructions": "Add numbered standalone product rules or UX constraints not already covered.",
            "prompt_instruction": "Use a numbered list of 1-2 sentence items only if such rules exist.",
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "dispute_management",
            "heading": "Dispute Management",
            "level": 1,
            "render_style": "body",
            "content_instructions": "State whether dispute management changes due to this feature.",
            "prompt_instruction": "If unchanged, explicitly say there is no change in dispute management.",
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "testing_certification",
            "heading": "Testing, Certification & Audits",
            "level": 1,
            "render_style": "body",
            "content_instructions": "Describe test environments, mandatory scenarios, certification steps, and audit expectations.",
            "prompt_instruction": "Cover enrollment, transaction, fallback, and disablement scenarios and identify approving authority.",
            "include_table": True,
            "table_fallback_profile": "test_matrix",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "annexure_prechecks",
            "heading": "Annexure: Pre-Checks",
            "level": 1,
            "render_style": "body",
            "content_instructions": "Define pre-checks required before enrollment/enabling and at transaction time, with failure consequences for each. Derive the specific checks from the input.",
            "prompt_instruction": "Split into Before Enrollment/Enabling and At Transaction Time. Each item should name the check, which participant performs it, and the consequence if it fails.",
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
    ],
}

BRD_BLUEPRINT = {
    "title": "Business Requirements Document",
    "subtitle": "Business and implementation requirements",
    "doc_type": "BRD",
    "tone": "Structured, accountability-focused, complete",
    "audience": "Engineering & product leads, participant organisations, authority internal teams",
    "include_cover_page": True,
    "include_toc": True,
    "sections": [
        # ── Section 1: Background ──────────────────────────────────────────
        {
            "section_key": "background_current_state",
            "heading": "i. Current State",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "Describe how the relevant flow works TODAY before this change. "
                "Explain the existing mechanism, who the participants are, and how the current process operates. "
                "Name the canonical APIs in use today, using only names present in the supplied domain knowledge or context. Minimum 2 paragraphs."
            ),
            "prompt_instruction": (
                "Write 2 paragraphs: (1) the existing process and participants, "
                "(2) how the current system handles the relevant scenario."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "background_limitations",
            "heading": "ii. Limitations / Challenges",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "Explain why the current state is insufficient. "
                "Cover security gaps, friction points, scalability concerns, or regulatory gaps. "
                "Use bullet points or numbered items for each limitation. Minimum 1 paragraph + 4 bullet points."
            ),
            "prompt_instruction": (
                "Write a short intro paragraph then list ≥ 4 concrete limitations as bullet points. "
                "Each bullet names the limitation, the stakeholder it affects, and the consequence."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "background_rationale",
            "heading": "iii. Why the Proposed Change",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "State the business and technical justification for this change. "
                "Reference regulator guidelines, security improvements, user experience benefits, "
                "or ecosystem mandates where applicable. Minimum 2 paragraphs."
            ),
            "prompt_instruction": (
                "Write 2 paragraphs: (1) the business case and regulatory context, "
                "(2) the expected benefits and why this approach was chosen over alternatives."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Section 2: Product Overview ───────────────────────────────────
        {
            "section_key": "product_description",
            "heading": "i. Product Description",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "Provide a high-level description of what this feature/product does and what business outcome it achieves. "
                "Name the affected participants and the scope of changes. "
                "Include a 4-column Architecture & Ownership Boundaries table [Layer | Owns (does) | Boundary (does NOT do) | APIs Touched]. Minimum 2 paragraphs."
            ),
            "prompt_instruction": (
                "Write 2-3 paragraphs describing: what the feature does, who it affects, "
                "and what problem it solves from a business perspective. No API details."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "product_construct_setting",
            "heading": "ii. Product Construct: Setting / Enrollment",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "Describe the end-to-end enrollment or setting process. "
                "Cover: UI placement, consent capture, indicative user journey step by step, "
                "and a Roles & Responsibilities table with columns [Step, Activity, Responsible]. "
                "Rows: Pre-Check → Step 1..N → Post Response. Minimum 1 paragraph + R&R table."
            ),
            "prompt_instruction": (
                "Write a brief overview paragraph then produce a Step/Activity/Responsible table. "
                "Each step names the acting entity, using the participant names for THIS domain. "
                "Reference each API by canonical name and BUSINESS PURPOSE only — NO XML/payloads or wire samples (wire detail belongs in the TSD). Never invent placeholder API names."
            ),
            "include_table": True,
            "include_diagram": True,
            "diagram_type": "activity",
            "diagram_description": (
                "Enrollment / setting journey across every participant in the flow"
            ),
            "table_fallback_profile": "process_steps",
        },
        {
            "section_key": "product_construct_transaction",
            "heading": "iii. Product Construct: Transaction Flow",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "Describe the end-to-end transaction flow after enrollment. "
                "Cover: triggering conditions, authentication path, the fulfilment sequence "
                "across participants, "
                "and a Roles & Responsibilities table with columns [Step, Activity, Responsible]. "
                "Rows: Pre-Check → Step 1..N → Post Response. Minimum 1 paragraph + R&R table."
            ),
            "prompt_instruction": (
                "Write a brief overview paragraph then produce a Step/Activity/Responsible table. "
                "Name the entity performing each step. Reference APIs by canonical name + business purpose only — NO XML/payloads (wire detail belongs in the TSD). Describe failure handling as customer-facing error CATEGORIES (auth-failure, business-decline, technical-failure), NOT wire codes or their classification."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": True,
            "diagram_type": "sequence",
            "diagram_description": (
                "End-to-end flow across every participant, in order"
            ),
        },
        # ── Section 3: Other Salient Points ──────────────────────────────
        {
            "section_key": "salient_points",
            "heading": "Other Salient Points",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "List edge cases, constraints, opt-in/opt-out rules, backward-compatibility requirements, "
                "and any operational rules not covered in the Product Construct sections. "
                "Minimum 6 numbered points. Each point is one self-contained business rule or constraint."
            ),
            "prompt_instruction": (
                "Use numbered_items. Each item is a standalone, actionable business rule. "
                "Cover: optionality, device constraints, retry logic, consent withdrawal, "
                "backward compatibility, and any exception handling at the business level."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Section 4: Dispute Management ────────────────────────────────
        {
            "section_key": "dispute_management",
            "heading": "Dispute Management",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "State explicitly whether dispute management changes due to this feature. "
                "If unchanged, say so clearly and reference the domain's standard dispute framework. "
                "If changed, describe the new liability assignment, SLA, and escalation path. "
                "Minimum 2 paragraphs."
            ),
            "prompt_instruction": (
                "Start with a declarative statement on whether the dispute process changes. "
                "Then describe liability, SLA, and escalation path explicitly."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Section 5: Functional Requirements ───────────────────────────
        {
            "section_key": "functional_requirements",
            "heading": "Functional Requirements",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "List all functional requirements for this feature as a table. "
                "Table columns: [ID, Requirement, Priority]. "
                "IDs: FR-01, FR-02, ... Minimum 8 functional requirement rows. "
                "Each requirement is one testable statement. "
                "Write a 1-paragraph intro before the table."
            ),
            "prompt_instruction": (
                "Write a short intro paragraph, then produce a table with columns "
                "[ID, Requirement, Priority]. "
                "IDs start at FR-01. Priority values: High / Medium / Low. "
                "Each requirement is a single testable statement starting with 'The system shall'."
            ),
            "include_table": True,
            "table_fallback_profile": "requirement_table",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Section 6: Envisaged Changes — Authority ─────────────────────
        {
            "section_key": "changes_npci_setting",
            "heading": "A. Authority Platform — Setting / Schema / Registration Changes",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "List all authority-platform, shared-library, and schema changes required for the "
                "enrollment/setting flow. Number each requirement. "
                "Cover: new API messages, changed schema fields, CL version changes, configuration flags. "
                "Minimum 2 paragraphs + ≥ 4 numbered requirements."
            ),
            "prompt_instruction": (
                "Write a short intro, then number each change: (1) what changes, (2) which API or schema, "
                "(3) which participant pair. Describe each change at the BUSINESS level — NO XML/payloads and NO field-level contract tables (field types/lengths/validation belong in the TSD). Reference canonical API names by business purpose; never invent placeholders."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "changes_npci_transaction",
            "heading": "B. Authority Platform — Transaction Flow / Processing Changes",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "List all authority routing, processing, and validation changes for the transaction flow. "
                "Number each requirement. Cover: refCategory handling, credential validation, "
                "switch routing changes, response handling. "
                "Minimum 2 paragraphs + ≥ 4 numbered requirements."
            ),
            "prompt_instruction": (
                "Write a short intro, then number each change with entity ownership. "
                "Describe failure handling as customer-facing error CATEGORIES (auth-failure, business-decline, customer-cancellation, technical-failure) with the owning entity and customer message — NOT alphanumeric wire codes, their classification, or XML payloads (those belong in the TSD). Cover at least 4 failure paths at the business level."
            ),
            "include_table": False,
            "include_diagram": True,
            "diagram_type": "sequence",
            "diagram_description": (
                "Authority switch processing changes for the new transaction flow"
            ),
        },
        # ── Section 7: Envisaged Changes — Participant Systems ───────────
        {
            "section_key": "changes_psp_app",
            "heading": "A. Participant Systems — App-side Changes",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "List all participant-side application changes required for enrollment, "
                "consent capture, and device binding. Number each requirement. "
                "Cover: UI changes, CL SDK invocation, local key storage, device check logic. "
                "Minimum 2 paragraphs + ≥ 4 numbered requirements."
            ),
            "prompt_instruction": (
                "Write a short intro, then number each app-side change. "
                "Name the owning entity for each. Reference every API by canonical name + BUSINESS PURPOSE only — NO XML/payloads (wire detail belongs in the TSD). Never invent placeholder API names."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "activity",
            "diagram_description": "",
        },
        {
            "section_key": "changes_psp_transaction",
            "heading": "B. Participant Systems — Transaction-time Changes",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                "List all participant-side changes required at transaction time. "
                "Number each requirement. Cover: credential capture, CL invocation, "
                "fallback handling, response presentation. "
                "Minimum 2 paragraphs + ≥ 4 numbered requirements."
            ),
            "prompt_instruction": (
                "Write a short intro, then number each transaction-time change with the canonical API (by business purpose — NO XML/payloads, wire detail belongs in the TSD) and describe Failure / Timeout / Reversal handling at the BUSINESS level (no idempotency-key algorithms or wire codes)."
            ),
            "include_table": False,
            "include_diagram": True,
            "diagram_type": "activity",
            "diagram_description": (
                "Participant responsibilities during transaction execution"
            ),
        },
        # ── Section 8: Envisaged Changes — servicing role (pack-driven) ──
        {
            "section_key": "changes_issuer_auth",
            "heading": f"A. {_SERVICING_ROLE} — Auth / Registration Changes",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                f"List all {_SERVICING_ROLE} changes required for authentication, registration, "
                "and credential management under this feature. Number each requirement. "
                f"Cover: what the {_SERVICING_ROLE} must store, validate, and respond to. "
                "Minimum 2 paragraphs + ≥ 4 numbered requirements."
            ),
            "prompt_instruction": (
                f"Write a short intro, then number each {_SERVICING_ROLE} auth/registration change. "
                f"Name the canonical APIs touched, by business purpose only — NO XML/payloads and NO field-level contract tables (those belong in the TSD). Specify at the business level what the {_SERVICING_ROLE} must store, validate, and respond to. "
                "Derive all specifics from the input — do not assume a particular auth mechanism."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "changes_issuer_transaction",
            "heading": f"B. {_SERVICING_ROLE} — Transaction Flow Changes",
            "level": 2,
            "render_style": "body",
            "content_instructions": (
                f"List all {_SERVICING_ROLE} changes for processing {_SERVICING_TXN_PHRASE} under this feature. "
                "Number each requirement. Cover: credential/auth validation, request processing, "
                "response codes, timeout handling, fallback. "
                "Minimum 2 paragraphs + ≥ 4 numbered requirements."
            ),
            "prompt_instruction": (
                "Write a short intro, then number each transaction-flow change with the canonical API (by business purpose — NO XML/payloads, wire detail belongs in the TSD) and describe Failure / Timeout / Reversal handling at the BUSINESS level using customer-facing error CATEGORIES (not alphanumeric wire codes or idempotency-key algorithms)."
            ),
            "include_table": False,
            "include_diagram": True,
            "diagram_type": "sequence",
            "diagram_description": (
                f"{_SERVICING_ROLE} processing of {_SERVICING_TXN_PHRASE} and response flow"
            ),
        },
        # ── Data Model — Core Entities ────────────────────────────────────
        {
            "section_key": "data_model_core_entities",
            "heading": "Business Entities & Lifecycle",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Identify every BUSINESS entity introduced or modified by this change. "
                "This is a CONCEPTUAL business definition — NOT a database schema. "
                "For each entity produce a heading + 4-column table: "
                "[Business Attribute, Purpose, Cardinality, Lifecycle / Notes]. "
                "DO NOT include datatypes, lengths, validation regex, primary-key declarations, "
                "or foreign-key references — those belong in the TSD. "
                f"Cover at minimum: the feature's primary business aggregate ({_AGGREGATE_EXAMPLES}), "
                "the Member/Participant entity, the Policy/Configuration "
                "entity (if business rules need configuration), and the Transaction record. "
                "Minimum 1 intro paragraph + ≥ 3 entity sections."
            ),
            "prompt_instruction": (
                "Produce a heading per business entity with a 4-column conceptual attribute table. "
                "Describe each attribute by its business purpose (e.g. 'circleName — human-friendly "
                "label shown to corporate admin'). NO datatypes, NO lengths, NO PK/FK markers — "
                "those are TSD content."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Transaction State Machine ─────────────────────────────────────
        {
            "section_key": "transaction_state_machine",
            "heading": "Transaction Lifecycle — Business States",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Describe the primary transaction's BUSINESS lifecycle — states meaningful to "
                "the customer / approver / treasury / dispute reviewer, NOT implementation states. "
                "Produce: "
                "(a) States table [State, Customer-Visible Meaning, Owner Layer (responsible for "
                "the state), Time Spent (typical), Notes] — include business terminals "
                "(APPROVED, COMPLETED, FAILED, CANCELLED, EXPIRED, DISPUTED). "
                "(b) Transitions table [From State, To State, BUSINESS Event That Triggers It, "
                "Owner Layer] — name the business event ('approver clicks accept', "
                "'EMI cycle expires', 'customer initiates dispute'), NOT the API call that "
                "implements it. The TSD will map business events → API calls. "
                "(c) Business invariants — states that can never coexist, who can revert a "
                "decision, business-time deadlines (e.g. 'a transaction in PENDING_APPROVAL "
                "for 48h auto-CANCELS'). "
                "Owner Layer values: the participant roles defined for this domain, plus Customer / "
                "Corporate Approver. "
                "Minimum 1 intro paragraph + ≥ 5 states + ≥ 6 transitions + business invariants."
            ),
            "prompt_instruction": (
                "Produce two tables (Business States, Business Transitions) and a numbered "
                "Business Invariants list. The Trigger column names the BUSINESS EVENT, not "
                "the implementing API. NO API references, NO field names — those are TSD content."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": True,
            "diagram_type": "activity",
            "diagram_description": (
                "Transaction state machine: states as nodes, transitions labelled with the triggering API"
            ),
        },
        # ── Failure, Reversal & Idempotency ───────────────────────────────
        {
            "section_key": "failure_reversal",
            "heading": "Failure Handling & Customer Recovery",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Describe failure handling at the BUSINESS / customer-experience level. Sub-sections: "
                "(a) Decline Categories — list each business decline category (auth-failure, "
                "insufficient-funds, business-rule-violation, fraud-flag, technical-failure) with: "
                "customer-facing message, owner layer, what the customer can do next (retry, contact "
                "bank, raise dispute). "
                "(b) Timeout Handling — what the customer sees when a flow stalls, who is responsible "
                "for resolving the unknown final state, and the time window before the customer can "
                "treat the transaction as failed. "
                "(c) Reversal — conditions under which a completed transaction is reversed, who "
                "initiates it, what the customer / counter-party experience, and the business window "
                "within which reversal is permitted. "
                "(d) Dispute Path — when a customer can raise a dispute, who owns the investigation, "
                "and the SLA for first response. "
                "DO NOT include raw wire error codes, per-code classification mapping, idempotency-"
                "key construction, or implementation-level retry parameters — those belong in the TSD. "
                "Render the Decline Categories as a 5-column table: "
                "[Category, Customer-Facing Message, Owner Layer, Customer Next Step, Notes]. "
                "Minimum 1 intro paragraph + 4 sub-sections + Decline Categories table."
            ),
            "prompt_instruction": (
                "Produce four sub-sections (Decline Categories, Timeout, Reversal, Dispute Path). "
                "Use BUSINESS categories (auth-failure / insufficient-funds / business-rule-violation), "
                "NOT wire codes. Specify time windows as business commitments (24h, T+1, etc.). "
                "Customer-Facing Message column shows what the customer actually sees."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Risk, Fraud & Misuse ──────────────────────────────────────────
        {
            "section_key": "risk_fraud_misuse",
            "heading": "Risk, Fraud & Misuse",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Enumerate at minimum 5 misuse / fraud / abuse scenarios specific to this feature. "
                "Render as a 6-column table [Scenario, Attacker Profile, Abuse Vector, Detection Signal, Mitigation Control, Owner Layer]. "
                f"Cover at minimum: {_RISK_SCENARIO_EXAMPLES}. "
                "Minimum 1 intro paragraph + 5-row table + closing paragraph on residual risk."
            ),
            "prompt_instruction": (
                "Produce a 6-column risk table with at least 5 distinct scenarios. Mitigations must be "
                "concrete (specific control, not 'monitor'). Owner column uses canonical layer labels."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Assumptions, Dependencies & Constraints (scope containment) ───
        {
            "section_key": "assumptions_dependencies_constraints",
            "heading": "Assumptions, Dependencies & Constraints",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Three sub-tables required for scope containment: "
                "(a) Assumptions — [#, Assumption, Owner, Risk if Wrong, Validation Method]; "
                "(b) Dependencies — [#, Depends On, Owning Entity, Type (Internal/External/Regulatory), "
                "Blocking (Y/N), Resolution Path]; "
                "(c) Constraints — [#, Constraint, Source (Regulatory/Architectural/Commercial), "
                "Implication for Design]. "
                "Each table at minimum 4 rows. "
                "Concrete numbers in Assumptions MUST be labelled 'Assumption:' or cited [S#]."
            ),
            "prompt_instruction": (
                "Produce three labelled tables (Assumptions, Dependencies, Constraints). "
                "Owner column uses the domain's canonical layer labels (authority / participant / regulator). "
                "Be specific — 'the authority confirms onboarding API capacity' is acceptable; "
                "'system works correctly' is not."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Out of Scope & Future Enhancements (scope containment) ────────
        {
            "section_key": "out_of_scope_future",
            "heading": "Out of Scope & Future Enhancements",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Scope-containment section, two sub-parts: "
                "(a) Out of Scope — numbered list of items deliberately EXCLUDED from this change, "
                "each with a one-line rationale (e.g. 'X is out of scope because Y will land in Phase 2'). "
                "Minimum 5 items. "
                "(b) Future Enhancements — numbered list of capabilities recognised but deferred, "
                "each with target phase / quarter / dependency. Minimum 3 items. "
                "Both lists are explicit and comprehensive — leaving items implicit causes scope creep "
                "during certification / review."
            ),
            "prompt_instruction": (
                "Produce two numbered lists. Out of Scope items are concrete (a feature, an API, "
                "a participant class, a region) — never vague ('miscellaneous edge cases'). "
                "Future Enhancements items name a specific phase or trigger condition."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Service Level Agreements (operational hardening) ─────────────
        {
            "section_key": "sla_matrix",
            "heading": "Service Level Agreements (SLA) Matrix",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Operational-hardening section. Render as 7-column table: "
                "[Flow / API, Owning Entity, Latency Target (p95 / p99), Throughput Target (TPS), "
                "Availability Target (% uptime), Timeout Threshold, SLA Breach Action]. "
                f"Cover at minimum: {_SLA_FLOW_EXAMPLES}. "
                "Numeric values MUST be cited [S#] from corpus, supplied via PM clarification, or "
                "labelled 'Assumption:'. Minimum 6 rows. "
                "After the table, include a paragraph on degradation behaviour — what happens when an "
                "SLA is breached (circuit breaker, queue, retry-with-backoff, fallback path)."
            ),
            "prompt_instruction": (
                "Produce a 7-column SLA table. Numeric SLAs must be anchored. Owning Entity uses canonical "
                "layer labels. Add a closing paragraph on degradation behaviour."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Operational Readiness (operational hardening) ─────────────────
        {
            "section_key": "operational_readiness",
            "heading": "Operational Readiness — Monitoring, Alerting & Runbook",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Production-readiness checklist, four sub-tables: "
                "(a) Monitoring Metrics — [Metric Name, Type (counter/gauge/histogram), "
                "Owning System, Threshold / SLO, Sampling Rate]; "
                "(b) Alerting Rules — [Alert Name, Trigger Condition, Severity (P1/P2/P3), "
                "Owner On-Call Group, Escalation Path]; "
                "(c) Runbook Hooks — [Failure Scenario, First-Response Action, "
                "Auto-Remediation Available (Y/N), Manual Investigation Steps]; "
                "(d) Capacity Planning — [Resource, Current Capacity, Projected Peak, "
                "Headroom %, Scale-Out Mechanism]. "
                "Each table at minimum 4 rows. Cover at minimum: transaction throughput, error-rate "
                "spike, P99 latency breach, downstream dependency timeout, replay/duplicate burst."
            ),
            "prompt_instruction": (
                "Produce four sub-tables (Monitoring Metrics, Alerting Rules, Runbook Hooks, "
                "Capacity Planning). Severity classifications use P1/P2/P3. Auto-Remediation column "
                "specifies the mechanism when 'Y' (e.g. 'circuit breaker after 5 consecutive errors')."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Regulatory & Compliance Hooks (regulatory closure — split) ────
        {
            "section_key": "regulatory_compliance",
            "heading": "Regulatory & Compliance Hooks",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Regulatory-closure section, four mandatory sub-parts: "
                "(a) Regulator Directions Table — [Directive Name, Reference No., Issue Date, "
                "Specific Clause, How This Change Complies]; minimum 3 rows, every row carries [S#] "
                "if corpus evidence supplied. "
                "(b) Authority Circulars Mapping — [Circular Name, OC Number, Status (Superseded / Extended / "
                "Compliant), Specific Section, Compliance Notes]; minimum 2 rows. "
                "(c) DPDP Act & Data Privacy Mapping — [DPDP Provision, This Change's Trigger, "
                "Consent Mechanism, Retention Period, Deletion Path, Breach Notification SLA]; "
                "minimum 4 rows covering at least: consent capture, lawful basis, "
                "purpose limitation, retention, deletion, breach reporting. "
                "(d) Reporting & Audit Obligations — [Report Type, Frequency, Recipient (Regulator/Internal), "
                "Retention Period, Format, Owner]; minimum 4 rows covering at least: transaction reporting, "
                "fraud reporting, audit log retention, certification artefacts. "
                "Every regulatory claim MUST carry a [S#] citation when corpus evidence is supplied."
            ),
            "prompt_instruction": (
                "Produce four labelled sub-tables. Cite [S#] for every regulatory reference from the "
                "supplied corpus. Regulator directive names follow their canonical form. "
                "DPDP provisions name the specific clause (e.g. 'Section 6 - Consent', 'Section 8 - "
                "Lawful basis')."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Annexure A — API Change Summary ───────────────────────────────
        {
            "section_key": "annexure_api_changes",
            "heading": "Annexure A: API Change Inventory (Business-Level)",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Inventory of the WIRE APIs (Req/Resp message pairs) that the RATIFIED PLAN "
                "actually ADDS or CHANGES — business level only (the TSD gives wire detail). "
                "CRITICAL — list ONLY genuine wire APIs. An INTERNAL operation (a database/cache "
                "read, a Kafka emit on an EXISTING topic, a config read, an inter-service method call) is "
                "NOT a wire API: do NOT coin a Req/Resp name for it and do NOT list it as NEW. "
                "IF the plan adds/changes NO wire API (a participant-internal feature), write exactly ONE "
                "line — 'No new or changed wire API; this feature is participant-internal and introduces no "
                "wire message.' — and render NO table. "
                "OTHERWISE render a 4-column table [API Name (canonical Req/Resp), Change Type "
                "(NEW/EXTENDED), Business Purpose, Participant Pair] covering ONLY those real wire APIs. "
                "Mirror the Envisaged-Changes section exactly — do not introduce an API here that isn't there."
            ),
            "prompt_instruction": (
                "List ONLY genuine wire APIs the ratified plan adds/changes. NEVER invent a Req/Resp "
                "name for an internal DB/cache/Kafka/config operation. None → state so in one line, omit the "
                "table. Do not duplicate or expand on the Envisaged-Changes API list."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Annexure B — Error Code Reference ─────────────────────────────
        {
            "section_key": "annexure_error_codes",
            "heading": "Annexure B: Error Categories & Customer Messages",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Customer-facing error CATEGORIES at the business level — the TSD will provide "
                "the per-code wire mapping with its classification. "
                "Render as a 4-column table: "
                "[Error Category, When It Occurs (business condition), Customer-Facing Message, "
                "Customer Next Step (retry / contact bank / raise dispute / cancel)]. "
                "DO NOT include raw wire error codes, their classification, owning-entity at the "
                "code level, or triggering APIs — those belong in the TSD. "
                "Cover at minimum 6 categories: auth-failure, insufficient-funds, business-rule-"
                "violation (e.g. limit breach), fraud / risk hold, technical timeout, customer-"
                "cancellation."
            ),
            "prompt_instruction": (
                "Produce a 4-column error-CATEGORY table. Use business categories, NOT wire codes. "
                "Customer-Facing Message column shows the literal text the customer sees in the "
                "participant app. Customer Next Step is concrete (e.g. 'retry with a smaller amount', "
                "'contact the issuer bank's customer care', 'wait 30 minutes and retry')."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Annexure C — Configuration Parameters ─────────────────────────
        {
            "section_key": "annexure_config",
            "heading": "Annexure C: Configuration Parameters",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "List every configurable parameter introduced or affected. Render as a table "
                "[Parameter, Default Value, Min, Max, Owner Layer, Reset Frequency, Description]. "
                "Cover: transaction limits, velocity counters, retry counts, timeout values, key validity windows, feature flags. "
                "Specific numeric values must be cited [S#] from corpus, supplied by PM clarification, or labelled 'Assumption: ...'. "
                "Minimum 5 rows."
            ),
            "prompt_instruction": (
                "Produce a 7-column configuration-parameter table. Concrete numbers must be anchored or "
                "labelled as Assumption."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Glossary & Abbreviations (institutional packaging) ────────────
        {
            "section_key": "glossary_abbreviations",
            "heading": "Glossary & Abbreviations",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Reviewer-aid table for first-time readers. Render as 3-column table "
                "[Term / Abbreviation, Expansion / Definition, First Used In Section]. "
                "Cover at minimum: every API named in the document, every domain/regulatory acronym "
                "used in it, every domain-specific "
                "term introduced (e.g. 'Lien Mark', 'CRED Block', 'DEEMED status', "
                "'refCategory', 'idempotency key'). Minimum 15 rows. Sort alphabetically."
            ),
            "prompt_instruction": (
                "Produce a 3-column alphabetical Glossary. Definitions are precise — expand the acronym AND state "
                "the role it plays in this domain's flow; a bare restatement such as "
                "'X = a participant' is not acceptable."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Open Questions & Decision Log (institutional packaging) ───────
        {
            "section_key": "open_questions_decision_log",
            "heading": "Open Questions & Decision Log",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Working-doc convention — track outstanding questions and resolved decisions. "
                "Two sub-tables: "
                "(a) Open Questions — [#, Question, Owner, Target Resolution Date, Blocking Stage, "
                "Status (Open / In Discussion / Pending Approval)]; minimum 4 items. "
                "(b) Decision Log — [#, Decision, Date, Decided By, Rationale, Citation [S#] / Assumption]; "
                "minimum 4 items capturing key choices made during this BRD's drafting "
                "(architecture, ownership boundaries, failure-handling approach, regulatory interpretation). "
                "An honest BRD has both lists non-empty — empty Open Questions usually means the author "
                "hasn't yet thought hard enough about ambiguity."
            ),
            "prompt_instruction": (
                "Produce two labelled tables (Open Questions, Decision Log). Status field uses one of "
                "the three exact values listed. Decision Log Rationale is a one-sentence explanation, "
                "not 'we decided X.'"
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        # ── Merged Envisaged-Changes sections (used by COMPACT + STANDARD) ─
        # COMPREHENSIVE keeps the 6-way granular split (authority×Setting/Tx, etc.).
        # Lighter tiers consolidate per-layer: authority / participant / issuer × 1 section each.
        # This avoids forcing a 6-section split when a typical feature only
        # touches 2-3 of the underlying combinations.
        {
            "section_key": "changes_npci_merged",
            "heading": "Envisaged Changes — Authority Platform",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "FIRST decide, from the RATIFIED PLAN's actual API/wire surface, whether this feature "
                "changes ANYTHING at the authority — i.e. adds or changes a wire message (Req/Resp pair), a "
                "shared-library element, or authority switch routing/validation. "
                "• IF IT DOES NOT (a participant-internal feature — the plan adds no new/changed wire message or "
                "XSD): say so in 1-2 sentences ('the authority is not impacted: no new or changed wire message, "
                "no shared-library change, no switch behaviour change; the feature is participant-internal'). Render NO "
                "table and invent NO APIs. Do NOT pad with 'no-change' rows for unaffected APIs — one "
                "line of prose is enough. STOP there. "
                "• ONLY IF the plan genuinely adds/changes a wire API: name it (canonical Req/Resp + "
                "business purpose), the participant pair, and the business behaviour change, in a table "
                "[Existing API, Change Type (EXTENDED/NEW), Business Behaviour Change, Participant Pair]. "
                "ABSOLUTE RULE: NEVER coin a Req/Resp API name for an INTERNAL operation — a database/cache "
                "read, a Kafka emit on an EXISTING topic, a config read, or an inter-service method call is "
                "NOT a wire API and must not appear here as NEW. No XML, no field types, no library versions, no "
                "TD/BD mapping (TSD content)."
            ),
            "prompt_instruction": (
                "Internal-only feature → one or two sentences that the authority is unaffected, NO table, NO invented "
                "APIs. Real wire change → the 4-column table for ONLY the genuine wire APIs. Never name a "
                "Req/Resp for an internal DB/cache/Kafka/config operation."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": True,
            "diagram_type": "sequence",
            "diagram_description": (
                "Authority switch processing for the new feature: registration + transaction flow"
            ),
        },
        {
            "section_key": "changes_psp_merged",
            "heading": "Envisaged Changes — Participant Systems",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Describe every participant-side BUSINESS CHANGE required by this feature, "
                "consolidating app-side and transaction-time concerns into ONE section. "
                "Participants own business logic, transaction limits, UX, consent capture, "
                "policy enforcement. For every change, identify: "
                "(a) the canonical API being extended by NAME and BUSINESS PURPOSE only; "
                "(b) the participant pair; "
                "(c) the customer-experience or business-rule change; "
                "(d) which business policies / limits the participant enforces. "
                "DO NOT include sample XML payloads, field-level data types, validation regex, "
                "CL invocation byte format, or implementation-level retry parameters — those "
                "are TSD content. Describe failure UX (what the customer sees) and dispute path "
                "at the business level. "
                "Render as a 5-column table: [Existing API, Change Type, Customer Experience / "
                "UX Change, Participant-Side Business Rule, Failure UX]. "
                "Minimum 2 paragraphs + 5-column table with ≥ 4 rows."
            ),
            "prompt_instruction": (
                "Write a short intro, then produce the 5-column participant change table. Customer "
                "Experience column describes what changes for the customer (UI, consent, error "
                "presentation). NO XML, NO field-level details — those belong in the TSD."
            ),
            "include_table": False,
            "include_diagram": True,
            "diagram_type": "activity",
            "diagram_description": (
                "Participant responsibilities across enrollment and transaction execution"
            ),
        },
        {
            "section_key": "changes_issuer_merged",
            "heading": f"Envisaged Changes — {_SERVICING_ROLE}",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                f"Describe every {_SERVICING_ROLE} BUSINESS CHANGE required by this feature, "
                "consolidating auth/registration and transaction-processing concerns into ONE section. "
                f"{_SERVICING_ROLE} owns {_SERVICING_ROLE_RESPONSIBILITIES}. "
                "For every change, identify: "
                "(a) the canonical API being extended by NAME and BUSINESS PURPOSE only; "
                "(b) the participant pair; "
                f"(c) the customer-facing decision the {_SERVICING_ROLE} must make (approve / decline / "
                "challenge / hold); "
                f"(d) the BUSINESS DECLINE CATEGORIES the {_SERVICING_ROLE} may return — "
                "categorical, NOT per-code. "
                "DO NOT include sample XML payloads, field-level types, per-code "
                "classification mappings, or cryptographic implementation — those are TSD content. "
                "Render as a 5-column table: [Existing API, Change Type, Customer-Facing Decision, "
                "Decline Categories, Reversal / Recovery Path]. "
                "Minimum 2 paragraphs + 5-column table with ≥ 4 rows."
            ),
            "prompt_instruction": (
                f"Write a short intro, then produce the 5-column {_SERVICING_ROLE} change table. "
                "Decline Categories are business-level, NOT wire codes. NO XML, NO byte-level "
                "credential format — those belong in TSD."
            ),
            "include_table": False,
            "include_diagram": True,
            "diagram_type": "sequence",
            "diagram_description": (
                f"{_SERVICING_ROLE} processing across auth/registration and {_SERVICING_TXN_PHRASE}"
            ),
        },
        # ── References (Source Index reproduction) ────────────────────────
        {
            "section_key": "references",
            "heading": "References",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Reproduce VERBATIM the '## Source index' block supplied at the end of the "
                "Retrieved corpus evidence in the user message. Format as a numbered list: "
                "[S1] <source file>, [S2] <source file>, ... List ONLY the [S#] tags that were "
                "actually cited inline in the document above. If a tag was retrieved but never "
                "cited, omit it. If a [W#] (web research) tag was cited, include those too. "
                "Below the source list, add a short paragraph with the document classification, "
                "review status, and a one-line note: 'This BRD references the above authority/regulator "
                "evidence and is subject to the platform-supplied document metadata.'"
            ),
            "prompt_instruction": (
                "Reproduce the Source index as a numbered list, INCLUDING only the tags actually "
                "cited in the document. This section MUST appear LAST."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
    ],
}

TSD_BLUEPRINT = {
    "title": "Technical Specification Document",
    "subtitle": "Engineering implementation contract",
    "doc_type": "TSD",
    "tone": "Precise, technical, unambiguous",
    "audience": "Engineers at participant organisations and the authority",
    "include_cover_page": True,
    "include_toc": True,
    "sections": [
        {
            "section_key": "background",
            "heading": "Background",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Describe the current state of the ecosystem relevant to this feature. "
                "Cover: (i) what exists today, (ii) the gap or limitation that motivated this change, "
                "(iii) the rationale for the proposed technical approach. Minimum 2 substantive paragraphs."
            ),
            "prompt_instruction": (
                "Write current state first, then limitations, then rationale in separate paragraphs. "
                "Keep the language engineering-level but not implementation-specific."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "product_overview",
            "heading": "Product Overview",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Provide a high-level description of the feature being specified. "
                "What it does, which participants it touches, and what business outcome it enables. "
                "No API field details here — conceptual prose only. Minimum 2 paragraphs."
            ),
            "prompt_instruction": (
                "Describe the feature from a product perspective. Name the participants and flows at a high level. "
                "Do not describe XML field names or payload structures in this section."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "architecture_components",
            "heading": "System Architecture & Component Design",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Specify the actual software components this change adds or modifies, from the RATIFIED "
                "TECHNICAL DESIGN in the input: name each class/module, whether it is NEW or MODIFIED, its "
                "responsibility, and the EXACT injection point (the existing method the change hooks into). "
                "Include a table [Component, New/Modified, Responsibility, Key Method/Hook]. Name real "
                "classes/methods from the design — do NOT describe a generic 'service layer'."
            ),
            "prompt_instruction": (
                "Lead with a 1-paragraph architecture overview, then the component table. Every row must "
                "name a real class/file from the design and where it plugs in. No XML payloads here."
            ),
            "include_table": True,
            "table_fallback_profile": "field_spec",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "data_model",
            "heading": "Data Model & Keyspace",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Specify every data structure this change introduces or reads, from the RATIFIED TECHNICAL "
                "DESIGN: cache/Redis keys (the EXACT key format), in-memory structures, DB columns or config "
                "rows, each with its TTL/lifecycle and what it holds. Include a table [Key / Structure, "
                "Purpose, TTL / Lifecycle]. Introduce nothing beyond the design's data model."
            ),
            "prompt_instruction": (
                "Name the exact keys/structures from the design (e.g. the fingerprint/counter key format, "
                "the config key). 1-paragraph intro then the table. If the change adds no new data structure, "
                "state that in one line — do not pad."
            ),
            "include_table": True,
            "table_fallback_profile": "field_spec",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "control_flow",
            "heading": "Control Flow & Sequence",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Describe the end-to-end runtime control flow of the change as a step-by-step sequence — "
                "from the injection point, through the decision logic, to EACH outcome (e.g. allow / block / "
                "bypass). Use a table [Step, Action, Component]. Cover every branch the design defines."
            ),
            "prompt_instruction": (
                "Write a 1-paragraph overview then a Step/Action/Component table. The sequence diagram must "
                "be BETWEEN the internal components (injection point → new service → data store → decision → "
                "response), NOT a generic inter-participant wire flow — unless the design adds a wire message."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": True,
            "diagram_type": "sequence",
            "diagram_description": "Internal control flow: injection point → new service → data store → decision → response path",
        },
        {
            "section_key": "configuration",
            "heading": "Configuration & Tunability",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Specify every configuration parameter the change introduces, from the design: the EXACT key "
                "name, default value, where it is stored, and whether it can change WITHOUT a redeploy. "
                "Include a table [Config Key, Default, Stored In, Tunable w/o Redeploy, Description]. If the "
                "design says values are tunable at runtime, say exactly how."
            ),
            "prompt_instruction": (
                "Name the real config keys/defaults from the design. 1-paragraph intro then the table. Do "
                "not invent parameters the design does not define."
            ),
            "include_table": True,
            "table_fallback_profile": "field_spec",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "interface_spec",
            "heading": "Interface Specification",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "BEGIN with an 'Interfaces in Scope' inventory TABLE listing the API / message / interface "
                "THIS flow ACTUALLY uses — derived ONLY from the ratified plan / flow, not only the ones "
                "that change, and inventing NONE. Columns: [Interface, Type (wire message / event "
                "stream / internal service method / config), Status (NEW / EXTENDED / REUSED — no change / "
                "CONSUMED — read-only), Role in this flow]. "
                "When the flow rides on an EXISTING wire API, show it EVEN THOUGH it is unchanged (Status "
                "'REUSED — no change' / 'CONSUMED — read-only') so the engineer sees the complete surface. "
                "Do NOT assume or force any particular API — treat any API named below as an EXAMPLE, not a "
                "requirements; include an interface ONLY IF this specific flow uses it. List exactly the "
                "interfaces the plan's flow touches, no more and no less. "
                "THEN detail the interfaces that CHANGE: a wire change → request/response XML samples "
                "(declaring the namespace exactly as the domain's schema binds it) + a field dictionary [Field Name, dType, dLength, Description, Mandatory] from "
                "the design; an INTERNAL change → the new method signatures + the event/Ack contract the "
                "design decided (no invented inter-participant XML/fields/endpoints). For a REUSED interface, state "
                "'reused unchanged' and do NOT redefine its schema."
            ),
            "prompt_instruction": (
                "Lead with the Interfaces-in-Scope table covering exactly the interfaces THIS flow uses — "
                "include a reused/consumed existing API ONLY when the flow actually rides on it (marked 'no "
                "change'); never force a specific API. Then detail only the ones that change. Put XML/code "
                "in code_blocks; invent no fields or endpoints."
            ),
            "include_table": True,
            "table_fallback_profile": "field_spec",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "failure_resilience",
            "heading": "Failure Handling & Resilience",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Specify the change's behaviour under failure EXACTLY as the design ratified it: the fail-OPEN "
                "vs fail-CLOSED posture (state which, verbatim from the design — never invert it), timeout "
                "values, retry/idempotency behaviour, and what is emitted for monitoring/audit on each failure "
                "path. Cover the degraded path (e.g. cache/store unavailable)."
            ),
            "prompt_instruction": (
                "State the fail-open/closed posture exactly as the design says — never the opposite. Cover "
                "timeout, idempotency, and the monitoring/audit signal on degradation."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "error_handling",
            "heading": "Error & Response Handling",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Provide ONE table of the failure/response scenarios this change handles, using ONLY the "
                "response codes the RATIFIED DESIGN defines — do NOT invent wire error codes the design "
                "did not specify. Columns: [Scenario, Response / Code, Description, Where Raised]. For an "
                "internal change, use the internal response/decline code the design decided; every code and "
                "state name MUST be identical to what the other sections use. Minimum 4 rows, short intro first."
            ),
            "prompt_instruction": (
                "Use ONLY codes named in the design. Keep every code/state identical across ALL sections — "
                "never give two different codes for the same event. Invent no wire codes."
            ),
            "include_table": True,
            "table_fallback_profile": "error_matrix",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "testing",
            "heading": "Testing & Verification",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Specify how the change is verified: unit tests for the new component's decision logic, "
                "integration tests for the injection point, and scenario tests for each branch the design "
                "defines (e.g. trigger fires, does-not-fire, degraded/bypass path). Map each PM success "
                "criterion to a concrete test. Use a table [Test, Scenario, Expected Result] where useful."
            ),
            "prompt_instruction": (
                "Derive tests from the design's branches and the success criteria — one row per behaviour. "
                "Concrete and checkable; no generic 'test thoroughly' filler."
            ),
            "include_table": True,
            "table_fallback_profile": "process_steps",
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "rollout_rollback",
            "heading": "Rollout & Rollback",
            "level": 1,
            "render_style": "body",
            "content_instructions": (
                "Specify how the change is enabled, ramped, and backed out: the enable/disable switch (flag or "
                "config key from the design), any phased rollout, and the exact rollback step if it must be "
                "disabled in production. State whether disabling needs a redeploy or is config-only."
            ),
            "prompt_instruction": (
                "Name the real enable/disable control from the design. Keep it to the operational steps — "
                "enable, ramp, back-out — in a few tight paragraphs."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "section_key": "note",
            "heading": "Notes & Assumptions",
            "level": 1,
            "render_style": "body",
            "validation_fill_numbered_items": True,
            "content_instructions": (
                "List all cross-flow technical notes, caveats, and mandatory operational rules "
                "that apply to this feature's implementation. "
                "Use a numbered list. Each note is a standalone, actionable technical statement. "
                "Minimum 4 numbered notes covering integration rules, edge cases, "
                "timeout handling, and any other implementation-critical details from the input."
            ),
            "prompt_instruction": (
                "Use numbered_items for this section, not paragraphs. "
                "Each note must be self-contained and implementation-actionable. "
                "Derive all notes from the input — no generic boilerplate."
            ),
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
    ],
}


def _normalize_doc_type(doc_type: str) -> str:
    return (doc_type or "").strip().lower()


# ─────────────────────────────────────────────────────────────────────────────
# BRD tier definitions — dynamic blueprint sizing based on feature complexity.
# ─────────────────────────────────────────────────────────────────────────────
# Three tiers, each with its own Envisaged-Changes granularity:
#
#   compact (~11 sections) — single contained change. Single MERGED authority
#       envisaged-changes section. No state machine, no extra annexures.
#
#   standard (~20 sections) — typical authority feature. Three MERGED Envisaged-
#       Changes sections (authority / participant / issuer, one each). No state machine
#       (most features don't introduce a new transaction lifecycle). No
#       annexure_config (most features don't add new configurables).
#       This is the right size for most "proper feature additions".
#
#   comprehensive (~30 sections) — greenfield / multi-participant /
#       regulatory-heavy. Six GRANULAR Envisaged-Changes sections
#       (authority×{Setting,Tx}, participant×{App,Tx}, issuer×{Auth,Tx}). State machine,
#       SLA matrix, operational readiness, all annexures, all institutional
#       packaging.
#
# Subset note: compact ⊂ standard for the SHARED keys, but Envisaged-Changes
# differs across tiers (merged in compact/standard, granular in comprehensive)
# — by design.

BRD_TIER_COMPACT_KEYS = {
    # Minimal viable BRD: single feature, single layer, no regulatory load.
    "background_current_state",
    "background_limitations",
    "background_rationale",
    "product_description",
    "product_construct_transaction",
    "salient_points",
    "functional_requirements",
    "changes_npci_merged",         # merged NPCI section (covers Setting + Transaction)
    "failure_reversal",
    "annexure_api_changes",
    "references",
}

BRD_TIER_STANDARD_KEYS = BRD_TIER_COMPACT_KEYS | {
    # + flow setup, dispute, full per-layer Envisaged Changes (merged),
    # data model, risk/fraud, regulatory, error-code annexure, glossary.
    # NO state machine (only when a real lifecycle changes).
    # NO annexure_config (only when new configurables introduced).
    # Result: ~20 sections — right-sized for typical features.
    "product_construct_setting",
    "dispute_management",
    "changes_psp_merged",          # merged PSP section
    "changes_issuer_merged",       # merged Issuer Bank section
    "data_model_core_entities",
    "risk_fraud_misuse",
    "regulatory_compliance",
    "annexure_error_codes",
    "glossary_abbreviations",
}

# Comprehensive — explicit allowlist excluding the merged sections (which
# are STANDARD/COMPACT-only). Uses the 6-way granular Envisaged-Changes
# layout for full coverage.
BRD_TIER_COMPREHENSIVE_KEYS = {
    "background_current_state",
    "background_limitations",
    "background_rationale",
    "product_description",
    "product_construct_setting",
    "product_construct_transaction",
    "salient_points",
    "dispute_management",
    "functional_requirements",
    # Granular Envisaged Changes — 6 sections, one per layer × phase
    "changes_npci_setting",
    "changes_npci_transaction",
    "changes_psp_app",
    "changes_psp_transaction",
    "changes_issuer_auth",
    "changes_issuer_transaction",
    "data_model_core_entities",
    "transaction_state_machine",
    "failure_reversal",
    "risk_fraud_misuse",
    "assumptions_dependencies_constraints",
    "out_of_scope_future",
    "sla_matrix",
    "operational_readiness",
    "regulatory_compliance",
    "annexure_api_changes",
    "annexure_error_codes",
    # annexure_config DROPPED from BRD — configuration parameters with
    # types, defaults, min/max, and reset frequency are TSD-level content.
    # The BRD declares the BUSINESS need for configurability (in
    # assumptions_dependencies_constraints or in the relevant Envisaged
    # Changes section); the TSD specifies the parameter values + schema.
    "glossary_abbreviations",
    "open_questions_decision_log",
    "references",
}


def _filter_blueprint_by_tier(blueprint: dict, tier: str) -> dict:
    """Return a copy of `blueprint` containing only the sections for `tier`.

    Each tier has its own explicit keyset — no tier returns the unfiltered
    blueprint, so the merged Envisaged-Changes sections never leak into
    comprehensive (they're a STANDARD/COMPACT replacement, not addition).
    """
    tier = (tier or "comprehensive").strip().lower()
    if tier == "compact":
        keep = BRD_TIER_COMPACT_KEYS
    elif tier == "standard":
        keep = BRD_TIER_STANDARD_KEYS
    else:
        keep = BRD_TIER_COMPREHENSIVE_KEYS
    bp = deepcopy(blueprint)
    bp["sections"] = [s for s in bp["sections"] if s.get("section_key") in keep]
    return bp


def get_document_blueprint(doc_type: str, tier: str | None = None) -> dict[str, Any] | None:
    """Return a deep-copy of the blueprint for `doc_type`, filtered by tier.

    `tier` is honoured only for BRD (other doc types ignore it). Pass
    `"compact"` / `"standard"` / `"comprehensive"` (default).
    """
    normalized = _normalize_doc_type(doc_type)
    if normalized == "circular":
        return deepcopy(CIRCULAR_BLUEPRINT)
    if normalized == "product note":
        return deepcopy(PRODUCT_NOTE_BLUEPRINT)
    if normalized == "brd":
        return _filter_blueprint_by_tier(deepcopy(BRD_BLUEPRINT), tier or "comprehensive")
    if normalized == "tsd":
        return deepcopy(TSD_BLUEPRINT)
    return None


def derive_subject(prompt: str) -> str:
    cleaned = " ".join(prompt.split())
    if not cleaned:
        return "Subject: Circular update"
    shortened = cleaned[:140].rstrip(" .,;:")
    if len(shortened.split()) > 20:
        shortened = " ".join(shortened.split()[:20])
    if not shortened.lower().startswith("subject:"):
        shortened = f"Subject: {shortened}"
    return shortened


def build_blueprint_plan(doc_type: str, brief: dict[str, Any]) -> dict[str, Any] | None:
    # Tier (compact / standard / comprehensive) carried via `brief` — picked
    # by the LLM-classifier in pipeline.retrieve_context. Defaults to
    # comprehensive if absent for back-compat with non-BRD doc types.
    tier = (brief or {}).get("brd_tier") or "comprehensive"
    blueprint = get_document_blueprint(doc_type, tier=tier)
    if not blueprint:
        return None

    prompt = brief.get("prompt", "")
    # Circular letterhead defaults. These are rendered INTO the document, so a
    # hardcoded issuer signs one ecosystem's authority onto another's circular.
    # Resolved per-pack at call time (not import) — the brief always wins.
    from app.core.domain.registry import prompt_block
    organization_name = brief.get("organization_name") or prompt_block("authority", "the issuing authority")
    domain_code = prompt_block("domain_name", "GEN")
    signatory_department = brief.get("signatory_department") or "Product & Operations"
    current_year = datetime.now().year
    reference_code = (brief.get("reference_code")
                      or f"{organization_name}/{domain_code}/OC No. 001/{current_year}-{current_year + 1}")
    issue_date = brief.get("issue_date") or datetime.now().strftime("%d %B %Y")
    recipient_line = brief.get("recipient_line") or f"All {prompt_block('ecosystem_actors', 'member organisations')}"
    subject = brief.get("subject_line") or derive_subject(prompt)
    version_number = brief.get("version_number") or "1.0"
    classification = brief.get("classification") or "Draft / Confidential"

    # Use the field names that match _revision_row_data() in docx_builder.py
    revision_history = [
        {
            "version": version_number,
            "version_no": version_number,
            "document_name": blueprint["title"],
            "date_of_change": issue_date,
            "changed_by": brief.get("signatory_name") or organization_name,
            "reviewed_by": "",
            "remarks": "Initial draft",
        }
    ]

    blueprint["title"] = subject.replace("Subject:", "").strip() or "Circular"
    if _normalize_doc_type(doc_type) != "circular":
        blueprint["title"] = brief.get("document_title") or blueprint["title"]
    blueprint["subtitle"] = prompt[:120] if prompt else blueprint["subtitle"]
    blueprint["document_meta"] = {
        "organization_name": organization_name,
        "reference_code": reference_code,
        "issue_date": issue_date,
        "recipient_line": recipient_line,
        "subject_line": subject,
        "version_number": version_number,
        "classification": classification,
        "revision_history": revision_history,
        "signatory_name": brief.get("signatory_name") or "Authorized Signatory",
        "signatory_title": brief.get("signatory_title") or "Senior Vice President",
        "signatory_department": signatory_department,
        "audience": brief.get("audience") or blueprint.get("audience", ""),
        "desired_outcome": brief.get("desired_outcome") or "",
        "format_constraints": brief.get("format_constraints") or "",
    }
    return blueprint
