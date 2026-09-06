# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-0: the persisted flow state and its watermark.

The load-bearing test here is `test_history_accumulates_across_rounds` — the
orchestrator builds a FRESH FlowState per dispatch, so a persistence layer that
diffs against the STORED history length silently drops every round-2
transition. The watermark must count what each instance flushed, and these
tests would fail the naive implementation.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.cert_agent import flow_store
from app.services.cert_agent.flow import FlowState
from app.services.cert_agent.state_machine import Phase, Trigger

CFLOW = "CFLOW-test-0131"
CHANGE, PARTNER = "change-1", "partner-1"


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register models so metadata is complete
    from app.core.database import Base
    from app.models.phase_c import CertFlowState

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[CertFlowState.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _flow(db, *, round_no: int = 1) -> FlowState:
    return FlowState(CFLOW, persist=flow_store.persister(
        db, change_request_id=CHANGE, partner_id=PARTNER, current_round=round_no))


# ── persistence basics ───────────────────────────────────────────────────────

def test_phase_read_back_after_flowstate_destroyed(db_session):
    flow = _flow(db_session)
    flow.fire(Trigger.readiness_declared)
    flow.fire(Trigger.config_submitted)
    del flow

    row = flow_store.load(db_session, CFLOW)
    assert row is not None
    assert row.phase == Phase.CONFIG_RECEIVED.value
    assert row.change_request_id == CHANGE
    assert row.partner_id == PARTNER
    assert row.current_round == 1


def test_history_is_ordered_and_timestamped(db_session):
    flow = _flow(db_session)
    flow.fire(Trigger.readiness_declared)
    flow.fire(Trigger.config_submitted)

    row = flow_store.load(db_session, CFLOW)
    assert [e[0] for e in row.history] == ["readiness_declared", "config_submitted"]
    assert [e[1] for e in row.history] == ["CONFIG_REQUESTED", "CONFIG_RECEIVED"]
    stamps = [datetime.fromisoformat(e[2]) for e in row.history]  # must parse
    assert stamps == sorted(stamps)


def test_unknown_cflow_loads_none(db_session):
    assert flow_store.load(db_session, "CFLOW-never-seen") is None


def test_illegal_trigger_is_not_persisted(db_session):
    """The audit must not show a transition that never happened."""
    flow = _flow(db_session)
    flow.fire(Trigger.all_cases_passed)       # nonsense from NOT_STARTED
    assert flow_store.load(db_session, CFLOW) is None, \
        "an illegal move persisted a row"

    flow.fire(Trigger.readiness_declared)     # flow still works afterwards
    row = flow_store.load(db_session, CFLOW)
    assert [e[0] for e in row.history] == ["readiness_declared"]


# ── failure containment + the watermark ──────────────────────────────────────

def test_failed_write_does_not_abort_and_retries_on_next_save(db_session):
    calls = {"n": 0}
    real = flow_store.persister(db_session, change_request_id=CHANGE,
                                partner_id=PARTNER, current_round=1)

    def flaky(flow):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store down")
        real(flow)

    flow = FlowState(CFLOW, persist=flaky)
    flow.fire(Trigger.readiness_declared)     # persist raises — must not propagate
    assert flow.phase is Phase.CONFIG_REQUESTED
    assert flow_store.load(db_session, CFLOW) is None

    flow.fire(Trigger.config_submitted)       # next save carries the missed entry
    row = flow_store.load(db_session, CFLOW)
    assert [e[0] for e in row.history] == ["readiness_declared", "config_submitted"]


def test_history_accumulates_across_rounds(db_session):
    """THE watermark pin. Round 2's FlowState starts with an empty in-memory
    history against a populated row; `history[len(stored):]` would flush
    nothing and drop all of round 2."""
    r1 = _flow(db_session, round_no=1)
    r1.fire(Trigger.readiness_declared)
    r1.fire(Trigger.config_submitted)
    r1.fire(Trigger.setup_completed)
    r1.fire(Trigger.run_started)

    r2 = _flow(db_session, round_no=2)        # fresh instance, empty history
    r2.fire(Trigger.readiness_declared)
    r2.fire(Trigger.config_submitted)
    r2.fire(Trigger.setup_completed)

    row = flow_store.load(db_session, CFLOW)
    assert len(row.history) == 7, "round 2's transitions were dropped"
    assert [e[0] for e in row.history] == [
        "readiness_declared", "config_submitted", "setup_completed", "run_started",
        "readiness_declared", "config_submitted", "setup_completed",
    ]
    assert row.current_round == 2
    assert row.phase == Phase.SETUP.value      # round 2's latest, overwritten


def test_each_save_advances_the_watermark_not_the_row_diff(db_session):
    """Two fires on one instance flush exactly one new entry each — no
    duplicates from re-appending already-flushed history."""
    flow = _flow(db_session)
    flow.fire(Trigger.readiness_declared)
    flow.fire(Trigger.config_submitted)
    flow.fire(Trigger.setup_completed)

    row = flow_store.load(db_session, CFLOW)
    assert len(row.history) == 3
    assert flow.flushed == 3


# ── model / migration round-trip ─────────────────────────────────────────────

def test_model_round_trips_defaults(db_session):
    from app.models.phase_c import CertFlowState

    db_session.add(CertFlowState(cflow_id="CFLOW-raw", change_request_id=CHANGE,
                                 partner_id=PARTNER))
    db_session.commit()
    row = db_session.get(CertFlowState, "CFLOW-raw")
    assert row.phase == "NOT_STARTED"
    assert row.current_round == 1
    assert row.history == []
    assert row.halted_reason is None
    assert row.created_at is not None
