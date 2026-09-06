# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pydantic models for workbook planning and rendering boundaries."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field, field_validator


# BRD/TSD-only refactor: coverage_tag is now free-form so TSD-authored
# scenario names pass through Pydantic validation. The set below is the
# recommended vocabulary — tc_store_sync._COVERAGE_SUBSETS routes these
# seven names to cert subsets; unknown names fall through with empty
# subset (documented, accepted behaviour). BRD/TSD-authored slugs like
# "duplicate_vpa" are welcome.
RECOMMENDED_COVERAGE_TAGS = frozenset({
    "happy_path", "timeout", "neg_ack", "decline",
    "deemed", "revoke", "partial",
})


# Slice 2 (cert-tc-v2) — canonical UPI message leg names.
# Req/Resp cover the primary send + reply. Notification/Ack cover the
# async downstream ReqTxnConfirmation / RespTxnConfirmation-style leg.
# Nullable on TestCaseStub — unset means "not leg-specific".
MessageLeg = Literal["Req", "Resp", "Notification", "Ack"]


# Slice 3 (cert-tc-v2) — reviewer priority.
# P0 = blocking (must pass to certify), P1 = should-pass, P2 = nice-to-have.
# None (default) means the Planner did not assign one — treated as P1 by
# consumers that need a value, but the workbook DETAILS block only prints
# the line when a priority IS set.
Priority = Literal["P0", "P1", "P2"]


class TraceabilityRefs(BaseModel):
    """Slice 3 — where this test case's requirement came from.

    All three fields are lists of short reference tokens. Empty lists mean
    "no traceable source recorded" — the reviewer can still read the DETAILS
    block, but there's no automated audit link. Tokens are free-text so
    both BRD-style (`BRD-FR-14`) and prose-style (`§4.2 mandate flow`)
    references round-trip cleanly.
    """

    brd_refs: list[str] = Field(default_factory=list)
    tsd_refs: list[str] = Field(default_factory=list)
    xsd_field_ref: str | None = None


Confidence = Literal["high", "medium", "low"]


class FlowDefinition(BaseModel):
    """One UPI flow's wire-format definition authored by the engine for a new API.

    Mirrors cert-agent's POST /api/flows body so the sync layer forwards it
    verbatim. Mustache placeholders in `request_xml_template` are rendered by
    cert-simulator at dispatch time. The engine emits these only for APIs
    cert-agent doesn't already know about; the sync layer's diff endpoint
    surfaces them under `proposed_flow_defs` for operator review.
    """

    flow_code:            str
    api_request:          str
    api_response:         str
    request_xml_template: str = ""
    simulator_endpoint:   str = "/execute"
    expected_resp_codes:  list[str] = Field(default_factory=lambda: ["00"])
    default_test_data:    dict      = Field(default_factory=dict)
    role:                 str = ""
    description:          str = ""
    # Provenance — BRD section title, RAG doc id, or circular reference.
    # Surfaced in the SyncDiffModal so the operator can audit the source.
    source:               str = ""
    # `high` = grounded in a retrieved spec/sample; `low` = no grounding,
    # operator MUST fill in the XML before confirming. The engine never
    # fabricates XML — `low` rows ship with `request_xml_template = ""`.
    confidence:           Confidence = "medium"


class RenderedTestCase(BaseModel):
    """Final text blocks written into a test-case row."""

    test_id: str
    details_block: str
    description_block: str
    steps_block: str


