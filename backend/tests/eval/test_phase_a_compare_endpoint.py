# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase A Excellence — Slice 4 A/B comparison endpoint tests."""
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
class _FakeChange:
    id: str
    title: str


@dataclass
class _FakeVerdict:
    id: str
    change_request_id: str
    checkpoint_id: str
    verdict: str
    is_override: bool = False
    hard_fail_codes: list = field(default_factory=list)
    warn_codes: list = field(default_factory=list)
    reasons_json: list = field(default_factory=list)
    previous_verdict_id: str | None = None
    critic_model: str | None = None
    latency_ms: int = 100
    created_at: datetime = field(default_factory=lambda: datetime(2026, 5, 25, tzinfo=timezone.utc))


@dataclass
class _FakeBRD:
    change_request_id: str
    content: str = ""
    version: int = 1


@dataclass
class _FakeTS:
    change_request_id: str
    content: str = ""
    version: int = 1


@dataclass
class _FakeCanvas:
    change_request_id: str
    content: str = ""
    version: int = 1


@dataclass
class _FakeResearch:
    change_request_id: str
    combined_report: str = ""
    version: int = 1


@dataclass
class _FakeClar:
    change_request_id: str
    questions: list = field(default_factory=list)
    status: str = ""
    version: int = 1


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)
    def filter(self, *args, **kwargs):
        return self
    def order_by(self, *args, **kwargs):
        return self
    def first(self):
        return self._rows[0] if self._rows else None
    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, **named_rows):
        self._rows = named_rows
    def query(self, model):
        name = model.__name__
        return _Query(self._rows.get(name, []))


def _impact_db(change_id, *, verdicts=None, brd=None, ts=None, canvas=None, research=None, clar=None, change=None):
    return _FakeDb(
        EvalVerdict=verdicts or [],
        BRD=[brd] if brd else [],
        TechSpec=[ts] if ts else [],
        ProductCanvas=[canvas] if canvas else [],
        ResearchOutput=[research] if research else [],
        Clarification=[clar] if clar else [],
        ChangeRequest=[change] if change else [],
    )


class TestImpact:
    def test_zero_verdicts_returns_zeros(self):
        db = _impact_db("cr-empty")
        result = eval_api._compute_change_impact(db, "cr-empty")
        assert result["verdicts"]["total"] == 0
        assert result["overrides"] == 0
        assert result["retry_runs"] == 0
        assert result["reasons_total"] == 0
        assert result["artifact_stats"]["brd_fr_count"] == 0

    def test_counts_verdicts_overrides_retries(self):
        verdicts = [
            _FakeVerdict("v1", "cr-1", "brd_to_tech_spec", "FAIL", hard_fail_codes=["UNMAPPED_REQUIREMENT"], reasons_json=["FR-04 missing"]),
            _FakeVerdict("v2", "cr-1", "brd_to_tech_spec", "PASS", is_override=True, previous_verdict_id="v1", reasons_json=["Override accepted"]),
            _FakeVerdict("v3", "cr-1", "brd_to_tech_spec", "WARN", reasons_json=["minor"], critic_model="critic:openai:gpt-4o-mini"),
        ]
        db = _impact_db("cr-1", verdicts=verdicts)
        r = eval_api._compute_change_impact(db, "cr-1")
        assert r["verdicts"] == {"total": 3, "PASS": 1, "WARN": 1, "FAIL": 1}
        assert r["overrides"] == 1
        assert r["retry_runs"] == 1
        assert r["reasons_total"] == 3
        assert r["critic_runs"] == 1
        assert r["hard_fail_codes_top"][0]["code"] == "UNMAPPED_REQUIREMENT"

    def test_artifact_stats_count_frs_and_error_codes(self):
        brd = _FakeBRD("cr-1", content="FR-01\nFR-02\nFR-03\n")
        ts = _FakeTS("cr-1", content="FR-01 covered\nU30 returned\nZ6 also\n")
        db = _impact_db("cr-1", brd=brd, ts=ts)
        r = eval_api._compute_change_impact(db, "cr-1")
        assert r["artifact_stats"]["brd_fr_count"] == 3
        assert r["artifact_stats"]["tech_spec_fr_count"] == 1
        assert r["artifact_stats"]["tech_spec_error_codes"] == 2


class TestCompare:
    def test_compare_returns_diff(self, monkeypatch):
        a_verdicts = []  # control: harness off, no verdicts
        b_verdicts = [
            _FakeVerdict("v1", "cr-B", "brd_to_tech_spec", "FAIL", hard_fail_codes=["UNMAPPED_REQUIREMENT"], reasons_json=["FR-04 missing"]),
            _FakeVerdict("v2", "cr-B", "brd_to_tech_spec", "PASS", previous_verdict_id="v1", reasons_json=["fixed"]),
        ]

        # Patch _get_change_or_404 so we don't need a real DB row.
        def _stub_get(db, cid):
            return _FakeChange(id=cid, title=f"Title {cid}")
        monkeypatch.setattr(eval_api, "_get_change_or_404", _stub_get)

        # Provide a DB that returns different verdicts depending on a flag —
        # we cheat by alternating between two databases via dispatch.
        a_db = _impact_db("cr-A", verdicts=a_verdicts, brd=_FakeBRD("cr-A", "FR-01"))
        b_db = _impact_db("cr-B", verdicts=b_verdicts, brd=_FakeBRD("cr-B", "FR-01\nFR-02\nFR-03"))

        # Compose: monkeypatch _compute_change_impact to dispatch by change_id
        orig_compute = eval_api._compute_change_impact
        def _dispatch(db, change_id):
            return orig_compute(a_db if change_id == "cr-A" else b_db, change_id)
        monkeypatch.setattr(eval_api, "_compute_change_impact", _dispatch)

        result = eval_api.eval_compare(change_a="cr-A", change_b="cr-B", db=None)
        assert result["a"]["title"] == "Title cr-A"
        assert result["b"]["title"] == "Title cr-B"
        assert result["diff"]["verdicts_total"]["a"] == 0
        assert result["diff"]["verdicts_total"]["b"] == 2
        assert result["diff"]["verdicts_total"]["delta"] == 2
        assert result["diff"]["verdicts_fail"]["delta"] == 1
        assert result["diff"]["retry_runs"]["delta"] == 1

    def test_same_id_raises_400(self, monkeypatch):
        monkeypatch.setattr(eval_api, "_get_change_or_404", lambda db, cid: _FakeChange(cid, "t"))
        with pytest.raises(HTTPException) as exc:
            eval_api.eval_compare(change_a="cr-X", change_b="cr-X", db=None)
        assert exc.value.status_code == 400
