# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Strategist — synthesises the pre-flight CertificationStrategy artifact.

Deterministic. Reads the EnrichedBrief + WorkbookPlan already produced
upstream and folds them into a CertificationStrategy. Zero LLM cost.

BRD/TSD-only refactor: no npci_specs field catalog, no XSD-diff bucketing,
no delta/full-lifecycle branching — the pack is always "TSD-driven".
"""

from __future__ import annotations

from app.excel_testcase_engine.schemas.certification_strategy import (
    CertificationStrategy,
    FieldChange,
)
from app.excel_testcase_engine.schemas.enriched_brief import EnrichedBrief
from app.excel_testcase_engine.schemas.workbook_plan import WorkbookPlan


def _feature_understanding(brief: EnrichedBrief) -> str:
    """One-line understanding derived from the brief."""
    return (
        f"{brief.feature_name}: {brief.original_brief.strip()}"
        if brief.original_brief.strip()
        else brief.feature_name
    ) or "network feature (no brief text)"


def _strategy_narrative(brief: EnrichedBrief) -> str:
    """Explain the shape of the pack — a single BRD/TSD-driven story."""
    apis = ", ".join(brief.apis) if brief.apis else "the APIs named in the TSD"
    return (
        "TSD-driven certification pack. Scenarios come from the TSD "
        "Testing & Verification section; APIs are used verbatim as named "
        f"in the TSD Interface Specification ({apis}); error codes come "
        "from the BRD. No canonical the network grounding is applied — the BRD "
        "and TSD are the sole sources of truth."
    )


def _primary_api_for_stub(apis: list[str]) -> str | None:
    """Prefer the request-side API (Req*); fall back to the first entry."""
    for name in apis:
        if name.startswith("Req"):
            return name
    return apis[0] if apis else None


def _bucket_field_changes(
    brief: EnrichedBrief, plan: WorkbookPlan,
) -> tuple[list[FieldChange], list[FieldChange]]:
    """Split field-level coverage from the plan into (added, modified) buckets.

    BRD/TSD-only: we no longer have an XSD diff to authoritatively bucket
    fields. Every field the plan mentions via ``covers_field`` is reported
    as ``added`` (safe default; reviewers can reclassify from BRD context).
    ``modified`` stays empty. Both lists are deduplicated on (api, field).
    """
    added: dict[tuple[str, str], FieldChange] = {}
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            if not tc.covers_field:
                continue
            api = _primary_api_for_stub(tc.apis)
            if not api:
                continue
            key = (api, tc.covers_field)
            added.setdefault(key, FieldChange(
                api=api, field_name=tc.covers_field,
                change_type="added", description="",
            ))
    return list(added.values()), []


def _collect_business_rules(brief: EnrichedBrief, plan: WorkbookPlan) -> list[str]:
    """Union of covers_business_rule values across the plan. Sorted for stability."""
    rules: set[str] = set()
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            if tc.covers_business_rule:
                rules.add(tc.covers_business_rule.strip())
    return sorted(r for r in rules if r)


def _collect_affected_legs(plan: WorkbookPlan) -> list[str]:
    """Union of message_leg values across the plan. Sorted for stability."""
    legs: set[str] = set()
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            if tc.message_leg:
                legs.add(tc.message_leg)
    return sorted(legs)


def synthesize_strategy(brief: EnrichedBrief, plan: WorkbookPlan) -> CertificationStrategy:
    """Fold EnrichedBrief + WorkbookPlan into a CertificationStrategy.

    Deterministic — same inputs always produce the same output.
    """
    fields_added, fields_modified = _bucket_field_changes(brief, plan)
    affected_apis = sorted(set(brief.apis))

    return CertificationStrategy(
        feature_understanding=_feature_understanding(brief),
        api_classification=brief.api_classification,
        affected_apis=affected_apis,
        affected_message_legs=_collect_affected_legs(plan),
        business_rules=_collect_business_rules(brief, plan),
        fields_added=fields_added,
        fields_modified=fields_modified,
        strategy_narrative=_strategy_narrative(brief),
        coverage_matrix=dict(plan.coverage_audit),
        plan_filename=plan.filename,
        total_test_cases=sum(len(s.test_cases) for s in plan.sheets),
        sheets=[s.name for s in plan.sheets],
    )