class TestCaseStub(BaseModel):
    """Planner output for one test case before writer prose is attached."""

    __test__: ClassVar[bool] = False

    test_id: str
    apis: list[str]
    api_type: str
    entities: list[str]
    approval_type: str = ""
    payer_handle: str = ""
    payee_handle: str = ""
    scenario_summary: str
    expected_status: Literal["Success", "Failure", "Deemed", "Partial"]
    response_code: str = ""
    coverage_tag: str = "happy_path"
    pair_id: str | None = None
    highlight: bool = False
    # Display label for the workbook's "Scope" column. Empty means "resolve from
    # the active pack at render time" (see testcase_sheet._display_scope) — it
    # used to default to the literal "UPI 2.0", which stamped one ecosystem's
    # version onto every row of every workbook in every domain.
    scope: str = ""
    # CANONICAL WIRE VALUE — "Bank" | "NPCI". Do NOT widen this to a domain's real
    # participant names: `cert_push` forwards it to cert-agent, whose prompt matches
    # BANK/NPCI exactly, so a new value silently mis-groups a cert run across a
    # trust boundary. The workbook renders a per-domain LABEL for it instead
    # (testcase_sheet._display_initiator); the wire keeps these two tokens.
    txn_initiated_by: str = "Bank"
    psp_as: str = ""
    # BRD Functional Requirement id this case verifies. Enables Writer prose
    # grounding and Validator's _fr_link_check. Nullable so back-compat holds
    # for runs where BRD feature-criteria extraction returned empty.
    fr_ref: str | None = None
    # Which UPI operation this case tests (init/auth/debit/credit/
    # debit_reversal/credit_reversal/meta_query). Drives scope-ownership
    # enforcement in the Planner. Empty string when unset — treated as
    # unconstrained (backward compat).
    operation: str = ""

    # Slice 2 (cert-tc-v2) — field-level traceability.
    # `covers_field`         — the FieldSpec.name (e.g. "preAuthLimit") this
    #                          case exercises. Required for field-negative
    #                          coverage tags (missing_mandatory /
    #                          invalid_value / field_length / invalid_format
    #                          / invalid_enum / wrong_leg / unexpected_leg);
    #                          None for flow-level tags. Enforced by
    #                          validator.md's field_negative_wo_covers_field
    #                          check when the tag is field-shaped.
    # `message_leg`          — which UPI leg the field manifests on. Only
    #                          meaningful for wrong_leg / unexpected_leg.
    # `covers_business_rule` — free-text BRD business-rule handle (or FR-id
    #                          fragment) when the case exercises a rule
    #                          rather than a raw field. Populated for
    #                          coverage_tag == "business_rule_fail" cases.
    covers_field: str | None = None
    message_leg: MessageLeg | None = None
    covers_business_rule: str | None = None

    # Slice 3 (cert-tc-v2) — reviewer priority + BRD/TSD/XSD traceability.
    # Both nullable so plans authored before Slice 3 (or by lightweight
    # briefs where the LLM couldn't decide) still validate. Writer emits
    # DETAILS-block lines only when the value is populated.
    priority: Priority | None = None
    traceability: TraceabilityRefs | None = None

    rendered: RenderedTestCase | None = None

    # Defensive normaliser: the post_processor mutates `expected_status` to
    # archetype-specific casing ("SUCCESS" for Archetype A/B, "Success" for
    # C). Pydantic v2's default doesn't validate on assignment, so the
    # mutation is allowed at runtime — but a subsequent
    # `WorkbookPlan.model_validate(...)` (used by streaming.py to reload
    # the saved 03-rendered_plan.json for to_markdown rendering) would
    # then fail the strict Literal check, leaving the UI to show only the
    # "Workbook generated" fallback. Normalising any-case input back to
    # canonical Title Case on load keeps that path robust without changing
    # what gets written to the .xlsx (the renderer applies casing at write
    # time independently).
    @field_validator("expected_status", mode="before")
    @classmethod
    def _normalize_expected_status(cls, v):
        if isinstance(v, str):
            canon = {
                "success": "Success",
                "failure": "Failure",
                "deemed":  "Deemed",
                "partial": "Partial",
            }
            normalized = canon.get(v.lower().strip())
            if normalized is not None:
                return normalized
        return v

    # The planner LLM decides this per case (Bank vs NPCI, see
    # prompts/planner.md). Clamp casing variants to the canonical spellings
    # the Annexure column expects; anything unrecognised (including the
    # legacy combined "NPCI/Bank" seen in old reference packs) falls back to
    # "Bank" — the dominant value — rather than leaking free text into the
    # workbook and the cert-push initiated_by mapping
    # (cert_push._normalise_initiated_by matches BANK/NPCI exactly).
    @field_validator("txn_initiated_by", mode="before")
    @classmethod
    def _normalize_txn_initiated_by(cls, v):
        if isinstance(v, str):
            canon = {
                "bank": "Bank",
                "npci": "NPCI",
            }
            return canon.get(v.lower().strip(), "Bank")
        return "Bank"


class SheetSpec(BaseModel):
    """One workbook sheet and its cases or metadata."""

    name: str
    # Defaults to "" rather than being required. The renderer builds the
    # Index/Summary/Subset/Modes/Version Log sheets itself from `plan.sheets`
    # membership + `plan.archetype` alone — it never reads a SheetSpec for
    # them — so the planner LLM has no valid layout string to give those
    # entries. Historically it emitted `null` there, which used to hard-fail
    # WorkbookPlan validation (a required `str` field rejects None). An
    # empty string is treated as "metadata-only" by every consumer (none of
    # `_TEST_CASE_LAYOUTS`, `LAYOUT_REGISTRY`, or the renderer's `scope` /
    # `uat_mobile` branches match it), so this sheet is simply skipped at
    # write time instead of blowing up the whole plan.
    layout: str = ""
    tab_color: str = "9DC3E6"
    test_cases: list[TestCaseStub] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    @field_validator("layout", mode="before")
    @classmethod
    def _normalize_layout(cls, v):
        # Coerce None / non-string junk from the LLM into "" instead of
        # raising — see the field comment above for why "" is safe.
        if v is None:
            return ""
        return v

    @field_validator("tab_color", mode="before")
    @classmethod
    def _normalize_tab_color(cls, v):
        # openpyxl's `tabColor` setter accepts ONLY bare aRGB hex ("4472C4" /
        # "FF4472C4") and raises `ValueError: Colors must be aRGB hex values`
        # on anything else. The planner prompt asks for `tab_color` without
        # pinning the format, so the LLM freely emits CSS-style "#4472C4" —
        # which crashed `testcase_sheet.build` mid-render, killed the WS, and
        # surfaced to the user as the generic "Generation connection closed
        # before completion". Non-deterministic by nature: the same prompt
        # yields a bare hex on one run and a "#"-prefixed one on the next.
        #
        # WHY normalise here rather than at the renderer: this is the single
        # choke point every plan passes through — LLM output, artifact reload
        # in streaming._wb_plan_for_job, and uploaded-pack round-trips alike.
        # WHY fall back instead of raising: a cosmetic tab colour must never
        # cost the user a full generation run.
        if not isinstance(v, str):
            return "9DC3E6"
        cleaned = v.strip().lstrip("#").upper()
        if len(cleaned) in (6, 8) and all(c in "0123456789ABCDEF" for c in cleaned):
            return cleaned
        return "9DC3E6"


class WorkbookPlan(BaseModel):
    """Complete workbook plan consumed by the deterministic renderer."""

    filename: str
    archetype: Literal["A", "B", "C"]
    sheets: list[SheetSpec]
    global_conventions: dict = Field(default_factory=dict)
    coverage_audit: dict[str, dict[str, int]] = Field(default_factory=dict)
    # BRD/TSD-only: the flow_generator node was removed with the rest of
    # the domain-knowledge scaffolding, so this list is always empty. Kept
    # on the schema so the cert-simulator sync layer's operator-modal
    # fallback continues to work unchanged.
    flow_definitions: list[FlowDefinition] = Field(default_factory=list)
