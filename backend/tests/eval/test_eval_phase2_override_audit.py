# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 2 override tests (reason required + audit trail fields)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from app.schemas.eval import EvalOverrideRequest
from app.services.evaluation.checkpoints import PolicyMode
from app.services.evaluation import store


@dataclass
class _PreviousVerdict:
    id: str = "prev-1"
    change_request_id: str = "cr-1"
    checkpoint_id: str = "brd_to_tech_spec"
    from_stage: str = "brd"
    to_stage: str = "tech_spec"
    verdict: str = "FAIL"
    scores_json: dict = field(default_factory=dict)
    reasons_json: list[str] = field(default_factory=lambda: ["Missing mandatory section"])
    source_artifact_ids: list[str] = field(default_factory=lambda: ["brd-1"])
    target_artifact_ids: list[str] = field(default_factory=lambda: ["ts-1"])
    rubric_version: str = "eval-harness.phase0.v1"
    deterministic_version: str = "deterministic.v1"
    critic_model: str | None = None
    judge_model: str | None = "rule-based.judge.v1"


class _FakeEvalVerdict:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    def commit(self):
        return None

    def refresh(self, _row):
        return None

    def rollback(self):
        return None


class TestPhase2Override:
    def test_override_request_requires_reason(self):
        with pytest.raises(ValidationError):
            EvalOverrideRequest(checkpoint_id="brd_to_tech_spec", reason="short")

    def test_override_is_recorded_with_audit_fields(self, monkeypatch):
        db = _FakeDb()
        previous = _PreviousVerdict()
        monkeypatch.setattr(store, "_eval_verdict_model", lambda: _FakeEvalVerdict)

        row = store.save_override_verdict(
            db,
            previous_verdict=previous,
            override_actor="alice",
            override_reason="Risk review approved this rollout for pilot partners.",
            policy_mode=PolicyMode.HARD_GATE,
        )

        assert row is not None
        assert row.is_override is True
        assert row.override_actor == "alice"
        assert row.override_reason.startswith("Risk review approved")
        assert row.previous_verdict_id == previous.id
        assert row.verdict == "PASS"
        assert row.passed is True
