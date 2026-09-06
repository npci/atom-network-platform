# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Stale-push detection: 'pushed once' must never mean 'pushed forever'.

Regression guard for the prod defect where an early approve+push stamped
push_state="pushed", later fix rounds re-froze the manifest (invalidating the
APPROVAL but not the PUSH state), and the final re-consented push silently
skipped every repo and reported success — git kept the older approved state
while the panel showed the new one.

Covers the full matrix: _push_all (fresh / same-hash resume / legacy row /
stale row / mixed repos), the /push endpoint gate, and _push_view's staleness
flag that drives the UI banner. Style mirrors test_approve_xsd_gate.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agents import agentic_orchestrator as O
from app.api import agentic as A


# ── _push_all matrix ───────────────────────────────────────────────────────────

class _DB:
    """Canned per-model rows: AgenticRunRepo queries pop from ``rr_seq`` in repo
    order (one query per repo in _push_all's loop)."""
    def __init__(self, rr_seq):
        self.rr_seq = list(rr_seq)
        self.added = []
    def get(self, model, rid):
        return SimpleNamespace(gitlab_repo="org/repo", gitlab_url=None)
    def query(self, model):
        nxt = self.rr_seq.pop(0) if self.rr_seq else None
        return SimpleNamespace(filter=lambda *a: SimpleNamespace(first=lambda: nxt))
    def add(self, obj): self.added.append(obj)
    def commit(self): pass


def _rr(state="pushed", pushed_hash="H2", branch="old-branch"):
    return SimpleNamespace(push_state=state, pushed_manifest_hash=pushed_hash,
                           branch=branch, base_commit_sha="base1", mr_url=None)


def _man(repos=("repoA",), manifest_hash="H2"):
    return SimpleNamespace(
        manifest_hash=manifest_hash,
        per_repo=[{"repo_id": r, "base_commit_sha": "base1", "shared_branch_name": "feat/x"}
                  for r in repos],
        operations=[{"repo_id": r, "path": f"src/{r}.java", "op": "modify"} for r in repos])


def _patch_push(monkeypatch, pushes, events):
    monkeypatch.setattr(O, "settings", SimpleNamespace(
        gitlab_url="https://gitlab.example", gitlab_token="tok", gitlab_push_token=None))
    monkeypatch.setattr(O, "_git_push_branch",
                        lambda ws, rid, branch, base, auth, clean, paths: (pushes.append(
                            {"repo_id": rid, "branch": branch, "paths": paths}) or "newsha"))
    monkeypatch.setattr(O, "_maybe_open_mr", lambda repo, branch: None)
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append(kind))


def test_fresh_push_records_pushed_manifest_hash(monkeypatch):
    pushes, events = [], []
    _patch_push(monkeypatch, pushes, events)
    db = _DB([None])                                   # no rr row yet
    out = O._push_all(db, "run1", _man(), "feat/x", "ws1")
    assert out["pushed"] is True and len(pushes) == 1
    assert db.added and db.added[0].pushed_manifest_hash == "H2"
    assert out["targets"][0]["branch"] == "feat/x" and out["targets"][0]["commit"] == "newsha"


def test_resume_same_hash_skips_without_repushing(monkeypatch):
    pushes, events = [], []
    _patch_push(monkeypatch, pushes, events)
    out = O._push_all(_DB([_rr(pushed_hash="H2")]), "run1", _man(), "feat/x-2", "ws1")
    assert out["pushed"] is True and pushes == []      # idempotent crash-recovery path
    assert out["targets"][0]["branch"] == "old-branch"


def test_legacy_null_hash_row_is_never_surprise_repushed(monkeypatch):
    pushes, events = [], []
    _patch_push(monkeypatch, pushes, events)
    out = O._push_all(_DB([_rr(pushed_hash=None)]), "run1", _man(), "feat/x-2", "ws1")
    assert out["pushed"] is True and pushes == []


def test_stale_hash_repushes_and_rebinds(monkeypatch):
    pushes, events = [], []
    _patch_push(monkeypatch, pushes, events)
    rr = _rr(pushed_hash="H1")                          # pushed under an OLDER manifest
    out = O._push_all(_DB([rr]), "run1", _man(manifest_hash="H2"), "feat/x-2", "ws1")
    assert len(pushes) == 1 and pushes[0]["branch"] == "feat/x-2"
    assert pushes[0]["paths"] == ["src/repoA.java"]     # stages the CURRENT manifest paths
    assert rr.pushed_manifest_hash == "H2" and rr.branch == "feat/x-2"
    assert "push_superseding" in events
    assert out["targets"][0]["branch"] == "feat/x-2" and out["targets"][0]["commit"] == "newsha"


