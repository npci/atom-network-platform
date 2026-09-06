# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Global agentic concurrency cap (A16) — must count RUNNING work, not queued rows.

Regression for the 2026-08-25 UAT outage. `create_run` stamps `status='active'`
at INSERT, before any worker has seen the task, so a cap that counted bare
`status='active'` counted runs that were merely QUEUED — including the very run
the check was gating.

Once N >= cap runs sat undispatched (nothing was consuming the `agentic` queue,
because the worker had been started without `-Q agentic`), every drive task
counted its own idle siblings, deferred via `self.retry(countdown=30)`, and never
started a phase. No run could reach a terminal state to free a slot, and
`max_retries=None` meant the loop never broke: a permanent, self-inflicted
deadlock where the only thing blocking each task was the existence of the others.

The cap must therefore gate on an UNEXPIRED LEASE — the thing that actually marks
a run as consuming a worker slot, a clone, LLM calls and a JVM heap.
"""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 — register every model on Base before create_all
import importlib as _importlib
import pkgutil as _pkgutil
for _m in _pkgutil.iter_modules(__import__("app.models", fromlist=["x"]).__path__):
    _importlib.import_module(f"app.models.{_m.name}")
from app.core.database import Base
from app.models.agentic import AgenticRun
from app.models.base import utcnow
from app.services.celery_tasks import _active_agentic_run_count


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _run(db, *, change_id, lease_owner=None, lease_delta=None, phase="pending",
         status="active", kind="analysis"):
    row = AgenticRun(
        change_request_id=change_id, phase=phase, status=status, kind=kind,
        selected_repo_ids=["r1"], attempts_json={}, lease_owner=lease_owner,
        lease_expires_at=(utcnow() + lease_delta) if lease_delta is not None else None,
    )
    db.add(row)
    db.flush()
    return row


def test_queued_but_undispatched_runs_do_not_consume_the_cap(db):
    """THE deadlock. Seven created-but-never-driven runs (lease-free, exactly what
    an unconsumed queue produces) must count as ZERO against the cap — otherwise
    they block each other forever and the platform never recovers."""
    for i in range(7):
        _run(db, change_id=f"chg-{i}")
    assert _active_agentic_run_count(db) == 0


def test_leased_runs_count(db):
    """Runs a worker actually holds ARE the thing the cap exists to bound."""
    for i in range(3):
        _run(db, change_id=f"chg-{i}", lease_owner=f"w-{i}", lease_delta=timedelta(minutes=5))
    assert _active_agentic_run_count(db) == 3


def test_expired_lease_does_not_count(db):
    """A crashed/redeployed worker leaves a stale lease. That run is not executing —
    `agentic.recover` reclaims it separately — so it must not hold a slot hostage."""
    _run(db, change_id="chg-live", lease_owner="w1", lease_delta=timedelta(minutes=5))
    _run(db, change_id="chg-dead", lease_owner="w2", lease_delta=timedelta(minutes=-5))
    assert _active_agentic_run_count(db) == 1


def test_terminal_runs_do_not_count(db):
    """Even holding an unexpired lease, a terminal run isn't occupying a slot."""
    _run(db, change_id="chg-done", status="completed",
         lease_owner="w1", lease_delta=timedelta(minutes=5))
    _run(db, change_id="chg-cancelled", status="cancelled",
         lease_owner="w2", lease_delta=timedelta(minutes=5))
    assert _active_agentic_run_count(db) == 0


def test_mixed_fleet_counts_only_the_executing_ones(db):
    """End-to-end shape of the outage: a few genuinely running, plus a pile of
    undispatched zombies. Only the running ones may count — with the old query
    this returned 6 and wedged the platform at the default cap of 5."""
    for i in range(2):
        _run(db, change_id=f"live-{i}", lease_owner=f"w-{i}", lease_delta=timedelta(minutes=5))
    for i in range(4):
        _run(db, change_id=f"zombie-{i}")
    assert _active_agentic_run_count(db) == 2
