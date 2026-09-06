# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""API-facing schemas for evaluation verdict endpoints."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EvalVerdictResponse(BaseModel):
    id: str
    change_request_id: str
    checkpoint_id: str
    from_stage: str
    to_stage: str
    verdict: str
    passed: bool
    policy_mode: str
    confidence: float | None = None
    scores: dict[str, Any]
    hard_fail_codes: list[str]
    warn_codes: list[str]
    reasons: list[str]
    source_artifact_ids: list[str]
    target_artifact_ids: list[str]
    rubric_version: str
    deterministic_version: str
    critic_model: str | None = None
    judge_model: str | None = None
    latency_ms: int
    retry_recommended: bool
    is_override: bool
    override_actor: str | None = None
    override_reason: str | None = None
    previous_verdict_id: str | None = None
    created_at: str | None = None


class EvalVerdictListResponse(BaseModel):
    change_request_id: str
    checkpoint: str | None = None
    count: int
    verdicts: list[EvalVerdictResponse]


class EvalLatestCheckpointResponse(BaseModel):
    change_request_id: str
    checkpoint: str
    verdict: EvalVerdictResponse | None = None
    message: str | None = None


class EvalLatestByCheckpointResponse(BaseModel):
    change_request_id: str
    checkpoints: dict[str, EvalVerdictResponse | None]


class EvalPolicyEntryResponse(BaseModel):
    checkpoint_id: str
    policy_mode: str
    source: str


class EvalPolicyListResponse(BaseModel):
    policies: list[EvalPolicyEntryResponse]


class EvalPolicyUpdateRequest(BaseModel):
    policies: dict[str, str]
    reason: str = Field(..., min_length=8)
    confirm_production: bool = False
    confirm_text: str | None = None


class EvalPolicyUpdateResponse(BaseModel):
    updated: list[EvalPolicyEntryResponse]


class EvalPolicyAuditEntryResponse(BaseModel):
    id: str
    checkpoint_id: str
    old_policy_mode: str
    new_policy_mode: str
    actor_user_id: str | None = None
    actor_username: str
    reason: str
    app_env: str
    created_at: str | None = None


class EvalPolicyAuditListResponse(BaseModel):
    count: int
    items: list[EvalPolicyAuditEntryResponse]


class EvalOverrideRequest(BaseModel):
    checkpoint_id: str
    reason: str = Field(..., min_length=8)
    previous_verdict_id: str | None = None


class EvalOverrideResponse(BaseModel):
    change_request_id: str
    checkpoint_id: str
    override_verdict: EvalVerdictResponse
