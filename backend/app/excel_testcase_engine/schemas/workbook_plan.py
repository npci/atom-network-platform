# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Pydantic models for workbook planning and rendering boundaries."""

from __future__ import annotations

from app.excel_testcase_engine.observability import get_logger
from app.services.cert_roles import authority_wire, partner_wire

from typing import ClassVar, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

LOGGER = get_logger("network.schemas.workbook_plan")


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


# Canonical network message leg names.
# Req/Resp cover the primary send + reply. Notification/Ack cover the
# async downstream ReqTxnConfirmation / RespTxnConfirmation-style leg.
# Nullable on TestCaseStub — unset means "not leg-specific".
MessageLeg = Literal["Req", "Resp", "Notification", "Ack"]


# Reviewer priority.
# P0 = blocking (must pass to certify), P1 = should-pass, P2 = nice-to-have.
# None (default) means the Planner did not assign one — treated as P1 by
# consumers that need a value, but the workbook DETAILS block only prints
# the line when a priority IS set.
Priority = Literal["P0", "P1", "P2"]


class TraceabilityRefs(BaseModel):
    """Where this test case's requirement came from.

    All three fields are lists of short reference tokens. Empty lists mean
    "no traceable source recorded" — the reviewer can still read the DETAILS
    block, but there's no automated audit link. Tokens are free-text so
    both BRD-style (`BRD-FR-14`) and prose-style (`§4.2 approval flow`)
    references round-trip cleanly.
    """

    brd_refs: list[str] = Field(default_factory=list)
    tsd_refs: list[str] = Field(default_factory=list)
    xsd_field_ref: str | None = None


Confidence = Literal["high", "medium", "low"]


class FlowDefinition(BaseModel):
    """One network flow's wire-format definition authored by the engine for a new API.

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
    # The two sides of the exchange, in the platform's OWN vocabulary. These
    # were `payer_handle` / `payee_handle`: the workbook's HEADER text was
    # pack-ified (excel_writer/layouts.py) but the KEYS were not, so every
    # domain's exported cert JSON still carried one ecosystem's field names for
    # a library loan or a charge-point session.
    #
    # `validation_alias` keeps ACCEPTING the old spellings — stored rendered
    # plans, uploaded workbooks parsed by `api._COL_SYNONYMS`, and the
    # markdown-upload path in `api/cert_push.py` all still populate these — so
    # only what the engine EMITS changed.
    initiator_handle: str = Field(
        default="",
        validation_alias=AliasChoices("initiator_handle", "payer_handle"),
    )
    counterparty_handle: str = Field(
        default="",
        validation_alias=AliasChoices("counterparty_handle", "payee_handle"),
    )
    scenario_summary: str
    expected_status: Literal["Success", "Failure", "Deemed", "Partial"]
    response_code: str = ""
    coverage_tag: str = "happy_path"
    pair_id: str | None = None
    highlight: bool = False
    # Display label for the workbook's "Scope" column. Empty means "resolve from
    # the active pack at render time" (see testcase_sheet._display_scope) — it
    # used to default to the literal "v2.0", which stamped one ecosystem's
    # version onto every row of every workbook in every domain.
    scope: str = ""
    # CANONICAL WIRE VALUE, resolved from the active pack's `cert_roles:`
    # (`partner_wire` / `authority_wire`). Do NOT widen it to a domain's real
    # participant names: `cert_push` forwards it to cert-agent, which matches
    # these tokens exactly, so a new value silently mis-groups a cert run
    # across a trust boundary. The workbook renders a per-domain LABEL for it
    # instead (testcase_sheet._display_initiator); the wire keeps the tokens.
    # Defaulted via `default_factory` because the pack is chosen at runtime —
    # a plain default would freeze one deployment's token at import.
    txn_initiated_by: str = Field(default_factory=lambda: partner_wire())
    # Which side of the exchange the participant acts as. Same rename and same
    # dual-accept as the two handle fields above; the workbook column header
    # already came from the pack's `role_as_label`.
    role_as: str = Field(
        default="",
        validation_alias=AliasChoices("role_as", "psp_as"),
    )
    # BRD Functional Requirement id this case verifies. Enables Writer prose
    # grounding and Validator's _fr_link_check. Nullable so back-compat holds
    # for runs where BRD feature-criteria extraction returned empty.
    fr_ref: str | None = None
    # Which network operation this case tests (init/auth/debit/credit/
    # debit_reversal/credit_reversal/meta_query). Drives scope-ownership
    # enforcement in the Planner. Empty string when unset — treated as
    # unconstrained (backward compat).
    operation: str = ""

    # Field-level traceability.
    # `covers_field`         — the FieldSpec.name (e.g. "preAuthLimit") this
    #                          case exercises. Required for field-negative
    #                          coverage tags (missing_mandatory /
    #                          invalid_value / field_length / invalid_format
    #                          / invalid_enum / wrong_leg / unexpected_leg);
    #                          None for flow-level tags. Enforced by
    #                          validator.md's field_negative_wo_covers_field
    #                          check when the tag is field-shaped.
    # `message_leg`          — which network leg the field manifests on. Only
    #                          meaningful for wrong_leg / unexpected_leg.
    # `covers_business_rule` — free-text BRD business-rule handle (or FR-id
    #                          fragment) when the case exercises a rule
    #                          rather than a raw field. Populated for
    #                          coverage_tag == "business_rule_fail" cases.
    covers_field: str | None = None
    message_leg: MessageLeg | None = None
    covers_business_rule: str | None = None

    # Reviewer priority + BRD/TSD/XSD traceability.
    # Both nullable so plans authored before these fields existed (or by lightweight
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

    # The planner and writer LLMs decide this per case, and BOTH prompts are
    # rendered with the active pack's initiator tokens (`_planner_system` /
    # `_writer_system`), so a well-behaved model emits one of the two spellings
    # below. Clamp casing variants to the canonical spellings the Annexure
    # column expects; anything unrecognised (including the legacy combined
    # "NPCI/Bank" seen in old reference packs) falls back to the PARTNER token —
    # the dominant value — rather than leaking free text into the workbook and
    # the cert-push initiated_by mapping (cert_push._normalise_initiated_by
    # re-derives the role through cert_roles.normalize_role).
    #
    # The fallback WARNS rather than passing quietly, because its output is
    # indistinguishable from a correct answer. While writer.md still hardcoded
    # one ecosystem's tokens, every authority-initiated case on a non-payments
    # pack landed here and was relabelled partner-initiated — with a green test
    # suite throughout, because the suite only ever ran the pack whose tokens
    # were hardcoded.
    @field_validator("txn_initiated_by", mode="before")
    @classmethod
    def _normalize_txn_initiated_by(cls, v):
        if isinstance(v, str):
            canon = {
                partner_wire().lower(): partner_wire(),
                authority_wire().lower(): authority_wire(),
            }
            key = v.lower().strip()
            if key in canon:
                return canon[key]
            if key:
                LOGGER.warning(
                    "workbook_plan.unrecognised_initiator",
                    received=v,
                    expected=sorted(canon.values()),
                    fallback=partner_wire(),
                )
        return partner_wire()


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
