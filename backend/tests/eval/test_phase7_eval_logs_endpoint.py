# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 7 — global Eval Logs endpoint tests.

Calls list_all_eval_verdicts directly with a fake DB session, matching the
pattern used by test_eval_policy_audit.py (no TestClient).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest
from fastapi import HTTPException

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
from tests._optional_stubs import stub_jwt, stub_pgvector

stub_jwt()
stub_pgvector()

from app.api import eval as eval_api  # noqa: E402


@dataclass
class _FakeVerdict:
    id: str
    change_request_id: str
    checkpoint_id: str
    verdict: str
    policy_mode: str
    is_override: bool = False
    from_stage: str = "x"
    to_stage: str = "y"
    passed: bool = True
    confidence: float = 0.9
    scores_json: dict = field(default_factory=dict)
    hard_fail_codes: list = field(default_factory=list)
    warn_codes: list = field(default_factory=list)
    reasons_json: list = field(default_factory=list)
    source_artifact_ids: list = field(default_factory=list)
    target_artifact_ids: list = field(default_factory=list)
    rubric_version: str = "r.v1"
    deterministic_version: str = "d.v1"
    critic_model: str | None = None
    judge_model: str | None = "rule-based.judge.v1"
    latency_ms: int = 12
    retry_recommended: bool = False
    override_actor: str | None = None
    override_reason: str | None = None
    previous_verdict_id: str | None = None
    created_at: object = None

    def to_dict(self):
        return {
            "id": self.id,
            "change_request_id": self.change_request_id,
            "checkpoint_id": self.checkpoint_id,
            "verdict": self.verdict,
            "policy_mode": self.policy_mode,
            "is_override": self.is_override,
            "passed": self.passed,
            "confidence": self.confidence,
            "scores": self.scores_json,
            "hard_fail_codes": self.hard_fail_codes,
            "warn_codes": self.warn_codes,
            "reasons": self.reasons_json,
            "source_artifact_ids": self.source_artifact_ids,
            "target_artifact_ids": self.target_artifact_ids,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "rubric_version": self.rubric_version,
            "deterministic_version": self.deterministic_version,
            "critic_model": self.critic_model,
            "judge_model": self.judge_model,
            "latency_ms": self.latency_ms,
            "retry_recommended": self.retry_recommended,
            "override_actor": self.override_actor,
            "override_reason": self.override_reason,
            "previous_verdict_id": self.previous_verdict_id,
            "created_at": "2026-05-24T12:00:00+00:00",
        }


@dataclass
class _FakeChange:
    id: str
    title: str
    initial_prompt: str = ""


SAMPLE_ROWS = [
    _FakeVerdict("v-1", "cr-A", "initial_to_prompt_enhanced", "PASS", "advisory"),
    _FakeVerdict("v-2", "cr-A", "prompt_to_research",         "WARN", "soft_gate"),
    _FakeVerdict("v-3", "cr-A", "clarification_to_brd",       "FAIL", "hard_gate",
                 hard_fail_codes=["MISSING_REQUIRED_ARTIFACT"]),
    _FakeVerdict("v-4", "cr-A", "clarification_to_brd",       "PASS", "hard_gate",
                 is_override=True, override_actor="admin",
                 override_reason="approve", previous_verdict_id="v-3"),
]


class _FakeQuery:
    """Minimal SQLAlchemy-query-shaped object that records filter operations
    and returns SAMPLE_ROWS / change-request rows based on the model."""

    def __init__(self, model, rows):
        self.model = model
        self._rows = list(rows)

    def filter(self, *conds):
        # We don't simulate column comparisons; tests assert on the API
        # behaviour with pre-filtered fixture data instead. The endpoint
        # passes valid filters through to SQLAlchemy and we exercise the
        # validation branches with bad inputs.
        return self

    def order_by(self, *_a, **_k):
        return self

    def offset(self, _n):
        return self

    def limit(self, _n):
        return self

    def count(self):
        return len(self._rows)

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, verdict_rows, change_rows):
        self._verdicts = verdict_rows
        self._changes = change_rows

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        if name == "EvalVerdict":
            return _FakeQuery(model, self._verdicts)
        if name == "ChangeRequest":
            return _FakeQuery(model, self._changes)
        return _FakeQuery(model, [])


def _call(db, **kwargs):
    return eval_api.list_all_eval_verdicts(
        change_id=kwargs.get("change_id"),
        checkpoint=kwargs.get("checkpoint"),
        verdict=kwargs.get("verdict"),
        policy_mode=kwargs.get("policy_mode"),
        is_override=kwargs.get("is_override"),
        since=kwargs.get("since"),
        limit=kwargs.get("limit", 100),
        offset=kwargs.get("offset", 0),
        db=db,
    )


class TestEvalLogsEndpoint:
    def test_returns_items_with_joined_title(self):
        db = _FakeDb(SAMPLE_ROWS, [_FakeChange(id="cr-A", title="VPA verification feature")])
        result = _call(db)
        assert result["total"] == 4
        assert result["count"] == 4
        assert all(item["change_request_title"] == "VPA verification feature" for item in result["items"])

    def test_includes_offset_limit_in_response(self):
        db = _FakeDb(SAMPLE_ROWS, [_FakeChange("cr-A", "title")])
        result = _call(db, limit=25, offset=10)
        assert result["limit"] == 25
        assert result["offset"] == 10

    def test_override_row_keeps_override_fields(self):
        db = _FakeDb(SAMPLE_ROWS, [_FakeChange("cr-A", "title")])
        result = _call(db)
        overrides = [i for i in result["items"] if i["is_override"]]
        assert len(overrides) == 1
        assert overrides[0]["override_actor"] == "admin"
        assert overrides[0]["previous_verdict_id"] == "v-3"

    def test_invalid_verdict_raises_400(self):
        db = _FakeDb(SAMPLE_ROWS, [_FakeChange("cr-A", "title")])
        with pytest.raises(HTTPException) as exc:
            _call(db, verdict="BANANA")
        assert exc.value.status_code == 400

    def test_invalid_policy_mode_raises_400(self):
        db = _FakeDb(SAMPLE_ROWS, [_FakeChange("cr-A", "title")])
        with pytest.raises(HTTPException) as exc:
            _call(db, policy_mode="banana")
        assert exc.value.status_code == 400

    def test_invalid_since_raises_400(self):
        db = _FakeDb(SAMPLE_ROWS, [_FakeChange("cr-A", "title")])
        with pytest.raises(HTTPException) as exc:
            _call(db, since="not-a-timestamp")
        assert exc.value.status_code == 400

    def test_valid_since_iso_passes(self):
        # No raise; helper just adds a filter — our fake doesn't apply it but
        # we exercise the parsing branch.
        db = _FakeDb(SAMPLE_ROWS, [_FakeChange("cr-A", "title")])
        result = _call(db, since="2026-05-24T00:00:00Z")
        assert result["total"] == 4
