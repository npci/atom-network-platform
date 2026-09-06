# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase A Excellence — Slice 5 eval metrics endpoint tests.

Calls eval_metrics directly with a fake DB session so we don't need the
TestClient + auth stack. Validates aggregation correctness across global
and per-checkpoint dimensions.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    policy_mode: str = "advisory"
    is_override: bool = False
    hard_fail_codes: list = field(default_factory=list)
    warn_codes: list = field(default_factory=list)
    latency_ms: int = 100
    critic_model: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc))


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)
    def filter(self, *args, **kwargs):
        return self
    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows
    def query(self, _model):
        return _Query(self._rows)


def _call(db, **kw):
    return eval_api.eval_metrics(
        since=kw.get("since"),
        until=kw.get("until"),
        checkpoint=kw.get("checkpoint"),
        db=db,
    )


def _sample_rows() -> list[_FakeVerdict]:
    base = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    return [
        _FakeVerdict("v1", "cr-A", "brd_to_tech_spec",     "PASS", latency_ms=120, critic_model="critic:openai:gpt-4o-mini", created_at=base),
        _FakeVerdict("v2", "cr-A", "brd_to_tech_spec",     "FAIL", latency_ms=200, hard_fail_codes=["UNMAPPED_REQUIREMENT"], created_at=base),
        _FakeVerdict("v3", "cr-A", "brd_to_tech_spec",     "PASS", is_override=True, latency_ms=80, created_at=base),
        _FakeVerdict("v4", "cr-A", "clarification_to_brd", "FAIL", latency_ms=150, hard_fail_codes=["MISSING_MANDATORY_SECTION"], created_at=base),
        _FakeVerdict("v5", "cr-A", "prompt_to_research",    "WARN", warn_codes=["CHECK_EXECUTION_ERROR"], latency_ms=300, created_at=base),
    ]


class TestEvalMetricsEndpoint:
    def test_global_counts_match(self):
        db = _FakeDb(_sample_rows())
        result = _call(db)
        g = result["global"]
        assert g["total"] == 5
        assert g["PASS"] == 2
        assert g["WARN"] == 1
        assert g["FAIL"] == 2
        assert g["overrides"] == 1
        assert 0 <= g["critic_share"] <= 1
        # critic_model only on one row -> 1/5 share
        assert abs(g["critic_share"] - 0.2) < 1e-6

    def test_top_hard_fail_codes_sorted(self):
        rows = _sample_rows() + [
            _FakeVerdict("vx", "cr-B", "brd_to_tech_spec", "FAIL", hard_fail_codes=["UNMAPPED_REQUIREMENT"]),
        ]
        db = _FakeDb(rows)
        result = _call(db)
        codes = result["global"]["top_hard_fail_codes"]
        assert codes[0]["code"] == "UNMAPPED_REQUIREMENT"
        assert codes[0]["count"] == 2

    def test_per_checkpoint_breakdown(self):
        db = _FakeDb(_sample_rows())
        result = _call(db)
        bt = next(c for c in result["checkpoints"] if c["checkpoint_id"] == "brd_to_tech_spec")
        assert bt["total"] == 3
        assert bt["PASS"] == 2
        assert bt["FAIL"] == 1
        assert bt["overrides"] == 1
        assert bt["avg_latency_ms"] > 0
        assert isinstance(bt["top_hard_fail_codes"], list)
        assert any(c["code"] == "UNMAPPED_REQUIREMENT" for c in bt["top_hard_fail_codes"])

    def test_invalid_since_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            _call(_FakeDb([]), since="not-iso")
        assert exc.value.status_code == 400

    def test_invalid_until_window_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            _call(
                _FakeDb([]),
                since="2026-05-25T12:00:00Z",
                until="2026-05-25T11:00:00Z",
            )
        assert exc.value.status_code == 400

    def test_empty_db_returns_zero_total(self):
        result = _call(_FakeDb([]))
        assert result["global"]["total"] == 0
        assert result["checkpoints"] == []
        assert result["global"]["top_hard_fail_codes"] == []

    def test_window_metadata_echoed(self):
        result = _call(_FakeDb([]), since="2026-05-25T12:00:00Z", checkpoint="brd_to_tech_spec")
        assert result["window"]["since"] is not None
        assert result["window"]["checkpoint"] == "brd_to_tech_spec"
