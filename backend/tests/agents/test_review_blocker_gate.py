# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A blocker-severity review finding must never ship silently.

Two guarantees:
1. A `blocker`-severity finding earns extra fix rounds (agentic_max_blocker_rounds) beyond the
   normal review cap — so a blocker surfaced in the LAST normal round still gets a fix pass.
2. If a blocker survives the budget, the run freezes (diff inspectable) but emits `review_blocked`
   and the push endpoints HARD-REFUSE it unless explicitly overridden.
"""
import asyncio
from types import SimpleNamespace

from app.core.config import settings
from app.agents import agentic_orchestrator as O
from app.models.agentic import AgenticPhase as P
import pytest


@pytest.fixture(autouse=True)
def _force_legacy_reviewer(monkeypatch):
    # This suite tests the LEGACY _phase_review blocker gate; pin the mode so the
    # goal_verifier default does not short-circuit it.
    monkeypatch.setattr(settings, "agentic_reviewer_mode", "legacy", raising=False)


class _Run:
    def __init__(self, phase, review_attempts):
        self.id = "run-1"; self.kind = "full"; self.phase = phase.value
        self.selected_repo_ids = ["repo1"]; self.handoff_json = {}
        self.workspace_run_id = None; self.change_request_id = "cr"
        self.attempts_json = {"review": review_attempts}


def _patch(monkeypatch, events, froze):
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(O.S, "advance", lambda db, run, to: setattr(run, "phase", to.value))
    monkeypatch.setattr(O, "_phase_freeze", lambda db, run, art: froze.append(1))
    monkeypatch.setattr(settings, "agentic_max_review_rounds", 2)
    monkeypatch.setattr(settings, "agentic_max_blocker_rounds", 4)


def _fake_review(blocking, has_blocker, sev="blocker"):
    async def _r(db, run, art, model):
        art["review"] = {"blocking": blocking, "has_blocker": has_blocker,
                         "items": [{"severity": sev, "file": "X.java", "line": 1, "why": "double credit"}]}
        return blocking
    return _r


def _run_review_phase(monkeypatch, *, review_attempts, blocking, has_blocker, sev="blocker"):
    events, froze = [], []
    _patch(monkeypatch, events, froze)
    monkeypatch.setattr(O, "_phase_review", _fake_review(blocking, has_blocker, sev))
    run = _Run(P.REVIEW, review_attempts)
    asyncio.run(O._step(None, run, {"ctx": object(), "intent": "x"}, None))
    return run, events, froze


def test_blocker_gets_extra_rounds_past_normal_cap(monkeypatch):
    # rounds=2 == normal cap, but < blocker cap(4): a blocker still earns a fix round.
    run, events, froze = _run_review_phase(monkeypatch, review_attempts=2, blocking=True, has_blocker=True)
    assert run.phase == P.CODE_CHANGE.value            # looped back to FIX, did not freeze
    assert froze == []
    assert not any(k == "review_blocked" for k, _ in events)


def test_blocker_hard_blocks_when_budget_exhausted(monkeypatch):
    # rounds=4 == blocker cap: out of fix budget with a blocker open → freeze + LOUD signal.
    run, events, froze = _run_review_phase(monkeypatch, review_attempts=4, blocking=True, has_blocker=True)
    assert froze == [1]                                # froze (diff inspectable)
    assert run.phase == P.AWAITING_HUMAN_APPROVAL.value
    rb = [p for k, p in events if k == "review_blocked"]
    assert rb and rb[0]["blockers"] == 1               # surfaced the unresolved blocker


def test_ordinary_blocking_uses_normal_cap_no_loud_block(monkeypatch):
    # error-severity blocking (NOT a blocker) at the normal cap → freeze for human approval,
    # no extra rounds, no review_blocked (unchanged behaviour for lesser findings).
    run, events, froze = _run_review_phase(monkeypatch, review_attempts=2, blocking=True,
                                           has_blocker=False, sev="error")
    assert froze == [1] and run.phase == P.AWAITING_HUMAN_APPROVAL.value
    assert not any(k == "review_blocked" for k, _ in events)


def test_reviewer_gaps_only_park_names_the_review_not_the_code(monkeypatch):
    # A park caused purely by reviewer verdict deficiencies must NOT show the
    # "⛔ blocking finding still open" banner — that tells the human the CODE is
    # broken when the REVIEW failed. Push stays gated (has_blocker=True) either way.
    events, froze = [], []
    _patch(monkeypatch, events, froze)

    async def _r(db, run, art, model):
        art["review"] = {"blocking": False, "has_blocker": True,
                         "items": [{"severity": "blocker", "category": "directive",
                                    "file": None, "line": None, "reviewer_gap": True,
                                    "why": "[D3] NOT VERIFIED — the reviewer did not return a verdict"}]}
        return False

    monkeypatch.setattr(O, "_phase_review", _r)
    run = _Run(P.REVIEW, 1)
    asyncio.run(O._step(None, run, {"ctx": object(), "intent": "x"}, None))
    assert froze == [1] and run.phase == P.AWAITING_HUMAN_APPROVAL.value
    rb = [p for k, p in events if k == "review_blocked"]
    assert rb and rb[0]["reviewer_gaps_only"] is True
    assert "REVIEW failure" in rb[0]["action"] and "⛔" not in rb[0]["action"]


def test_clean_review_freezes_normally(monkeypatch):
    run, events, froze = _run_review_phase(monkeypatch, review_attempts=0, blocking=False, has_blocker=False)
    assert froze == [1] and run.phase == P.AWAITING_HUMAN_APPROVAL.value
    assert not any(k == "review_blocked" for k, _ in events)


def test_p0_blocker_at_16th_position_is_NOT_silently_dropped(monkeypatch):
    """Codex P0 regression test: previously the items array was sliced [:15] over an UNSORTED
    list, so a blocker-severity finding at position 16+ disappeared from ``review.items`` while
    has_blocker stayed True — leaving _unresolved_blockers (which filters items by severity)
    to return an empty list, and the push gate silently shipped the change. The fix sorts
    blockers first and derives has_blocker from what's actually persisted in items, so the
    safety property — 'no blocker is ever silently dropped' — holds at any scale."""
    import asyncio
    from types import SimpleNamespace
    from app.agents import agentic_review, agentic_orchestrator as O

    # 18 blocking findings: 17 are warnings; ONE blocker hides at position 18 in the input order.
    findings = ([SimpleNamespace(severity="warning", category="correctness", why=f"warn{i}",
                                 suggested_fix="x", file="A.java", line=i, blocking=True)
                 for i in range(17)]
                + [SimpleNamespace(severity="blocker", category="security", why="auth bypass",
                                   suggested_fix="add auth", file="Sec.java", line=99, blocking=True)])

    async def _fake_run_review(db, **kw):
        return SimpleNamespace(findings=findings, blocking=True, reviewer_model="claude")
    monkeypatch.setattr(agentic_review, "run_review", _fake_run_review)
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)

    run = SimpleNamespace(id="r", change_request_id="cr", selected_repo_ids=["a"],
                         attempts_json={"review": 0}, workspace_run_id=None)
    art = {"ctx": SimpleNamespace(selected_repo_ids=["a"]), "intent": "x",
           "change_set": SimpleNamespace(plan={"summary": "s", "files": [{"path": "A.java"}]})}
    blocking = asyncio.run(O._phase_review(None, run, art, None))

    items = art["review"]["items"]
    assert blocking is True
    # Pre-fix this was False (blocker fell off the slice). Post-fix the blocker is sorted to
    # the front and ALWAYS makes the 15-cap, so the gate sees it.
    assert art["review"]["has_blocker"] is True
    assert any(it["severity"] == "blocker" and it["file"] == "Sec.java" for it in items), \
        "the blocker MUST be in items so _unresolved_blockers can surface it"
    # And the gate would actually block:
    from app.api.agentic import _unresolved_blockers
    man = SimpleNamespace(review=art["review"])
    assert len(_unresolved_blockers(man)) == 1


def test_sensitive_category_finding_is_treated_as_blocker_even_when_under_graded(monkeypatch):
    """My cf498c93 finding: the reviewer (same model as the implementer) under-graded an
    unauthenticated endpoint as `warning`, so the severity-only gate let it ship. The
    category-aware rule treats a BLOCKING finding in a sensitive category (security/auth/
    financial/regulatory) as a must-not-ship blocker regardless of the model's severity."""
    import asyncio
    from types import SimpleNamespace
    from app.agents import agentic_review, agentic_orchestrator as O

    findings = [SimpleNamespace(severity="warning", category="security", why="unauthenticated cancel endpoint",
                                suggested_fix="add auth", file="FinController.java", line=96, blocking=True)]

    async def _fake(db, **kw):
        return SimpleNamespace(findings=findings, blocking=True, reviewer_model="claude")
    monkeypatch.setattr(agentic_review, "run_review", _fake)
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    run = SimpleNamespace(id="r", change_request_id="cr", selected_repo_ids=["a"],
                         attempts_json={"review": 0}, workspace_run_id=None)
    art = {"ctx": SimpleNamespace(selected_repo_ids=["a"]), "intent": "x",
           "change_set": SimpleNamespace(plan={})}
    asyncio.run(O._phase_review(None, run, art, None))
    # severity is 'warning' but category is 'security' → must-block.
    assert art["review"]["has_blocker"] is True
    from app.api.agentic import _unresolved_blockers
    man = SimpleNamespace(review=art["review"])
    assert len(_unresolved_blockers(man)) == 1   # the push gate would hold it


def test_is_must_block_rule():
    from app.agents.agentic_orchestrator import is_must_block
    assert is_must_block("correctness", "blocker") is True       # blocker severity
    assert is_must_block("security", "warning") is True          # sensitive category
    assert is_must_block("auth", "info") is True
    assert is_must_block("financial", "error") is True
    assert is_must_block("correctness", "warning") is False      # ordinary → not must-block
    assert is_must_block(None, None) is False


def test_unresolved_blockers_helper_filters_severity():
    from app.api.agentic import _unresolved_blockers
    man = SimpleNamespace(review={"has_blocker": True, "items": [
        {"severity": "blocker", "why": "double credit"}, {"severity": "error", "why": "x"}]})
    blk = _unresolved_blockers(man)
    assert len(blk) == 1 and blk[0]["severity"] == "blocker"
    # No blocker flag, or no manifest review → nothing to block on.
    assert _unresolved_blockers(SimpleNamespace(review={"has_blocker": False, "items": []})) == []
    assert _unresolved_blockers(SimpleNamespace(review=None)) == []
