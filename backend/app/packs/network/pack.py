# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The default network domain pack.

Deliberately thin. It is a re-parenting of content that already existed —
`rules.py` moved here verbatim from `core/domain_rules.py`, and `artifacts()`
reads the blueprints that already live in `app.agents.blueprints`. That is the
whole argument for extraction over rewrite: the pack is a class statement
around content the platform already had.

`change_types()` and `artifacts()` are populated from what is true today rather
than left empty, because an empty required member is a lie that type-checks.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from app.core.domain.contract import (
    ArtifactSpec, CertVocabulary, ChangeType, CrossFieldRule, LabeledOption,
    Participant,
)
from app.core.prompts import load_prompt

from .api_design_principles import API_DESIGN_PRINCIPLES
from .rules import NETWORK_ERROR_CODE_EXAMPLES, NETWORK_HARD_RULES

# The docgen pipeline's ecosystem/message/flow reference block. Lives under
# `prompts/docgen/...` (not `packs/network/`) because it predates the pack seam and
# several doc-type-specific prompts (BRD/TSD/PN/Circular) still load it
# directly — moving the file would be a bigger, separate change. The GENERIC
# docgen fallback prompt, however, now goes through THIS pack key rather than
# unconditionally getting this domain's content — see docgen/agents/pipeline.py.
_DOCGEN_GENERIC_DOMAIN_NOTES = load_prompt("docgen/agents/pipeline/common_network_domain.md")

# Artifact key → (label, renderer, blueprint doc_type in app.agents.blueprints).
# blueprint may be None: a certification test-case workbook is not sectioned
# prose, which is why ArtifactSpec.blueprint is optional.
_ARTIFACTS: tuple[tuple[str, str, str, str | None], ...] = (
    ("canvas",     "Product Canvas",              "markdown", "canvas"),
    ("brd",        "Business Requirement Document", "docx",   "brd"),
    ("tech_spec",  "Technical Specification",     "docx",     "tech_spec"),
    ("xsd",        "XSD Schema",                  "markdown", "xsd"),
)


