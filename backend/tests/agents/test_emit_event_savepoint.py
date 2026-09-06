# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""emit_event savepoint-retry (§3 prod hardening): a (run_id, seq) collision is a benign
double-drive signal — it must be absorbed (retry once) or raised as a CLEAR concurrent-driver
error, WITHOUT aborting the caller's whole transaction (the PendingRollbackError cascade)."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.agentic import AgenticEvent
from app.agents import agentic_events as E

RUN = "run-1"


@pytest.fixture
def session(monkeypatch):
    # Keep the coding-log mirror out of the DB logic under test.
    monkeypatch.setattr(E, "_write_coding_log", lambda *a, **k: None)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    # Create ONLY the events table — the full metadata graph has FKs to models this test
    # doesn't import (code_repos, etc.). SQLite doesn't enforce the run_id FK (pragma off).
    AgenticEvent.__table__.create(engine)
    # The (run_id, seq) unique index ships in migration 0078, not the model — add it via raw
    # DDL (a named SQLAlchemy Index would bind permanently to the shared Table object and
    # collide on the next test's create).
    with engine.begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX uq_agentic_events_run_seq "
                          "ON agentic_events (run_id, seq)"))
    # autoflush OFF so the only flush calls are emit_event's own explicit ones — otherwise
    # the max(seq) SELECT would autoflush and trip the injected failure before the savepoint.
    s = sessionmaker(bind=engine, autoflush=False)()
    yield s
    s.close()


def _flush_raises_n_times(session, n):
    """Wrap session.flush so the first `n` calls raise a UNIQUE IntegrityError, then delegate.
    Restore with session.flush = real."""
    real = session.flush
    state = {"calls": 0, "real": real}

    def wrapped(*a, **k):
        state["calls"] += 1
        if state["calls"] <= n:
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        return real(*a, **k)
    session.flush = wrapped
    return state


def test_happy_path_allocates_monotonic_seq(session):
    e0 = E.emit_event(session, RUN, "a")
    e1 = E.emit_event(session, RUN, "b")
    session.commit()
    assert (e0.seq, e1.seq) == (0, 1)
    assert session.query(AgenticEvent).filter_by(run_id=RUN).count() == 2


def test_single_collision_is_absorbed_and_retried(session):
    E.emit_event(session, RUN, "seed"); session.commit()      # seq 0 committed
    state = _flush_raises_n_times(session, 1)                  # first flush collides, then OK
    ev = E.emit_event(session, RUN, "second")
    session.commit()
    assert state["calls"] >= 2                                 # collided once, then retried
    assert ev.seq == 1
    # exactly ONE second row — the expunge prevented a duplicate pending insert on retry
    assert session.query(AgenticEvent).filter_by(run_id=RUN).count() == 2


def test_persistent_collision_raises_clear_error_without_poisoning_session(session):
    E.emit_event(session, RUN, "seed"); session.commit()
    state = _flush_raises_n_times(session, 99)                 # every flush collides
    with pytest.raises(RuntimeError, match="concurrent|double-drive"):
        E.emit_event(session, RUN, "doomed")
    session.flush = state["real"]                              # restore for the assertion query
    # THE POINT: the outer transaction is NOT poisoned — a normal query still works
    # (pre-fix this raised PendingRollbackError and cascaded into unrelated failures).
    assert session.query(AgenticEvent).filter_by(run_id=RUN).count() == 1
