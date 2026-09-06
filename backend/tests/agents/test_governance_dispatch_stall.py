# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A governance stage nobody is consuming must say so.

Every other governance failure mode announces itself: a missing rulebook is a
409, a rule FAIL is a blocking finding, a crash is a terminal status with an
error. The one silent mode is a stage that was created and dispatched while no
Celery worker was listening — the row sits at PENDING, status "active", with
`run_created` + `governance_stage_created` and then nothing, indefinitely.

That happened on a live change: a gov_ea run sat pending for hours with no
signal anywhere until the worker was started. These pin the derived flag that
surfaces it.
"""
from __future__ import annotations

import datetime as _dt
import importlib as _importlib
import pkgutil as _pkgutil

import pytest

import app.models  # noqa: F401 — register every model on Base before create_all
for _m in _pkgutil.iter_modules(__import__("app.models", fromlist=["x"]).__path__):
    _importlib.import_module(f"app.models.{_m.name}")

from app.agents import governance_orchestrator as G  # noqa: E402
from app.models.agentic import AgenticRun  # noqa: E402
from app.models.base import utcnow  # noqa: E402


def _run(*, status="active", phase="pending", age_seconds=0):
    return AgenticRun(
        change_request_id="chg-1", kind="gov_ea", status=status, phase=phase,
        selected_repo_ids=["r1"], attempts_json={}, handoff_json={},
        created_at=utcnow() - _dt.timedelta(seconds=age_seconds),
    )


def test_freshly_queued_run_is_not_reported_as_stalled():
    """A stage dispatched a moment ago is normal, not broken."""
    assert G._dispatch_stalled(_run(age_seconds=5)) is None


def test_run_stuck_pending_past_the_threshold_is_reported():
    stalled = G._dispatch_stalled(_run(age_seconds=15 * 60))
    assert stalled is not None
    assert stalled["seconds"] >= 15 * 60
    assert "queue" in stalled["message"].lower()
    assert "worker" in stalled["message"].lower(), "must name the thing to go check"


@pytest.mark.parametrize("phase", ["workspace_ready", "context_ready", "review",
                                   "code_change", "awaiting_human_approval"])
def test_a_run_a_worker_already_touched_is_never_stalled(phase):
    """Past PENDING, a hang is a different problem with its own signals (lease
    expiry, the stall guard, a terminal error) — don't double-report it here."""
    assert G._dispatch_stalled(_run(phase=phase, age_seconds=24 * 3600)) is None


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_runs_are_never_stalled(status):
    assert G._dispatch_stalled(_run(status=status, age_seconds=24 * 3600)) is None


def test_missing_created_at_does_not_raise():
    """Defensive: a half-built row must not break the whole status endpoint."""
    run = _run()
    run.created_at = None
    assert G._dispatch_stalled(run) is None


@pytest.mark.parametrize("age_seconds,expect_stalled", [(5, False), (15 * 60, True)])
def test_naive_created_at_is_handled(age_seconds, expect_stalled):
    """Postgres (timestamptz) returns an AWARE datetime; SQLite returns a NAIVE
    one. Subtracting a naive value from utcnow() raises TypeError and takes the
    entire governance status endpoint down — caught only because the sqlite API
    tests exercise this path."""
    run = _run(age_seconds=age_seconds)
    run.created_at = run.created_at.replace(tzinfo=None)   # what sqlite gives back
    assert (G._dispatch_stalled(run) is not None) is expect_stalled
