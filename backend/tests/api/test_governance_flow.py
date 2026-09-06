# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance flow endpoints + gates — in-memory sqlite, celery dispatch mocked.

Locks in: start's fail-loud validations, the stage-approve blocker/override
contract (deferred-parent approval never dispatches a push), the agentic-complete
Build gate (409 until all_passed; inert with the flag off), and push_run_now's
refusal to race an active stage.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
# Import EVERY model module (several FK targets aren't in app.models.__init__ —
# code_repo, phase_b, partner_agents, …) so create_all sees the full graph.
import importlib as _importlib
import pkgutil as _pkgutil
for _m in _pkgutil.iter_modules(__import__("app.models", fromlist=["x"]).__path__):
    _importlib.import_module(f"app.models.{_m.name}")
from app.agents import governance_orchestrator as G
from app.core.config import settings
from app.core.database import Base
from app.models.agentic import AgenticRun, AgenticRunRepo, ChangeManifest
from app.models.base import utcnow
from app.models.governance_skill import GovernanceSkill
from app.models.user import UserRole

ADMIN = SimpleNamespace(id="u-admin", email="a@npci", role=UserRole.ADMIN)


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "governance_reviews_enabled", True, raising=False)


@pytest.fixture()
def no_celery(monkeypatch):
    """Capture task dispatches instead of touching a broker."""
    calls = {"drive": [], "push": []}
    from app.services import celery_tasks as CT
    monkeypatch.setattr(CT.agentic_drive_task, "delay", lambda *a, **k: calls["drive"].append(a))
    monkeypatch.setattr(CT.agentic_push_task, "delay", lambda *a, **k: calls["push"].append(a))
    return calls


def _skills(db):
    from app.agents import governance_skills as GS
    for st in ("ea", "infosec"):
        content = f"## RULE {st.upper()}-1: t\nb"
        db.add(GovernanceSkill(skill_type=st, version=1, content=content,
                               checksum=GS.checksum(content),
                               rules_json=[{"id": f"{st.upper()}-1", "title": "t"}]))
    db.flush()


def _parent(db, change_id="chg-1", *, pushed=True):
    run = AgenticRun(change_request_id=change_id, phase="completed", status="completed",
                     kind="code", selected_repo_ids=["r1"], attempts_json={},
                     handoff_json={"feature_branch": "atom/x",
                                   **({} if pushed else {"push_deferred": True})})
    db.add(run)
    db.flush()
    man = ChangeManifest(run_id=run.id, manifest_hash="p" * 64, selected_repo_ids=["r1"],
                         per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40,
                                    "shared_branch_name": "atom/x"}],
                         operations=[], verification={}, review={},
                         approved_at=utcnow(), approved_by="u1")
    db.add(man)
    if pushed:
        db.add(AgenticRunRepo(run_id=run.id, repo_id="r1", branch="atom/x",
                              push_state="pushed", pushed_manifest_hash="p" * 64))
    db.flush()
    return run


# ── /governance/start ─────────────────────────────────────────────────────────

def test_start_requires_the_flag(db, monkeypatch):
    # Pin the flag OFF: the container env may set GOVERNANCE_REVIEWS_ENABLED=true,
    # and this test asserts the disabled behaviour, not the environment's default.
    monkeypatch.setattr(settings, "governance_reviews_enabled", False, raising=False)
    from app.api.governance import start_governance
    with pytest.raises(HTTPException) as e:
        start_governance("chg-1", db, ADMIN)
    assert e.value.status_code == 409 and "disabled" in e.value.detail


def test_start_requires_approved_parent_and_both_skills(db, flag_on):
    from app.api.governance import start_governance
    with pytest.raises(HTTPException) as e:
        start_governance("chg-1", db, ADMIN)
    assert "no approved agentic code change" in e.value.detail
    _parent(db)
    with pytest.raises(HTTPException) as e:            # skills missing → fail loud
        start_governance("chg-1", db, ADMIN)
    assert "not uploaded" in e.value.detail


def test_start_creates_ea_stage_and_dispatches(db, flag_on, no_celery):
    from app.api.governance import start_governance
    _skills(db)
    _parent(db)
    out = start_governance("chg-1", db, ADMIN)
    assert out["started"] is True and out["kind"] == "gov_ea"
    assert len(no_celery["drive"]) == 1
    # second click while the stage is active → 409, no duplicate run
    with pytest.raises(HTTPException) as e:
        start_governance("chg-1", db, ADMIN)
    assert "another agentic run is active" in e.value.detail


def test_start_resumes_from_infosec_when_ea_passed(db, flag_on, no_celery):
    from app.api.governance import start_governance
    _skills(db)
    parent = _parent(db)
    ea = AgenticRun(change_request_id="chg-1", phase="completed", status="completed",
                    kind="gov_ea", selected_repo_ids=["r1"], attempts_json={},
                    parent_run_id=parent.id, workspace_run_id=parent.id,
                    handoff_json={"governance": {"stage": "ea", "parent_run_id": parent.id,
                                                 "result": "clean"}})
    db.add(ea)
    db.flush()
    out = start_governance("chg-1", db, ADMIN)
    assert out["started"] is True and out["kind"] == "gov_is"


# ── stage approve (blocker contract + deferred parent) ────────────────────────

