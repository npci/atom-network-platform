# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pydantic schemas for checkpoint contracts and verdicts.

These are plain data models — no DB, no LLM, no API imports.
Used by contracts.py (definitions), runner.py (Phase 1), and
the verdict store (Phase 1).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .checkpoints import CheckpointId, PolicyMode, VerdictValue
from .hard_fail_catalog import HARD_FAIL_CATALOG


# ── Rubric dimension ─────────────────────────────────────────────────────────

class RubricDimension(BaseModel):
    id: str = Field(..., min_length=1)
    name: str
    description: str
    weight: float = Field(..., ge=0.0)
    minimum_score: float = Field(..., ge=0.0, le=1.0)
    evidence_required: bool = True

    @field_validator("id")
    @classmethod
    def id_no_spaces(cls, v: str) -> str:
        if " " in v:
            raise ValueError(f"Rubric dimension id must not contain spaces: '{v}'")
        return v


# ── Checkpoint contract ───────────────────────────────────────────────────────

class CheckpointContract(BaseModel):
    checkpoint_id: CheckpointId
    display_name: str
    description: str
    from_stage: str
    to_stage: str
    policy_mode: PolicyMode
    rubric_version: str = Field(..., min_length=1)
    required_source_artifacts: list[str] = Field(..., min_length=1)
    required_target_artifacts: list[str] = Field(..., min_length=1)
    rubric_dimensions: list[RubricDimension] = Field(..., min_length=1)
    deterministic_checks: list[str] = Field(default_factory=list)
    hard_fail_codes: list[str] = Field(default_factory=list)
    warn_codes: list[str] = Field(default_factory=list)
    retry_allowed: bool = False
    override_allowed_roles: list[str] = Field(default_factory=list)

    @field_validator("hard_fail_codes", "warn_codes")
    @classmethod
    def codes_must_exist_in_catalog(cls, codes: list[str]) -> list[str]:
        for code in codes:
            if code not in HARD_FAIL_CATALOG:
                raise ValueError(
                    f"Code '{code}' is not in hard_fail_catalog.py. "
                    "Add it there before referencing it in a contract."
                )
        return codes

    @field_validator("required_source_artifacts", "required_target_artifacts")
    @classmethod
    def artifact_lists_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("Artifact list must not be empty.")
        return v

    @model_validator(mode="after")
    def rubric_weights_positive(self) -> CheckpointContract:
        for dim in self.rubric_dimensions:
            if dim.weight < 0:
                raise ValueError(f"Rubric dimension '{dim.id}' has negative weight.")
        return self


# ── Verdict ───────────────────────────────────────────────────────────────────

class EvalVerdict(BaseModel):
    """Structured result produced by the evaluation runner for one checkpoint run.

    Every field needed to reproduce and audit the decision must be present.
    'reasons' is required — a verdict without human-readable reasons is not acceptable.
    """
    checkpoint_id: CheckpointId
    verdict: VerdictValue
    passed: bool
    policy_mode: PolicyMode
    confidence: float = Field(..., ge=0.0, le=1.0)
    scores: dict[str, float] = Field(default_factory=dict)
    hard_fail_codes: list[str] = Field(default_factory=list)
    warn_codes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(..., min_length=1)
    source_artifact_ids: list[str] = Field(default_factory=list)
    target_artifact_ids: list[str] = Field(default_factory=list)
    rubric_version: str
    deterministic_version: str
    critic_model: Optional[str] = None
    judge_model: Optional[str] = None
    latency_ms: int = 0
    retry_recommended: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("reasons")
    @classmethod
    def reasons_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("A verdict must include at least one reason.")
        return v

    @model_validator(mode="after")
    def passed_consistent_with_verdict(self) -> EvalVerdict:
        # PASS → passed=True; FAIL → passed=False; WARN → depends on policy
        if self.verdict == VerdictValue.PASS and not self.passed:
            raise ValueError("verdict=PASS but passed=False — inconsistent.")
        if self.verdict == VerdictValue.FAIL and self.passed:
            raise ValueError("verdict=FAIL but passed=True — inconsistent.")
        return self
