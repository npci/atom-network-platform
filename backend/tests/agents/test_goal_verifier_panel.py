# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Panel runner — skeptic-0 short-circuit + aggregation (skeptic loop stubbed)."""
import asyncio
from types import SimpleNamespace

import pytest

from app.agents import agentic_goal_verifier as GV
from app.agents import goal_verifier_core as core
from app.core.config import settings


class _Ctx:
    selected_repo_ids = ["r1"]


def _stub_skeptics(monkeypatch, verdicts_by_idx):
    calls = []

    async def _fake(*, skeptic_idx, **kw):
        calls.append(skeptic_idx)
        return verdicts_by_idx[skeptic_idx]

    monkeypatch.setattr(GV, "_run_one_skeptic", _fake)
    monkeypatch.setattr(GV, "_render_diff", lambda *a, **k: "DIFF")
    monkeypatch.setattr(GV, "get_model", lambda *_: "claude-x")
    return calls


def _v(idx, refuted, conf="medium", blocking=core.Blocking.NONE):
    return core.SkepticVerdict(refuted=refuted, evidence=f"e{idx}", confidence=conf,
                               skeptic_idx=idx, blocking=blocking,
                               findings=[core.VerifierFinding("gap", "F.java:1", "d")] if refuted else [])


def _run(monkeypatch):
    return asyncio.run(GV.run_goal_verifier(
        None, run_id="run1", ctx=_Ctx(), change_set=SimpleNamespace(operations=[]),
        intent="do x", plan_block="PLAN"))


def test_skeptic0_high_conf_refute_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "agentic_verifier_panel_size", 3)
    calls = _stub_skeptics(monkeypatch, {0: _v(0, True, "high"), 1: _v(1, False), 2: _v(2, False)})
    r = _run(monkeypatch)
    assert calls == [0]                                  # skeptics 1,2 never spawned
    assert r.outcome is core.Outcome.NOT_ACHIEVED


def test_blocking_skeptic0_refute_does_NOT_short_circuit(monkeypatch):
    # C3: a high-conf BLOCKING refute (unverifiable/contradiction) must fan out the full
    # panel so aggregation can distinguish BLOCKED from a co-occurring fixable gap.
    monkeypatch.setattr(settings, "agentic_verifier_panel_size", 3)
    calls = _stub_skeptics(monkeypatch, {
        0: _v(0, True, "high", blocking=core.Blocking.UNVERIFIABLE),
        1: _v(1, False), 2: _v(2, False)})
    _run(monkeypatch)
    assert sorted(calls) == [0, 1, 2]                    # NOT short-circuited


def test_full_panel_runs_when_skeptic0_passes(monkeypatch):
    monkeypatch.setattr(settings, "agentic_verifier_panel_size", 3)
    calls = _stub_skeptics(monkeypatch, {0: _v(0, False), 1: _v(1, False), 2: _v(2, False)})
    r = _run(monkeypatch)
    assert sorted(calls) == [0, 1, 2]
    assert r.outcome is core.Outcome.ACHIEVED


def test_full_panel_cold_majority_refutes(monkeypatch):
    monkeypatch.setattr(settings, "agentic_verifier_panel_size", 3)
    _stub_skeptics(monkeypatch, {0: _v(0, False), 1: _v(1, True), 2: _v(2, True)})
    r = _run(monkeypatch)
    assert r.outcome is core.Outcome.NOT_ACHIEVED and r.gaps


def test_panel_size_one(monkeypatch):
    monkeypatch.setattr(settings, "agentic_verifier_panel_size", 1)
    calls = _stub_skeptics(monkeypatch, {0: _v(0, True, "high")})
    r = _run(monkeypatch)
    assert calls == [0] and r.outcome is core.Outcome.NOT_ACHIEVED
