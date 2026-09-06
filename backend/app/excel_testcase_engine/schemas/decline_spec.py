# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pydantic models for the per-feature Decline & Timeout design artifact.

This is the authored, human-approved source of truth for WHICH failure cases a
feature's certification pack must cover. It is produced during the BRD/TSD phase
(agent ``decline_designer``) and consumed deterministically by the cert engine:
one test stub per ``DeclineRow`` (see Phase 4 ``decline_expander``).

Design contract:
- The *list* of declines is authored + approved here; the cert engine never
  invents it. Completeness is therefore structural, not LLM-judged.
- ``failure_type`` reuses the engine's existing CoverageTag vocabulary so a row
  maps 1:1 onto a TestCaseStub.coverage_tag with no translation.
- ``owning_entity`` is the party that PRODUCES the failure; ``observing_entity``
  is the party that must HANDLE it. The two drive the role-sheet split (Phase 5).
- ``excluded`` records failure modes that were considered but are NOT reachable
  for this feature — this is what makes the kept list defensibly minimal.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# Reuses the engine's CoverageTag values (workbook_plan.CoverageTag) so a row
# maps directly onto TestCaseStub.coverage_tag. "deemed" is the uncertain
# outcome of a timeout that needs reconciliation/reversal.
FailureType = Literal["decline", "timeout", "neg_ack", "deemed"]


class DeclineRow(BaseModel):
    """One essential, reachable failure case for one API of the feature."""

    id:                str                 # stable handle, e.g. "DCL-RMT-001"
    api:               str                 # the primary API this failure occurs on
    owning_entity:     str                 # who FAILS (e.g. "Remitter Bank")
    observing_entity:  str = ""            # who must HANDLE it (e.g. "Payer PSP")
    stage:             str                 # flow step, e.g. "debit" / "credit" / "resolve"
    failure_type:      FailureType
    condition:         str                 # precise guard, e.g. "available_balance < amount"
    error_code:        str = ""            # canonical or minted code; "" until Pass 2 assigns it
    is_new_code:       bool = False        # True when Pass 2 minted a code (no catalog fit)
    new_code_def:      str = ""            # required iff is_new_code — one-line code definition
    required_behavior: str = ""            # reject / reverse / retry / reconcile / notify
    reachable:         bool = True         # only reachable rows enter the cert set
    rationale:         str = ""            # why this is essential to THIS feature
    brd_ref:           str = ""            # traceability, e.g. "FR-7"
    tsd_ref:           str = ""            # traceability, e.g. "§4.2"
    schema_gap:        bool = False        # XSD has no field able to signal this (Pass 2 flag)


class ExcludedCandidate(BaseModel):
    """A failure mode considered during design but not reachable for this feature."""

    candidate: str                         # e.g. "Mandate ceiling exceeded (T28)"
    reason:    str                         # e.g. "feature has no mandate leg"


class MintedCode(BaseModel):
    """A new error code designed for this feature (auto-promoted to the catalog)."""

    code:        str                       # e.g. "F01" (feature-minted namespace)
    category:    FailureType
    description: str
    when_to_use: str
    applies_to:  list[str] = Field(default_factory=list)   # starts narrow: [this api]


class FeatureDeclineSpec(BaseModel):
    """The full authored artifact for one change request."""

    feature_id: str
    apis:       list[str] = Field(default_factory=list)
    rows:       list[DeclineRow] = Field(default_factory=list)
    excluded:   list[ExcludedCandidate] = Field(default_factory=list)
    new_codes:  list[MintedCode] = Field(default_factory=list)

    def reachable_rows(self) -> list[DeclineRow]:
        """The rows that actually drive certification (reachable + coded)."""
        return [r for r in self.rows if r.reachable and r.error_code]
