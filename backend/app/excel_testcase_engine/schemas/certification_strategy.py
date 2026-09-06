# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CertificationStrategy artifact (Slice 3 — cert-tc-v2).

The strategy artifact is a per-run pre-flight document — the answer to
the user's "Output Format §1–9" checklist:

  1. Feature Understanding
  2. API Classification
  3. Affected APIs
  4. Affected Message Legs
  5. Business Rules Identified
  6. Fields Added
  7. Fields Modified
  8. Certification Strategy
  9. Coverage Matrix

It is synthesised DETERMINISTICALLY from the EnrichedBrief + WorkbookPlan
after the Planner runs (no additional LLM call). Persisted as
`<artifact_dir>/<job_id>/01b-certification_strategy.json` so a reviewer
can audit the pack shape before opening the xlsx.

The `feature_understanding` and `strategy_narrative` fields are the two
places we'd want an LLM to add prose in a future slice; today they hold
one-line summaries built from the brief.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FieldChange(BaseModel):
    """One field added or modified by the feature."""

    api: str
    field_name: str
    change_type: str = ""    # "added" | "modified" | "" (unspecified)
    description: str = ""


class CertificationStrategy(BaseModel):
    """The 9-section pre-flight strategy document."""

    # §1 — one-paragraph understanding of what the feature does.
    feature_understanding: str = ""

    # §2 — from EnrichedBrief.api_classification.
    api_classification: str = "unknown"

    # §3 — from EnrichedBrief.existing_apis_touched + new_api_names.
    affected_apis: list[str] = Field(default_factory=list)

    # §4 — union of TestCaseStub.message_leg across the plan.
    affected_message_legs: list[str] = Field(default_factory=list)

    # §5 — union of TestCaseStub.covers_business_rule + FieldSpec.business_rule_ref
    # for the touched APIs.
    business_rules: list[str] = Field(default_factory=list)

    # §6 & §7 — sourced from `EnrichedBrief.xsd_diff_summary` when a
    # structured XSD diff is attached (v3 authoritative path); else the
    # strategist falls back to `covers_field` + sparse catalog heuristics.
    fields_added: list[FieldChange] = Field(default_factory=list)
    fields_modified: list[FieldChange] = Field(default_factory=list)

    # §8 — one-paragraph narrative describing the certification approach
    # for this run. Deterministically synthesised from the classification;
    # a future slice may swap in LLM-authored prose.
    strategy_narrative: str = ""

    # §9 — from WorkbookPlan.coverage_audit — {api: {tag: count}}.
    coverage_matrix: dict[str, dict[str, int]] = Field(default_factory=dict)

    # Provenance so a reviewer knows this artifact matches the workbook.
    plan_filename: str = ""
    total_test_cases: int = 0
    sheets: list[str] = Field(default_factory=list)
