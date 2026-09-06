# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Regression from the aggressive validation pass (defect D3).

`flow_store.save` appends to a JSON `history` column by read-modify-write. Two
sessions that both load the row, both append, and both commit produce a LOST
UPDATE: the second write overwrites the first and a legitimate certification
transition vanishes from the audit trail. A tester reproduced it on real
Postgres 10 times out of 10.

Two layers of coverage, because the unit harness cannot show a real race:

  * always-on: the mechanism is present (the row is locked for update) and the
    insert race is recovered rather than propagated;
  * opt-in: the tester's actual two-session Postgres reproduction, run when
    `CERT_TEST_PG_URL` points at a live database. Without a lock these fail;
    with it they pass.

    CERT_TEST_PG_URL=postgresql://atom:atom@atom-verify-pg:5432/atomdb pytest \\
        tests/services/test_cert_flow_store_concurrency.py
"""
from __future__ import annotations

import inspect as _inspect
import os
import threading

import pytest

from app.services.cert_agent import flow_store
from app.services.cert_agent.flow import FlowState
from app.services.cert_agent.state_machine import Phase, Trigger

CFLOW = "CFLOW-race"
CHANGE, PARTNER = "change-1", "partner-1"


# ── always-on: the mechanism is there ────────────────────────────────────────

def test_the_lookup_statement_really_locks_the_row():
    """Behavioural, NOT a source grep. The first version of this test asserted
    `"with_for_update()" in source` and passed even with the call deleted —
    because the phrase also appears in the module's own docstring. So compile
    the statement the code actually issues and look for the lock in the SQL.
    """
    from sqlalchemy.dialects import postgresql

    captured = {}

    class _SpySession:
        def execute(self, statement):
            captured["stmt"] = statement
            raise _Stop()

    class _Stop(Exception):
        pass

    with pytest.raises(_Stop):
        flow_store._locked_row(_SpySession(), CFLOW)

    sql = str(captured["stmt"].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql.upper(), (
        "flow_store's row lookup does not lock the row — concurrent saves "
        f"will lose history. SQL was: {sql}"
    )


def test_the_lock_precedes_the_read_modify_write():
    """Ordering still matters: locking after the read buys nothing."""
    src = _inspect.getsource(flow_store.save)
    assert "_locked_row(" in src
    assert src.index("_locked_row(") < src.index("row.history = list(row.history or [])")


def test_insert_race_is_retried_into_the_winners_row(monkeypatch):
    """Two savers can both find no row and both insert. The loser gets an
    IntegrityError on the primary key and must append to the winner's row."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.phase_c import CertFlowState

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[CertFlowState.__table__])
    db = sessionmaker(bind=engine)()

    # The 'winner' already created the row.
    db.add(CertFlowState(cflow_id=CFLOW, change_request_id=CHANGE,
                         partner_id=PARTNER, phase="NOT_STARTED",
                         current_round=1, history=[["W", "NOT_STARTED", "t0"]]))
    db.commit()

    real_flush, state = db.flush, {"raised": False}

    def flaky_flush(*a, **kw):
        if not state["raised"]:
            state["raised"] = True
            raise IntegrityError("simulated insert race", None, Exception())
        return real_flush(*a, **kw)

    flow = FlowState(CFLOW)
    flow.fire(Trigger.readiness_declared)          # one pending entry
    monkeypatch.setattr(db, "flush", flaky_flush)
    flow_store.save(db, flow, change_request_id=CHANGE, partner_id=PARTNER,
                    current_round=1)
    monkeypatch.undo()

    row = flow_store.load(db, CFLOW)
    assert [e[0] for e in row.history] == ["W", "readiness_declared"], \
        "the retry clobbered the winner's history instead of appending"
    assert flow.flushed == 1
    db.close()


def test_watermark_does_not_advance_when_the_write_fails():
    """A failed save must leave the entries queued for the next one."""
    flow = FlowState(CFLOW, persist=None)
    flow.fire(Trigger.readiness_declared)
    assert flow.flushed == 0


# ── opt-in: the tester's real two-session Postgres reproduction ──────────────

_PG_URL = os.environ.get("CERT_TEST_PG_URL")
pg_only = pytest.mark.skipif(
    not _PG_URL,
    reason="set CERT_TEST_PG_URL to a live Postgres to run the real race",
)


@pg_only
def test_concurrent_saves_keep_every_transition():
    """THE D3 REPRO: two sessions load the same row and save concurrently.
    Every legitimate transition must survive — 10/10 lost one before the lock.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.phase_c import CertFlowState

    engine = create_engine(_PG_URL)
    Base.metadata.create_all(engine, tables=[CertFlowState.__table__])
    Session = sessionmaker(bind=engine)

    lost = 0
    iterations = 10
    for i in range(iterations):
        cflow = f"{CFLOW}-{i}"
        seed = Session()
        # Clear first: this test is meant to be re-run against the same
        # database, and a leftover row would fail the seed insert with an
        # IntegrityError that LOOKS like the defect but is only test debris.
        seed.query(CertFlowState).filter(CertFlowState.cflow_id == cflow).delete()
        seed.add(CertFlowState(cflow_id=cflow, change_request_id=CHANGE,
                               partner_id=PARTNER, phase="NOT_STARTED",
                               current_round=1, history=[]))
        seed.commit()
        seed.close()

        start = threading.Barrier(2)

        def saver(tag):
            db = Session()
            flow = FlowState(cflow, phase=Phase.NOT_STARTED)
            flow.history.append([tag, "CONFIG_REQUESTED", f"t-{tag}"])
            start.wait(timeout=10)
            try:
                flow_store.save(db, flow, change_request_id=CHANGE,
                                partner_id=PARTNER, current_round=1)
            finally:
                db.close()

        threads = [threading.Thread(target=saver, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        check = Session()
        row = flow_store.load(check, cflow)
        tags = {e[0] for e in (row.history or [])}
        check.close()
        if tags != {"A", "B"}:
            lost += 1

    assert lost == 0, f"{lost}/{iterations} concurrent saves lost flow history"


@pg_only
def test_concurrent_first_saves_do_not_lose_the_insert_race():
    """Same race with NO pre-existing row: both savers insert. One wins the
    primary key; the loser must retry into it, not vanish or raise."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.phase_c import CertFlowState

    engine = create_engine(_PG_URL)
    Base.metadata.create_all(engine, tables=[CertFlowState.__table__])
    Session = sessionmaker(bind=engine)

    cflow = f"{CFLOW}-insert"
    wipe = Session()
    wipe.query(CertFlowState).filter(CertFlowState.cflow_id == cflow).delete()
    wipe.commit()
    wipe.close()

    start, errors = threading.Barrier(2), []

    def saver(tag):
        db = Session()
        flow = FlowState(cflow, phase=Phase.NOT_STARTED)
        flow.history.append([tag, "CONFIG_REQUESTED", f"t-{tag}"])
        start.wait(timeout=10)
        try:
            flow_store.save(db, flow, change_request_id=CHANGE,
                            partner_id=PARTNER, current_round=1)
        except Exception as exc:  # noqa: BLE001 — recorded, asserted below
            errors.append(repr(exc))
        finally:
            db.close()

    threads = [threading.Thread(target=saver, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check = Session()
    row = flow_store.load(check, cflow)
    tags = {e[0] for e in (row.history or [])}
    check.close()
    assert not errors, f"a concurrent first save raised: {errors}"
    assert tags == {"A", "B"}, f"insert race lost a transition: {tags}"