class NetworkPack:
    """Satisfies `app.core.domain.contract.DomainPack` structurally.

    `certification()` and `channel()` ARE wired (below) — this domain has both, the
    certification harnesses and A2A. The general rule still holds for other
    packs: omitting a method is how a pack declares a capability it does not
    supply, and `certification_of(pack) -> None` is a true statement about an
    ecosystem with no certification body, not a stub to be filled in.
    """

    key = "network"
    version = "1.0"

    def change_types(self) -> Sequence[ChangeType]:
        artifact_keys = [a[0] for a in _ARTIFACTS]
        return (
            ChangeType(
                key="new_feature",
                label="New network feature",
                artifacts=artifact_keys,
                requires_certification=True,
                requires_publication=True,
            ),
            ChangeType(
                key="spec_amendment",
                label="Specification amendment",
                artifacts=["brd", "tech_spec"],
                requires_certification=True,
                requires_publication=True,
            ),
        )

    def artifacts(self) -> Sequence[ArtifactSpec]:
        # Lazy import: `agents.blueprints` must not be pulled in when the
        # registry merely resolves the pack at module-import time.
        from app.agents.blueprints import get as get_blueprint

        return tuple(
            ArtifactSpec(
                key=key,
                label=label,
                renderer=renderer,
                blueprint=get_blueprint(doc_type) if doc_type else None,
                prompt_blocks=["hard_rules"],
            )
            for key, label, renderer, doc_type in _ARTIFACTS
        )

    def prompt_blocks(self) -> Mapping[str, str]:
        """The vocabulary injected into system prompts.

        `hard_rules` and `error_codes` are what four agents imported directly
        from `core/domain_rules` before this seam existed. The remaining keys feed
        `app.core.prompt_blocks` substitution (PR #8); their values reproduce
        that module's defaults exactly, so routing through the pack changes no
        bytes.
        """
        return {
            "hard_rules": NETWORK_HARD_RULES,
            "api_design_principles": API_DESIGN_PRINCIPLES,
            "error_codes": NETWORK_ERROR_CODE_EXAMPLES,
            "authority": "the Authority",
            "evidence_sources": "Authority / Regulator",
            "evidence_heading": "Retrieved authority corpus evidence",
            "citation_example": "PSPs must revoke within 24h [S2].",
            "reference_kind": "circular",
            "claim_kinds": "regulatory obligation, error-code, API name, or dispute-SLA claim",
            "document_register": "regulated authority document",
            # ── Added for the Phase-A agent-prompt genericisation sweep ────────
            # (prompt_enhancer / deep_researcher / question_generator / canvas /
            # xsd / citations — previously these hardcoded the ecosystem's brand text
            # directly in Python instead of reading it from here).
            "platform_name": "the Network Change Management Platform",
            "domain_name": "the network",
            "ecosystem_description": "The Authority operates the network, a real-time payment infrastructure.",
            "ecosystem_actors": "banks, PSPs, TPAPs",
            "compliance_note": (
                "Any new network feature must comply with the regulator's guidelines and be "
                "backward-compatible with the existing ecosystem (PSPs, banks, TPAPs)."
            ),
            "regulatory_body": "the Regulator",
            "market_comparables": "global payment networks (Visa, Mastercard, Alipay, etc.)",
            "market_context": "the digital payments context",
            # Optional canvas-section extras — this domain has SGF/FRM concepts with no
            # cross-domain analogue; a pack without them simply doesn't supply
            # this key and the section renders without the extra bullets.
            "product_operating_extra": (
                "- **Impact on SGF** — effect on the Settlement Guarantee Fund\n"
                "- **Impact on FRM** — effect on Fraud Risk Management systems"
            ),
            "regulatory_claim_label": "Authority/Regulator guideline",
            "numbered_citation_example": "The per-transaction limit is ₹1 lakh for most banks [3].",
            "corpus_label": "authority corpus",
            "docgen_generic_domain_notes": _DOCGEN_GENERIC_DOMAIN_NOTES,
        }

    def certification(self, key: str | None = None):
        """This domain certifies, and has three harnesses depending on deployment.

        `key` selects one by name (`sim_pack` | `precert` | `cert_agent`) —
        that is the per-change/per-partner choice, made by whoever dispatches.
        `None` returns the deployment default. The pack names the capability;
        the deployment (or the dispatch) picks the implementation. A domain
        with no certification body omits this method entirely rather than
        returning a stub, which is how `certification_of()` reports absence.
        """
        from .certification import default_harness, harness_by_key

        return harness_by_key(key) if key else default_harness()

    def channel(self):
        """The network reaches its partners over A2A: authenticated, bidirectional.

        NOTE what this does NOT cover. Of the 25 A2A task types, 9 are the
        CERTIFICATION protocol (`cert_query`, `cert_test_request`,
        `cert_verdict_notification`, …). Those ride the same wire but they are
        not change communication — they belong to `certification()`, which this
        pack still omits until that harness is wired.

        Keeping them apart matters: forcing certification traffic through the
        partner channel would drag `cflow_id` and `cert_attempt` into the
        generic contract, and a domain with no certifier would inherit fields
        describing a thing it does not have.
        """
        from app.adapters.channel.a2a import A2AChannel

        return A2AChannel()

    def participants(self) -> Sequence[Participant]:
        return (
            Participant(key="authority", label="Authority", is_authority=True),
            Participant(key="psp", label="PSP Bank"),
            Participant(key="issuer", label="Issuer Bank"),
            Participant(key="beneficiary", label="Beneficiary Bank"),
            Participant(key="remitter", label="Remitter Bank"),
        )

    def wire_format(self) -> str:
        """Network messages are XML over HTTP. Read via `wire_format_of(pack)`;
        the certification case builder snapshots this key onto every stored
        assertion row, so the pack is consulted at generation time only."""
        return "xml"

    def combination_rules(self) -> Sequence[CrossFieldRule]:
        """Declared cross-field business rules for request-variant generation.

        Empty TODAY, and empty is the honest answer: this domain's valid business
        combinations (type/channel/mode tuples and their kin) are not yet
        declared anywhere machine-readable, and the generator must report that
        as a coverage gap rather than have this pack guess. Populate from
        approved scenario rules — never from field-level constraints.
        """
        return ()

    def cert_vocabulary(self) -> CertVocabulary:
        """The domain's own words on a certificate.

        These were module constants in `services/cert_orchestrator`, which
        made the engine carry one ecosystem's taxonomy — the scope a role
        certifies for, the product names printed on the certificate, and the
        `PR_`/`PE_` case-id prefixes that partition a suite by role. They are
        the Authority's vocabulary, so they live in the Authority's pack.

        `product_labels` is ORDERED, not a dict: "LITE" and "AUTOPAY" both
        map to the Lite-Autopay product and a flow code may contain both, so
        first-match-wins is the rule and declaration order is what expresses
        it.
        """
        return CertVocabulary(
            role_scopes={
                "PAYER_PSP":        ["ACQUIRER"],
                "PAYEE_PSP":        ["ISSUER"],
                "REMITTER_BANK":    ["ISSUER"],
                "BENEFICIARY_BANK": ["ISSUER"],
            },
            product_labels=[
                ("LITE", "Network -Lite Autopay Issuer"),
                ("AUTOPAY", "Network -Lite Autopay Issuer"),
                ("VOUCHER", "Voucher (Creation + Redemption) - B2C"),
                ("AADHAAR", "AADHAAR"),
                ("REVERSAL", "REVERSAL RC"),
                ("MMID", "MOBILE+MMID"),
            ],
            # Mirrors cert-agent's excel_import.py convention so platform and
            # simulator agree on the role partition.
            role_prefixes={
                "PAYER_PSP":        "PR_",
                "PAYEE_PSP":        "PE_",
                "REMITTER_BANK":    "RE_",
                "BENEFICIARY_BANK": "BE_",
            },
            # Which fields each role legitimately supplies, so a payer-side
            # payload cannot overwrite payee VPAs. A role absent here is not
            # partitioned and passes everything through.
            role_test_data_fields={
                "PAYER_PSP": ["payer_vpa", "payer_account_number",
                              "payer_ifsc", "payer_mobile", "mobile_number"],
                "PAYEE_PSP": ["payee_vpa", "payee_account_number",
                              "payee_ifsc", "merchant_category_code"],
                "REMITTER_BANK": ["payer_vpa", "account_number", "ifsc",
                                  "account_type", "iin"],
                "BENEFICIARY_BANK": ["payee_vpa", "account_number", "ifsc",
                                     "account_type", "iin"],
            },
            # Consolidates what used to be separately hardcoded in
            # question_generator._PARTIES, party_inference._CANONICAL_PARTIES,
            # brd_extractor._FC_PARTIES and services/brd_requirements — all four
            # now build their party choices from `role_scopes`/`role_labels`.
            role_labels={
                "PAYER_PSP":        "Payer PSP",
                "PAYEE_PSP":        "Payee PSP",
                "REMITTER_BANK":    "Remitter Bank",
                "BENEFICIARY_BANK": "Beneficiary Bank",
            },
        )

    def change_operations(self) -> Sequence[LabeledOption]:
        """The network's transaction-lifecycle operations — drives the per-operation
        scope-signal questions (question_generator.build_scope_signal_questions)
        and the feature-criteria extractor's `operation` vocabulary."""
        return (
            LabeledOption(key="init", label="Init (transaction initiation)"),
            LabeledOption(key="auth", label="Auth (authorisation flow)"),
            LabeledOption(key="debit", label="Debit (debit leg on remitter side)"),
            LabeledOption(key="credit", label="Credit (credit leg on beneficiary side)"),
            LabeledOption(key="debit_reversal", label="Debit reversal (unwind a deemed debit)"),
            LabeledOption(key="credit_reversal", label="Credit reversal (unwind a deemed credit)"),
        )

    def risk_levels(self) -> Sequence[LabeledOption]:
        return (
            LabeledOption(key="low", label="Low",
                          description="Minor UI/copy change, no new fields, no money-movement change"),
            LabeledOption(key="standard", label="Standard",
                          description="New fields or logic, existing money-movement flow"),
            LabeledOption(key="high", label="High",
                          description="New money-movement flow, new fraud/security surface, or novel dispute path"),
        )

    def compliance_levels(self) -> Sequence[LabeledOption]:
        # Keys are the literal strings `services/clarification_loader.py` reads
        # and stores as the `compliance_sensitivity` signal — NOT renamed here,
        # to stay behaviour-preserving for whatever downstream consumes them.
        return (
            LabeledOption(key="standard", label="Standard",
                          description="No new regulatory requirement"),
            LabeledOption(key="RBI-mandated", label="RBI-mandated",
                          description="Driven by a specific RBI circular or master direction"),
            LabeledOption(key="PMLA-touched", label="PMLA-touched",
                          description="Anti-money-laundering, KYC uplift, or reporting-adjacent"),
        )
