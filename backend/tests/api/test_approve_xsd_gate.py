# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase-A XSD approval must fail-closed on a schema that failed its authoritative build.

Regression guard: _post_xsd_advance used to DISCARD _phase_verify's result, so a schema
that failed JAXB-generate/javac/install still froze and could be approved — handing Phase B
a non-compiling contract. The build result now sets handoff.xsd_build_failed, and approve_xsd
refuses unless the human explicitly overrides with a logged reason (mirrors the push gate)."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import agentic as A
from app.api.agentic import ApproveRequest


class _Run:
    """Fake run that defaults any field _run_view reads to None."""
    def __init__(self, build_failed):
        self.id = "r1"; self.kind = "xsd"; self.phase = "awaiting_xsd_approval"
        self.change_request_id = "cr1"; self.status = "active"
        self.handoff_json = {"xsd_build_failed": build_failed} if build_failed is not None else {}
        # _run_view → _push_view calls object_session(run); a state with session=None
        # makes it return None so the push-lookup query is skipped.
        self._sa_instance_state = SimpleNamespace(session=None)
    def __getattr__(self, name):        # any other field _run_view touches → None
        if name.startswith("_"):
            raise AttributeError(name)
        return None


def _run(build_failed):
    return _Run(build_failed)


def _patch(monkeypatch, run, events):
    monkeypatch.setattr(A, "_run_or_404", lambda db, rid: run)
    monkeypatch.setattr(A, "_authz_write", lambda run, user: None)
    monkeypatch.setattr("app.agents.manifest.approve", lambda db, rid, h, uid: True)
    monkeypatch.setattr(A.S, "mark_terminal", lambda db, run, status: None)
    monkeypatch.setattr("app.agents.agentic_events.emit_event",
                        lambda db, rid, kind, payload=None: events.append((kind, payload)))


def _call(body):
    class _DB:
        def commit(self): pass
    return A.approve_xsd("r1", body, _DB(), SimpleNamespace(id="u1"))


def test_build_failed_schema_is_refused_without_override(monkeypatch):
    _patch(monkeypatch, _run(True), [])
    with pytest.raises(HTTPException) as ei:
        _call(ApproveRequest(manifest_hash="h"))
    assert ei.value.status_code == 409 and "failed its authoritative build" in ei.value.detail


def test_build_failed_override_requires_a_reason(monkeypatch):
    _patch(monkeypatch, _run(True), [])
    with pytest.raises(HTTPException) as ei:
        _call(ApproveRequest(manifest_hash="h", override_blockers=True, override_reason="short"))
    assert ei.value.status_code == 400 and "override_reason is required" in ei.value.detail


def test_build_failed_override_with_reason_approves_and_logs(monkeypatch):
    events = []
    _patch(monkeypatch, _run(True), events)
    out = _call(ApproveRequest(manifest_hash="h", override_blockers=True,
                               override_reason="accepted for demo, schema fix tracked separately"))
    assert out["approved"] is True
    assert any(k == "xsd_build_override" for k, _ in events)


def test_clean_schema_approves_normally(monkeypatch):
    events = []
    _patch(monkeypatch, _run(False), events)
    out = _call(ApproveRequest(manifest_hash="h"))
    assert out["approved"] is True
    assert not any(k == "xsd_build_override" for k, _ in events)


def test_approval_applies_pending_plan_supersession(monkeypatch):
    # Approving the manifest with a pending supersession must (1) rewrite the inherited
    # approach decision (approach → 'new', reuse directive lifted), (2) clear the pending
    # flag so a retried approval can't double-roll, (3) roll the plan with the
    # refine_supersession changelog kind, (4) announce it via plan_revised.
    events, rolled = [], {}
    run = _run(False)
    run.handoff_json = {
        "approach_decision": {"approach": "reuse",
                              "option": {"title": "Reuse ReqValAdd"}},
        "pending_plan_supersession": {"prior_approach": "reuse", "prior_title": "Reuse ReqValAdd",
                                      "requested": "create a new dedicated verification API",
                                      "new_files": ["core:x/ReqVerifyPayee.xsd"]},
    }
    _patch(monkeypatch, run, events)

    def _fake_roll(db, **kw):
        rolled.update(kw)
        return 4
    monkeypatch.setattr("app.agents.plan_versioning.record_approach_decision_version", _fake_roll)
    out = _call(ApproveRequest(manifest_hash="h"))
    assert out["approved"] is True
    ad = run.handoff_json["approach_decision"]
    assert ad["approach"] == "new"
    assert "no longer applies" in ad["directive"]
    assert ad["superseded_option"] == {"title": "Reuse ReqValAdd"}
    assert "pending_plan_supersession" not in run.handoff_json      # retry-safe
    assert rolled["kind"] == "refine_supersession"
    assert rolled["chosen"]["approach"] == "new"
    pr = next(p for k, p in events if k == "plan_revised")
    assert pr["version"] == 4


def test_supersession_retires_the_now_chosen_path_from_rejected(monkeypatch):
    # The supersession only fires from a reuse/extend decision — i.e. the gate offered a "new
    # API" option and the human rejected it, so it sits in `rejected`. Phase B renders BOTH the
    # directive and the rejected list, so leaving it there tells the code agent to implement the
    # new API and, three lines later, never to implement it.
    events, run = [], _run(False)
    run.handoff_json = {
        "approach_decision": {
            "approach": "reuse",
            "option": {"title": "Reuse ReqValAdd"},
            "evidence": [{"claim": "ReqValAdd already carries the field", "file": "a.xsd"}],
            "rejected": [{"id": "o2", "title": "New dedicated verification API", "approach": "new"},
                         {"id": "o3", "title": "Extend ReqTransfer instead", "approach": "extend"}]},
        "pending_plan_supersession": {"prior_approach": "reuse", "requested": "new API",
                                      "new_files": ["core:x/ReqVerifyPayee.xsd"]},
    }
    _patch(monkeypatch, run, events)
    monkeypatch.setattr("app.agents.plan_versioning.record_approach_decision_version",
                        lambda db, **kw: 4)
    _call(ApproveRequest(manifest_hash="h"))

    ad = run.handoff_json["approach_decision"]
    assert ad["approach"] == "new"
    # The approved path is no longer advertised as rejected; the genuinely-rejected one stays.
    assert [o["id"] for o in ad["rejected"]] == ["o3"]
    assert len(ad["superseded_rejected"]) == 2          # the original is kept on record
    # Gate evidence justified the SUPERSEDED choice — it must not read as grounding this one.
    assert "evidence" not in ad and ad["superseded_evidence"]


def test_supersession_plan_roll_failure_still_approves(monkeypatch):
    # The plan roll is best-effort: if it raises, the approval and the operative handoff
    # rewrite must stand (the rewrite is committed BEFORE the roll), and plan_revised is
    # still emitted (version None).
    events = []
    run = _run(False)
    run.handoff_json = {
        "approach_decision": {"approach": "reuse", "option": {"title": "Reuse X"}},
        "pending_plan_supersession": {"prior_approach": "reuse", "requested": "new API",
                                      "new_files": ["core:x/ReqNew.xsd"]},
    }
    _patch(monkeypatch, run, events)
    def _boom(db, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.agents.plan_versioning.record_approach_decision_version", _boom)
    out = _call(ApproveRequest(manifest_hash="h"))
    assert out["approved"] is True
    assert run.handoff_json["approach_decision"]["approach"] == "new"
    assert "pending_plan_supersession" not in run.handoff_json
    assert next(p for k, p in events if k == "plan_revised")["version"] is None
