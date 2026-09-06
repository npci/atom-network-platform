# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The NLLN domain pack — a library-loan change-management ecosystem.

Deliberately shaped like `app.packs.network.pack.NetworkPack` (same artifact keys,
same accessor methods populated) so the parallel is easy to read: swap the
words, keep the shape. `certification()` and `channel()` are OMITTED on
purpose — this pack proves out Phase A (research/canvas/BRD/tech-spec/XSD)
genericity; a library consortium's certification harness and partner
transport are a separate, unstarted piece of work, and omitting the methods
is how this pack says so truthfully rather than stubbing them.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from app.core.domain.contract import (
    ArtifactSpec, CertVocabulary, ChangeType, CrossFieldRule, LabeledOption,
    Participant,
)

from .rules import NLLN_ERROR_CODE_EXAMPLES, NLLN_HARD_RULES

# Same four document types NetworkPack declares — the section STRUCTURE
# (`app.agents.blueprints`) is platform document machinery, not payments
# content; only its section-body wording is UPI-flavoured today (a known,
# separate follow-up — see the genericisation punch list).
_ARTIFACTS: tuple[tuple[str, str, str, str | None], ...] = (
    ("canvas",     "Product Canvas",              "markdown", "canvas"),
    ("brd",        "Business Requirement Document", "docx",   "brd"),
    ("tech_spec",  "Technical Specification",     "docx",     "tech_spec"),
    ("xsd",        "XSD Schema",                  "markdown", "xsd"),
)


class NllnPack:
    """Satisfies `app.core.domain.contract.DomainPack` structurally."""

    key = "nlln"
    version = "1.0"

    def change_types(self) -> Sequence[ChangeType]:
        artifact_keys = [a[0] for a in _ARTIFACTS]
        return (
            ChangeType(
                key="new_feature",
                label="New library-loan feature",
                artifacts=artifact_keys,
                requires_certification=False,
                requires_publication=True,
            ),
            ChangeType(
                key="spec_amendment",
                label="Specification amendment",
                artifacts=["brd", "tech_spec"],
                requires_certification=False,
                requires_publication=True,
            ),
        )

    def artifacts(self) -> Sequence[ArtifactSpec]:
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
        return {
            "hard_rules": NLLN_HARD_RULES,
            "api_design_principles": "",
            "error_codes": NLLN_ERROR_CODE_EXAMPLES,
            "authority": "NLLC",
            "evidence_sources": "NLLC / Library Consortium Policy",
            "evidence_heading": "Retrieved NLLC corpus evidence",
            "citation_example": "Participant libraries must confirm a hold within 48h [S2].",
            "reference_kind": "policy circular",
            "claim_kinds": "consortium policy obligation, error-code, API name, or SLA claim",
            "document_register": "NLLC-governed specification document",
            "platform_name": "the NLLC Library-Loan Change Management Platform",
            "domain_name": "NLLN",
            "ecosystem_description": (
                "NLLC operates the inter-library loan switch connecting member "
                "libraries so patrons can borrow items held by any participant."
            ),
            "ecosystem_actors": "member libraries, NLLC, patrons",
            "compliance_note": (
                "Any new library-loan feature must comply with NLLC consortium "
                "policy and be backward-compatible with the existing participant "
                "network (lending libraries, borrowing libraries)."
            ),
            "regulatory_body": "NLLC",
            "market_comparables": "comparable library consortia and ILS vendors (Ex Libris, OCLC, Koha, etc.)",
            "market_context": "the inter-library resource-sharing context",
            # No SGF/FRM-style fund analogue in this domain — legitimately empty.
            "product_operating_extra": "",
            "regulatory_claim_label": "NLLC policy circular",
            "numbered_citation_example": "The standard loan period is 14 days for most participants [3].",
            "corpus_label": "NLLC corpus",
        }

    def participants(self) -> Sequence[Participant]:
        return (
            Participant(key="nllc", label="NLLC", is_authority=True),
            Participant(key="lending_library", label="Lending Library"),
            Participant(key="borrowing_library", label="Borrowing Library"),
            Participant(key="patron", label="Patron"),
        )

    def change_operations(self) -> Sequence[LabeledOption]:
        """The loan lifecycle's operations — mirrors NetworkPack.change_operations()
        for the scope-signal question fan-out."""
        return (
            LabeledOption(key="reserve", label="Reserve (place a hold on an item)"),
            LabeledOption(key="issue", label="Issue (check the item out to a patron)"),
            LabeledOption(key="renew", label="Renew (extend an active loan)"),
            LabeledOption(key="close", label="Close (return / close out a loan)"),
        )

    def risk_levels(self) -> Sequence[LabeledOption]:
        return (
            LabeledOption(key="low", label="Low",
                          description="Minor UI/copy change, no new fields, no loan-state change"),
            LabeledOption(key="standard", label="Standard",
                          description="New fields or logic, existing loan-lifecycle flow"),
            LabeledOption(key="high", label="High",
                          description="New loan-lifecycle flow, new fraud/abuse surface, or novel dispute path"),
        )

    def compliance_levels(self) -> Sequence[LabeledOption]:
        return (
            LabeledOption(key="standard", label="Standard",
                          description="No new consortium policy requirement"),
            LabeledOption(key="NLLC-mandated", label="NLLC-mandated",
                          description="Driven by a specific NLLC policy circular"),
            LabeledOption(key="patron-data-touched", label="Patron-data-touched",
                          description="Patron PII, borrowing history, or reporting-adjacent"),
        )

    def combination_rules(self) -> Sequence[CrossFieldRule]:
        return ()

    def cert_vocabulary(self) -> CertVocabulary:
        return CertVocabulary(
            role_scopes={
                "LENDING_LIBRARY":   ["PARTICIPANT"],
                "BORROWING_LIBRARY": ["PARTICIPANT"],
            },
            role_prefixes={
                "LENDING_LIBRARY":   "LL_",
                "BORROWING_LIBRARY": "BL_",
            },
            role_labels={
                "LENDING_LIBRARY":   "Lending Library",
                "BORROWING_LIBRARY": "Borrowing Library",
            },
            role_test_data_fields={
                "LENDING_LIBRARY":   ["item_id", "lending_library_id", "loan_period_days"],
                "BORROWING_LIBRARY": ["patron_id", "borrowing_library_id"],
            },
        )