def _parked_stage(db, parent, *, has_blocker=False, ops=None):
    run = AgenticRun(change_request_id=parent.change_request_id, phase="awaiting_human_approval",
                     status="active", kind="gov_ea", selected_repo_ids=["r1"], attempts_json={},
                     parent_run_id=parent.id, workspace_run_id=parent.id,
                     handoff_json={"governance": {"stage": "ea", "parent_run_id": parent.id,
                                                  "result": None,
                                                  "skill": {"type": "ea", "version": 1,
                                                            "checksum": "c" * 64}}})
    db.add(run)
    db.flush()
    items = ([{"category": "security", "severity": "blocker", "file": "A.java",
               "why": "hardcoded secret"}] if has_blocker else [])
    man = ChangeManifest(run_id=run.id, manifest_hash="s" * 64, selected_repo_ids=["r1"],
                         per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40,
                                    "shared_branch_name": "atom/x"}],
                         operations=ops or [], verification={},
                         review={"has_blocker": has_blocker, "items": items})
    db.add(man)
    db.flush()
    return run


def _approve(db, run, **kw):
    from app.api.agentic import ApproveRequest, approve_run
    body = ApproveRequest(manifest_hash="s" * 64, **kw)
    return approve_run(run.id, body, db, ADMIN)


def test_stage_approve_blocker_requires_reason(db, flag_on, no_celery):
    _skills(db)
    parent = _parent(db, pushed=False)
    run = _parked_stage(db, parent, has_blocker=True)
    with pytest.raises(HTTPException) as e:
        _approve(db, run)
    assert e.value.status_code == 409                       # blocked without override
    with pytest.raises(HTTPException) as e:
        _approve(db, run, override_blockers=True, override_reason="no")
    assert e.value.status_code == 400                       # reason too short
    out = _approve(db, run, override_blockers=True, override_reason="risk accepted by CISO")
    assert out["approved"] is True
    db.refresh(run)
    assert run.status == "completed"
    assert ((run.handoff_json or {}).get("governance") or {}).get("result") == "overridden"


def test_stage_approve_deferred_parent_never_dispatches_push(db, flag_on, no_celery):
    _skills(db)
    parent = _parent(db, pushed=False)
    run = _parked_stage(db, parent, ops=[{"op": "modify", "repo_id": "r1",
                                          "path": "src/A.java", "content_hash": "f" * 64}])
    out = _approve(db, run)
    assert out["approved"] is True and out.get("push_deferred") is True
    assert no_celery["push"] == []                          # no remote write on deferred parents
    db.refresh(run)
    assert ((run.handoff_json or {}).get("governance") or {}).get("result") == "fixes_approved"
    # InfoSec chained automatically (server-side sequencing)
    assert len(no_celery["drive"]) == 1
    nxt = db.query(AgenticRun).filter_by(kind="gov_is").one()
    assert nxt.parent_run_id == parent.id


def test_stage_approve_pushed_parent_dispatches_stage_push(db, flag_on, no_celery):
    _skills(db)
    parent = _parent(db, pushed=True)
    run = _parked_stage(db, parent, ops=[{"op": "modify", "repo_id": "r1",
                                          "path": "src/A.java", "content_hash": "f" * 64}])
    out = _approve(db, run)
    assert out["approved"] is True
    assert len(no_celery["push"]) == 1                      # push_run → push_stage_fixes route


# ── agentic-complete Build gate ───────────────────────────────────────────────

def _bridge(db, change_id):
    from app.api.phase_b import agentic_complete
    return agentic_complete(change_id, db, ADMIN)


def test_bridge_gate_blocks_until_all_passed(db, flag_on):
    parent = _parent(db)
    with pytest.raises(HTTPException) as e:
        _bridge(db, "chg-1")
    assert e.value.status_code == 409 and "governance reviews must pass" in e.value.detail
    for kind, result in (("gov_ea", "clean"), ("gov_is", "fixes_approved")):
        db.add(AgenticRun(change_request_id="chg-1", phase="completed", status="completed",
                          kind=kind, selected_repo_ids=["r1"], attempts_json={},
                          parent_run_id=parent.id, workspace_run_id=parent.id,
                          handoff_json={"governance": {"stage": G.STAGES[kind]["stage"],
                                                       "parent_run_id": parent.id,
                                                       "result": result}}))
    db.flush()
    out = _bridge(db, "chg-1")
    assert out["current_step"] == "build"


def test_bridge_passes_with_flag_off(db, monkeypatch):
    monkeypatch.setattr(settings, "governance_reviews_enabled", False, raising=False)
    _parent(db)
    assert _bridge(db, "chg-1")["current_step"] == "build"


# ── push_run_now refuses to race an active stage ──────────────────────────────

def test_deferred_push_blocked_while_stage_active(db, flag_on):
    from app.api.agentic import push_run_now
    parent = _parent(db, pushed=False)
    db.add(AgenticRun(change_request_id="chg-1", phase="review", status="active",
                      kind="gov_ea", selected_repo_ids=["r1"], attempts_json={},
                      parent_run_id=parent.id, workspace_run_id=parent.id,
                      handoff_json={"governance": {"stage": "ea", "parent_run_id": parent.id}}))
    db.flush()
    with pytest.raises(HTTPException) as e:
        push_run_now(parent.id, db, ADMIN)
    assert e.value.status_code == 409 and "governance review stage is in progress" in e.value.detail
