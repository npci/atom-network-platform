# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""UAT triage agent — fail-open + honesty invariants.

The two properties that must survive refactors: the agent NEVER raises (a
broken LLM layer degrades to a deterministic summary), and a recorded failure
can never come back labelled "pass" no matter what the model says.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agents import uat_triage


def _run(**kw):
    defaults = dict(change_title="chg", build_log="ok", build_failed=False,
                    test_log="ok", counts={"total": 1, "passed": 1, "failed": 0, "skipped": 0})
    defaults.update(kw)
    return asyncio.run(uat_triage.triage_from_logs(**defaults))


def test_llm_failure_degrades_to_deterministic_summary(monkeypatch):
    async def boom(**_kw):
        raise RuntimeError("provider down")
    monkeypatch.setattr(uat_triage, "call_llm", boom)
    out = _run(counts={"total": 3, "passed": 1, "failed": 2, "skipped": 0})
    assert out["ai"] is False
    assert out["overall"] == "issues_found"
    assert out["next_action"] == "fix_code"
    assert "unavailable" in out["summary"]


def test_unparseable_response_also_fails_open(monkeypatch):
    async def garbled(**_kw):
        return "sorry, here is prose not json"
    monkeypatch.setattr(uat_triage, "call_llm", garbled)
    out = _run()
    assert out["ai"] is False
    assert out["overall"] == "pass"          # nothing failed on record
    assert out["next_action"] == "proceed"


def test_model_cannot_whitewash_a_recorded_failure(monkeypatch):
    async def optimistic(**_kw):
        return json.dumps({"overall": "pass", "summary": "all good!",
                           "findings": [], "next_action": "proceed"})
    monkeypatch.setattr(uat_triage, "call_llm", optimistic)
    out = _run(counts={"total": 2, "passed": 1, "failed": 1, "skipped": 0})
    assert out["overall"] == "issues_found", \
        "a recorded test failure must never surface as an all-clear"


def test_findings_are_clamped_to_the_schema(monkeypatch):
    async def messy(**_kw):
        return json.dumps({
            "overall": "issues_found",
            "summary": "x" * 5000,
            "findings": [
                {"source": "??", "classification": "catastrophe",
                 "evidence": "e" * 9000, "reasoning": "r", "remediation": "m"},
                "not-a-dict",
                {"source": "build", "classification": "code_bug",
                 "test_id": "NLLN-004", "evidence": "line", "reasoning": "r2",
                 "remediation": "m2"},
            ],
            "next_action": "panic",
        })
    monkeypatch.setattr(uat_triage, "call_llm", messy)
    out = _run(counts={"total": 1, "passed": 0, "failed": 1, "skipped": 0})
    assert len(out["findings"]) == 2                       # non-dict dropped
    f0, f1 = out["findings"]
    assert f0["source"] == "test" and f0["classification"] == "env_issue"  # clamped
    assert len(f0["evidence"]) <= 1500 and len(out["summary"]) <= 2000
    assert f1["classification"] == "code_bug" and f1["test_id"] == "NLLN-004"
    assert out["next_action"] == "fix_code"                # invalid → derived


def test_logs_reach_the_model_wrapped_and_sliced(monkeypatch):
    captured = {}

    async def capture(**kw):
        captured.update(kw)
        return json.dumps({"overall": "pass", "summary": "s", "findings": [],
                           "next_action": "proceed"})
    monkeypatch.setattr(uat_triage, "call_llm", capture)
    big = "head-marker\n" + ("x" * 60_000) + "\ntail-marker"
    _run(build_log=big, test_log="PASS T1")
    user = captured["messages"][0]["content"]
    assert "BUILD_AND_DEPLOY_LOG" in user and "UAT_TEST_LOG" in user
    assert "head-marker" in user and "tail-marker" in user
    assert "[middle of log omitted]" in user
    assert captured["agent_name"] == "uat_triage"


def test_slice_keeps_short_logs_whole():
    assert uat_triage._slice_log("short log") == "short log"
    assert uat_triage._slice_log(None) == "(no log recorded)"


def test_uat_triage_is_routed():
    from app.core.llm_router import _AGENT_PURPOSE, Purpose
    assert _AGENT_PURPOSE.get("uat_triage") == Purpose.REASONING


@pytest.mark.parametrize("failed,expected", [(0, "pass"), (2, "issues_found")])
def test_fallback_overall_tracks_recorded_counts(monkeypatch, failed, expected):
    async def boom(**_kw):
        raise RuntimeError("down")
    monkeypatch.setattr(uat_triage, "call_llm", boom)
    out = _run(counts={"total": 2, "passed": 2 - failed, "failed": failed, "skipped": 0})
    assert out["overall"] == expected
