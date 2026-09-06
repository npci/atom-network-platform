# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A superseded (zombie) driver must NEVER write a terminal/phase transition onto the
run it no longer owns (§3).

Regression for the double-drive that FAILED a healthy run: the heartbeat sets `lost`
while `_step` runs, the phase cancel_check turns that into `stopped="cancelled"`, and
`_step` post-processing marks the run FAILED — but by then the NEW driver owns the row.
The guards in `drive_run` (after `_step`, and in the except handler) must roll that
uncommitted terminal write back and exit quietly so the healthy run survives.
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.agents import agentic_orchestrator as O
from app.agents import codegen_preflight


class _FakeDB:
    """Models commit/rollback boundaries so we can assert what was PERSISTED.

    `mark_terminal` mutates the ORM row but does NOT commit — the commit happens back
    in drive_run. So a rollback before that commit must discard the terminal write; we
    model that by reverting run.status to the last committed value on rollback.
    """
    def __init__(self, run):
        self._run = run
        self.commits: list[str] = []          # run.status snapshot at each commit
        self.rollbacks = 0
        self._committed_status = run.status

    def get(self, _model, _rid):
        return self._run

    def refresh(self, _run):
        pass

    def commit(self):
        self.commits.append(self._run.status)
        self._committed_status = self._run.status

    def rollback(self):
        self.rollbacks += 1
        self._run.status = self._committed_status


def _fake_run():
    return SimpleNamespace(
        id="run-z", change_request_id="cr-z", kind="code", phase="code_change",
        status="active", cancel_requested=False, selected_repo_ids=[],
        attempts_json={}, error_code=None, handoff_json={})


def _harness(monkeypatch, run):
    """Wire drive_run's collaborators to lightweight fakes; return the shared `lost` event."""
    lost = threading.Event()
    monkeypatch.setattr(O.S, "acquire_lease", lambda db, rid, owner: True)
    monkeypatch.setattr(O.S, "release_lease", lambda db, rid, owner=None: None)
    monkeypatch.setattr(O.S, "renew_lease", lambda db, rid, owner: True)
    monkeypatch.setattr(O.S, "mark_terminal",
                        lambda db, r, st, error=None: setattr(r, "status", getattr(st, "value", st)))
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(O, "_rehydrate_art", lambda db, r, art: None)
    monkeypatch.setattr(O, "_start_heartbeat", lambda rid, owner: (threading.Event(), lost))
    monkeypatch.setattr(codegen_preflight, "check_dependencies", lambda **k: [])
    return lost


def test_lease_lost_during_step_rolls_back_terminal_write(monkeypatch):
    run = _fake_run()
    db = _FakeDB(run)
    lost = _harness(monkeypatch, run)

    async def zombie_step(db, r, art, model):
        # Heartbeat discovers the lease was reclaimed WHILE this phase ran; the cancelled
        # branch then marks the run FAILED (uncommitted) — exactly the zombie double-drive.
        art["_lease_lost"].set()
        O.S.mark_terminal(db, r, O.AgenticStatus.FAILED, error="cancelled")
    monkeypatch.setattr(O, "_step", zombie_step)

    result = asyncio.run(O.drive_run(db, run.id, intent="x"))

    assert result == {"acquired": False, "lease_lost": True}
    assert "failed" not in db.commits          # the terminal write was NEVER committed
    assert db.rollbacks >= 1                    # it was rolled back
    assert run.status == "active"               # the healthy new owner's run survives


def test_lease_lost_and_step_raises_is_swallowed(monkeypatch):
    # The zombie's own emit_event can hit the (run_id, seq) IntegrityError and RAISE — the
    # except handler must ALSO be lease-aware, not fall through to mark_terminal(FAILED).
    run = _fake_run()
    db = _FakeDB(run)
    lost = _harness(monkeypatch, run)

    async def zombie_step_raises(db, r, art, model):
        art["_lease_lost"].set()
        O.S.mark_terminal(db, r, O.AgenticStatus.FAILED, error="cancelled")
        raise RuntimeError("duplicate key value violates unique constraint agentic_events_run_id_seq")
    monkeypatch.setattr(O, "_step", zombie_step_raises)

    result = asyncio.run(O.drive_run(db, run.id, intent="x"))

    assert result == {"acquired": False, "lease_lost": True}
    assert "failed" not in db.commits
    assert run.status == "active"


def test_step_raises_without_lease_loss_still_fails_the_run(monkeypatch):
    # Control: a GENUINE (non-lease-lost) error must still drive the run to FAILED — the new
    # guards must not swallow real failures.
    run = _fake_run()
    db = _FakeDB(run)
    _harness(monkeypatch, run)                  # `lost` stays clear

    async def real_error(db, r, art, model):
        raise ValueError("compile failed: cannot find symbol")   # non-transient
    monkeypatch.setattr(O, "_step", real_error)

    result = asyncio.run(O.drive_run(db, run.id, intent="x"))

    assert result["acquired"] is True and result.get("lease_lost") is None
    assert "failed" in db.commits               # normal failure path is intact
    assert run.status == "failed"
