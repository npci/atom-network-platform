# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cert scope-signals must be captured before plan ratification even on a PLAN-FIRST run.

Regression: the 12 scope-signal questions were only injected on the clarifications branch,
so whenever the analysis agent went straight to a plan (its strong "decide it yourself"
bias) they evaporated and cert enforcement silently defaulted to permissive. A plan-first
run now pivots to a scope-signals-only gate (once), and the resume reuses the persisted
plan instead of re-running the expensive analysis agent."""
import asyncio
from types import SimpleNamespace

from app.core.config import settings
from app.agents import agentic_orchestrator as O
from app.models.agentic import AgenticPhase as P

_SCOPE_Q = [{"id": "scope_signal::party_payer_psp", "text": "Payer PSP in scope?",
             "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]}]


def _patch(monkeypatch, *, captured, capture_on=True):
    events, advances = [], []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(O.S, "advance", lambda db, run, to: advances.append(to))
    monkeypatch.setattr(O, "_persist_change_analysis", lambda db, run, prop: None)
    monkeypatch.setattr(O, "_scope_signals_captured", lambda db, cid: captured)
    monkeypatch.setattr(settings, "agentic_plan_enforcement_audit", False)
    monkeypatch.setattr(settings, "capture_scope_signals", capture_on)
    # Accept `party_inference`: the orchestrator now passes the cached inference so the
    # "parties involved" multi-select is pre-checked. A zero-arg stub raised TypeError
    # inside the pivot, which swallowed it and advanced the run to plan-approval instead.
    monkeypatch.setattr("app.agents.question_generator.build_scope_signal_questions",
                        lambda party_inference=None: list(_SCOPE_Q))
    return events, advances


def _run(handoff=None):
    return SimpleNamespace(id="r", kind="analysis", phase=P.ANALYZING.value,
                           change_request_id="cr", handoff_json=handoff or {}, selected_repo_ids=["x"])


def _drive(db, run):
    asyncio.run(O._step(db, run, {"ctx": object(), "intent": "x"}, None))


def test_plan_first_run_pivots_to_scope_signals(monkeypatch):
    events, advances = _patch(monkeypatch, captured=False)
    async def fake_analysis(db, run, art, model):
        return {"summary": "add complaint", "functional_plan": {"summary": "add complaint"}}
    monkeypatch.setattr(O, "_phase_analysis", fake_analysis)
    run = _run()
    _drive(None, run)
    assert P.AWAITING_CLARIFICATIONS in advances and P.AWAITING_PLAN_APPROVAL not in advances
    assert run.handoff_json.get("scope_signals_pending") is True
    payload = dict(events)["clarifications_requested"]
    assert payload.get("scope_signals_only") is True
    assert any(q["id"].startswith("scope_signal::") for q in payload["questions"])


def test_plan_first_run_with_signals_captured_goes_straight_to_plan(monkeypatch):
    events, advances = _patch(monkeypatch, captured=True)          # a clarifications round already got them
    async def fake_analysis(db, run, art, model):
        return {"summary": "s", "functional_plan": {"summary": "s"}}
    monkeypatch.setattr(O, "_phase_analysis", fake_analysis)
    run = _run()
    _drive(None, run)
    assert P.AWAITING_PLAN_APPROVAL in advances and P.AWAITING_CLARIFICATIONS not in advances
    assert "plan_proposed" in [k for k, _ in events]
    assert run.handoff_json.get("scope_signals_pending") is None   # never pivoted


def test_capture_disabled_does_not_pivot(monkeypatch):
    events, advances = _patch(monkeypatch, captured=False, capture_on=False)
    async def fake_analysis(db, run, art, model):
        return {"summary": "s", "functional_plan": {"summary": "s"}}
    monkeypatch.setattr(O, "_phase_analysis", fake_analysis)
    run = _run()
    _drive(None, run)
    assert P.AWAITING_PLAN_APPROVAL in advances and P.AWAITING_CLARIFICATIONS not in advances


def test_resume_after_scope_signals_skips_reanalysis(monkeypatch):
    # The whole point of the persisted-plan skip: re-running the analysis agent purely to
    # re-derive a plan the scope answers don't change is pure waste.
    events, advances = _patch(monkeypatch, captured=True)
    async def _boom(db, run, art, model):
        raise AssertionError("re-ran the analysis agent on resume!")
    monkeypatch.setattr(O, "_phase_analysis", _boom)

    class _DB:                       # summary read is try/except → a raising query is fine
        def query(self, *a, **k):
            raise RuntimeError("no db in unit test")
    run = _run(handoff={"scope_signals_pending": True})
    _drive(_DB(), run)
    assert P.AWAITING_PLAN_APPROVAL in advances
    assert run.handoff_json.get("scope_signals_pending") is None   # flag cleared
    assert "plan_proposed" in [k for k, _ in events]