def test_mixed_repos_stale_pushes_current_skips(monkeypatch):
    # Partial-failure history: repoA pushed under H1 (stale), repoB pushed under H2
    # (current). Only the stale repo re-pushes.
    pushes, events = [], []
    _patch_push(monkeypatch, pushes, events)
    a, b = _rr(pushed_hash="H1", branch="feat/x"), _rr(pushed_hash="H2", branch="feat/x")
    out = O._push_all(_DB([a, b]), "run1", _man(repos=("repoA", "repoB")), "feat/x-2", "ws1")
    assert [p["repo_id"] for p in pushes] == ["repoA"]
    assert a.pushed_manifest_hash == "H2" and b.branch == "feat/x"
    assert sorted(out["repos"]) == ["repoA", "repoB"]


def test_no_git_credentials_still_skips_cleanly(monkeypatch):
    events = []
    monkeypatch.setattr(O, "settings", SimpleNamespace(
        gitlab_url=None, gitlab_token=None, gitlab_push_token=None))
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append(kind))
    out = O._push_all(_DB([]), "run1", _man(), "feat/x", "ws1")
    assert out == {"pushed": False, "skipped": True} and "push_skipped" in events


# ── /push endpoint gate ────────────────────────────────────────────────────────

class _Run:
    def __init__(self):
        self.id = "r1"; self.phase = "completed"; self.status = "completed"
        self.lease_owner = None; self.lease_expires_at = None
        self.change_request_id = "cr1"; self.manifest_hash = "H2"
        self.handoff_json = {}; self.cancel_requested = False
        self._sa_instance_state = SimpleNamespace(session=None)
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None


class _PushDB:
    def __init__(self, man, rr_rows):
        self.man, self.rr_rows = man, rr_rows
    def query(self, model):
        name = getattr(model, "__name__", str(model))
        if "ChangeManifest" in name:
            return SimpleNamespace(filter=lambda *a: SimpleNamespace(
                order_by=lambda *a2: SimpleNamespace(first=lambda: self.man)))
        return SimpleNamespace(filter=lambda *a: SimpleNamespace(all=lambda: self.rr_rows))
    def commit(self): pass


def _push_now(monkeypatch, man, rr_rows, dispatched):
    import sys
    run = _Run()
    # Pin the governance flag OFF (deployments run with it on): these tests cover
    # stale-push semantics, and _PushDB is too minimal to answer the flag-gated
    # active-governance-stage query.
    monkeypatch.setattr(A.settings, "governance_reviews_enabled", False)
    monkeypatch.setattr(A, "_run_or_404", lambda db, rid: run)
    monkeypatch.setattr(A, "_authz_write", lambda run, user: None)
    monkeypatch.setattr(A, "_unresolved_blockers", lambda man: [])
    monkeypatch.setattr("app.agents.agentic_events.emit_event",
                        lambda db, rid, kind, payload=None: None)
    monkeypatch.setitem(sys.modules, "app.services.celery_tasks", SimpleNamespace(
        agentic_push_task=SimpleNamespace(delay=lambda rid: dispatched.append(rid))))
    return A.push_run_now("r1", _PushDB(man, rr_rows), SimpleNamespace(id="u1"))


def _approved_man(h="H2"):
    return SimpleNamespace(approved_at="2026-07-28", manifest_hash=h)


def test_push_now_unapproved_409(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _push_now(monkeypatch, SimpleNamespace(approved_at=None), [], [])
    assert ei.value.status_code == 409 and "not approved" in ei.value.detail


def test_push_now_current_push_409(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _push_now(monkeypatch, _approved_man(), [_rr(pushed_hash="H2")], [])
    assert ei.value.status_code == 409 and "already pushed" in ei.value.detail


def test_push_now_legacy_null_hash_409(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _push_now(monkeypatch, _approved_man(), [_rr(pushed_hash=None)], [])
    assert ei.value.status_code == 409


def test_push_now_stale_push_dispatches_repush(monkeypatch):
    dispatched = []
    out = _push_now(monkeypatch, _approved_man("H2"), [_rr(pushed_hash="H1")], dispatched)
    assert out["push_dispatched"] is True and dispatched == ["r1"]


def test_push_now_never_pushed_dispatches(monkeypatch):
    dispatched = []
    out = _push_now(monkeypatch, _approved_man(), [], dispatched)
    assert out["push_dispatched"] is True and dispatched == ["r1"]


# ── _push_view staleness flag (drives the UI banner) ──────────────────────────

def _view_run(rows, manifest_hash="H2"):
    sess = SimpleNamespace(query=lambda model: SimpleNamespace(
        filter=lambda *a: SimpleNamespace(all=lambda: rows)))
    run = _Run()
    run.manifest_hash = manifest_hash
    run._sa_instance_state = SimpleNamespace(session=sess)
    return run


def test_push_view_states():
    assert A._push_view(_view_run([])) == {"pushed": False, "push_deferred": False, "push_stale": False}
    v = A._push_view(_view_run([_rr(pushed_hash="H2")]))
    assert v["pushed"] is True and v["push_stale"] is False
    v = A._push_view(_view_run([_rr(pushed_hash="H1")]))
    assert v["pushed"] is True and v["push_stale"] is True         # ← the banner case
    v = A._push_view(_view_run([_rr(pushed_hash=None)]))
    assert v["pushed"] is True and v["push_stale"] is False        # legacy row: unknown ≠ stale
