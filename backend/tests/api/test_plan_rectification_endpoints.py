# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""PM plan rectification is a BINDING ledger entry at both plan gates.

decide-clarifications (optional plan_rectification field) and decide-plan reopen
(free-text feedback) must each write a decision-ledger entry under
question_key='plan_rectification' — so downstream phases receive the PM's functional
choice even if the revised plan under-applies it — and ride the text into the resume
prompt where the rectification clause makes the agent feasibility-check + apply it."""
from types import SimpleNamespace

from app.api import agentic as A
from app.api.agentic import DecideClarificationsRequest, DecidePlanRequest


class _Run:
    """Fake run defaulting any field _run_view reads to None (see test_approve_xsd_gate)."""
    def __init__(self, phase):
        self.id = "r1"; self.kind = "analysis"; self.phase = phase
        self.change_request_id = "cr1"; self.status = "active"
        self.handoff_json = {}
        self._sa_instance_state = SimpleNamespace(session=None)
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None


class _DB:
    def __init__(self, ca=None):
        self._ca = ca
    def commit(self):
        pass
    def query(self, *a, **k):
        db = self
        class _Q:
            def filter(self, *a, **k): return self
            def order_by(self, *a, **k): return self
            def first(self): return db._ca
        return _Q()


def _patch(monkeypatch, run, ledger, resumed):
    monkeypatch.setattr(A, "_run_or_404", lambda db, rid: run)
    monkeypatch.setattr(A, "_authz_analysis", lambda user: None)
    monkeypatch.setattr("app.services.decision_ledger.append_entry",
                        lambda db, cr_id, **kw: ledger.append(kw))
    monkeypatch.setattr(A, "_resume_analysis",
                        lambda db, run, kind, payload: resumed.append((kind, payload)))


def test_clarification_rectification_is_ledgered_and_rides_the_resume(monkeypatch):
    ledger, resumed = [], []
    run = _Run("awaiting_clarifications")
    _patch(monkeypatch, run, ledger, resumed)
    monkeypatch.setattr(A, "_latest_event_payload", lambda db, rid, kind: {
        "questions": [{"id": "q1", "text": "Which flow?",
                       "options": [{"id": "o1", "label": "Ride ReqTransfer"}]}]})
    out = A.decide_clarifications(
        "r1",
        DecideClarificationsRequest(
            answers=[{"question_id": "q1", "chosen_option_id": "o1"}],
            plan_rectification="create a new dedicated verification API instead of reusing X"),
        _DB(), SimpleNamespace(id="u1"))
    assert out["answered"] == 2                       # 1 answer + the rectification line
    rect = next(e for e in ledger if e["question_key"] == "plan_rectification:clarifications:v0")
    assert "binding functional choice" in rect["directive"]
    assert rect["chosen"].startswith("create a new dedicated verification API")
    # The resume prompt carries the rectification with its comply-first framing.
    assert "PLAN RECTIFICATION" in run.handoff_json["clarification_answers"]
    assert resumed and resumed[0][1]["rectified"] is True


def test_clarification_without_rectification_writes_no_extra_entry(monkeypatch):
    ledger, resumed = [], []
    run = _Run("awaiting_clarifications")
    _patch(monkeypatch, run, ledger, resumed)
    monkeypatch.setattr(A, "_latest_event_payload", lambda db, rid, kind: {
        "questions": [{"id": "q1", "text": "Which flow?",
                       "options": [{"id": "o1", "label": "Ride ReqTransfer"}]}]})
    A.decide_clarifications(
        "r1", DecideClarificationsRequest(answers=[{"question_id": "q1", "chosen_option_id": "o1"}]),
        _DB(), SimpleNamespace(id="u1"))
    assert not any(str(e["question_key"]).startswith("plan_rectification") for e in ledger)
    assert "PLAN RECTIFICATION" not in run.handoff_json["clarification_answers"]


def test_plan_reopen_feedback_is_ledgered_as_rectification(monkeypatch):
    ledger, resumed = [], []
    run = _Run("awaiting_plan_approval")
    _patch(monkeypatch, run, ledger, resumed)
    ca = SimpleNamespace(status="awaiting_ratification", version=3)
    out = A.decide_plan(
        "r1", DecidePlanRequest(action="reopen", feedback="make it a new dedicated API"),
        _DB(ca=ca), SimpleNamespace(id="u1"))
    assert out["reopened"] is True
    assert ca.status == "draft"
    assert run.handoff_json["plan_feedback"] == "make it a new dedicated API"
    rect = next(e for e in ledger if e["question_key"] == "plan_rectification:ratify:v3")
    assert "binding functional choice" in rect["directive"]
    assert rect["chosen"] == "make it a new dedicated API"


def test_rectification_keys_are_per_gate_and_per_version(monkeypatch):
    # The ledger supersedes by question_key. A SHARED key made every reopen delete the
    # previous binding rectification — a PM who asks for a new API at clarifications and
    # then reopens over wording would silently lose the API directive. Distinct keys per
    # gate and per plan version accumulate; re-reopening the SAME version still supersedes.
    # The clarifications gate is version-scoped too: a reopen re-drives analysis back
    # through it, and reopen pops clarification_answers from the handoff — the ledger is
    # the only durable carrier, so a later round must not supersede an earlier round's
    # binding directive.
    ledger, resumed = [], []
    run = _Run("awaiting_clarifications")
    _patch(monkeypatch, run, ledger, resumed)
    monkeypatch.setattr(A, "_latest_event_payload", lambda db, rid, kind: {
        "questions": [{"id": "q1", "text": "Which flow?",
                       "options": [{"id": "o1", "label": "Ride ReqTransfer"}]}]})
    A.decide_clarifications(
        "r1",
        DecideClarificationsRequest(answers=[{"question_id": "q1", "chosen_option_id": "o1"}],
                                    plan_rectification="create a new dedicated verification API"),
        _DB(), SimpleNamespace(id="u1"))

    run.phase = "awaiting_plan_approval"
    for v in (3, 4):
        A.decide_plan("r1", DecidePlanRequest(action="reopen", feedback=f"tighten wording v{v}"),
                      _DB(ca=SimpleNamespace(status="awaiting_ratification", version=v)),
                      SimpleNamespace(id="u1"))

    keys = [e["question_key"] for e in ledger if str(e["question_key"]).startswith("plan_rectification")]
    assert keys == ["plan_rectification:clarifications:v0",
                    "plan_rectification:ratify:v3",
                    "plan_rectification:ratify:v4"]
    assert len(set(keys)) == 3      # nothing supersedes anything else


def test_clarification_rectification_key_tracks_plan_version(monkeypatch):
    # After a reopen the re-driven analysis re-asks clarifications against an EXISTING
    # plan version — the second rectification must land under a new key, not supersede
    # the first round's binding directive.
    ledger, resumed = [], []
    run = _Run("awaiting_clarifications")
    _patch(monkeypatch, run, ledger, resumed)
    monkeypatch.setattr(A, "_latest_event_payload", lambda db, rid, kind: {
        "questions": [{"id": "q1", "text": "Which flow?",
                       "options": [{"id": "o1", "label": "Ride ReqTransfer"}]}]})
    req = DecideClarificationsRequest(answers=[{"question_id": "q1", "chosen_option_id": "o1"}],
                                      plan_rectification="create a new dedicated API")
    A.decide_clarifications("r1", req, _DB(), SimpleNamespace(id="u1"))                     # pre-plan round
    run.phase = "awaiting_clarifications"
    A.decide_clarifications("r1", req, _DB(ca=SimpleNamespace(version=2)), SimpleNamespace(id="u1"))
    keys = [e["question_key"] for e in ledger if str(e["question_key"]).startswith("plan_rectification")]
    assert keys == ["plan_rectification:clarifications:v0", "plan_rectification:clarifications:v2"]
