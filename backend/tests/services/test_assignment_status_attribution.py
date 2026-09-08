# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""A forced operator advance must not be recorded as the partner's own work.

F-30, found live on change 8ebc2b48 / partner a1ee36a1 (Live Test Bank). The
cert dispatch endpoint's `advance=true` path walked the assignment
ACCEPTED→APPLIED→TESTED→READY_FOR_CERTIFICATION→CERTIFYING while stamping every
history row `actor_partner_id=<the partner>` and `actor_user_id=NULL`. The
partner had sent one message and had accepted, applied and tested nothing, so
the audit trail asserted four obligations it had not met.

The second half compounds it: the walk parks the assignment at CERTIFYING,
which outranks every negotiation state, so the partner's genuine
CHANGE_ACKNOWLEDGEMENT arriving later loses to the forward-only guard and is
discarded — writing NO history row at all. The real transition vanishes while
the fabricated one survives.
"""
from __future__ import annotations

import contextlib
import logging

import pytest


@contextlib.contextmanager
def capture_logger(name, level=logging.WARNING):
    """Capture records from one logger regardless of propagation.

    Not caplog: the app installs its own logging config and these loggers do
    not reliably propagate to root under a full-suite run, so caplog sees
    nothing and the assertion passes vacuously.
    """
    logger = logging.getLogger(name)
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    sink = _Sink(level=level)
    prev_level, prev_disabled = logger.level, logger.disabled
    logger.addHandler(sink)
    logger.setLevel(level)
    logger.disabled = False
    try:
        yield records
    finally:
        logger.removeHandler(sink)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register models
    from app.core.database import Base
    from app.models.change_request import ChangeRequest
    from app.models.phase_c import (
        AssignmentStatusHistory,
        ChangePartnerAssignment,
        PartnerAgent,
    )

    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            ChangeRequest.__table__,
            PartnerAgent.__table__,
            ChangePartnerAssignment.__table__,
            AssignmentStatusHistory.__table__,
        ],
    )
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed(db, *, change_id="chg-1", partner_id="p-1"):
    from app.models.change_request import ChangeRequest
    from app.models.phase_c import (
        AssignmentStatus,
        ChangePartnerAssignment,
        PartnerAgent,
        PartnerStatus,
    )

    db.add(ChangeRequest(
        id=change_id, initial_prompt="x", created_by="u1", negotiation_version=1,
    ))
    db.add(PartnerAgent(
        id=partner_id, name="Live Test Bank", status=PartnerStatus.ACTIVE,
        endpoint_url="https://partner.invalid",
    ))
    assignment = ChangePartnerAssignment(
        id="a-1", change_request_id=change_id, partner_id=partner_id,
        status=AssignmentStatus.RECEIVED,
    )
    db.add(assignment)
    db.commit()
    return assignment


def _history(db):
    from app.models.phase_c import AssignmentStatusHistory

    return db.query(AssignmentStatusHistory).order_by(
        AssignmentStatusHistory.created_at).all()


# The states the advance=true walk fabricates, in order.
_FORCED_WALK = ["accepted", "applied", "tested", "ready_for_certification",
                "certifying"]


def test_forced_walk_is_never_attributed_to_the_partner(db_session):
    """The operator's forced advance must name the operator, not the bank."""
    from app.services.assignment_status import force_advance_to_certifying

    assignment = _seed(db_session)

    written = force_advance_to_certifying(
        assignment, db_session,
        operator_user_id="demo-operator-01", operator_username="demo")
    db_session.commit()

    assert written == _FORCED_WALK
    rows = _history(db_session)
    assert [r.to_status for r in rows] == _FORCED_WALK

    for row in rows:
        # The regression: this was the partner's id, so "did the partner
        # accept?" answered yes for a row no partner message caused.
        assert row.actor_partner_id is None, (
            f"{row.to_status} attributed to partner {row.actor_partner_id} — "
            "a forced operator advance must never implicate the partner")
        assert row.actor_user_id == "demo-operator-01"
        assert row.reason.startswith("FORCED"), (
            "forced rows must be greppable so readers can exclude them from "
            "'the partner did X' claims")


def test_partner_transition_lost_to_the_guard_is_loud_and_leaves_no_row(
        db_session):
    """After the walk, the partner's real ACCEPTED is dropped — audibly."""
    from app.models.phase_c import AssignmentStatus
    from app.services.assignment_status import set_status

    assignment = _seed(db_session)
    set_status(assignment, AssignmentStatus.CERTIFYING, db_session,
               actor_user_id="demo-operator-01", reason="FORCED by operator demo")
    db_session.commit()
    before = len(_history(db_session))

    with capture_logger("app.services.assignment_status") as records:
        changed = set_status(
            assignment, AssignmentStatus.ACCEPTED, db_session,
            actor_partner_id="p-1",
            reason="change_acknowledgement from partner")
    db_session.commit()

    assert changed is False
    # The partner's real transition leaves NO trace in the table — the only
    # place it can surface is the log, so the log must not be INFO.
    assert len(_history(db_session)) == before
    assert assignment.status == AssignmentStatus.CERTIFYING

    assert records, (
        "a discarded partner transition logged below WARNING is how F-30 "
        "stayed invisible for 47 minutes")
    assert all(r.levelno >= logging.WARNING for r in records)
    msg = records[0].getMessage()
    assert "DISCARDED" in msg and "certifying" in msg and "accepted" in msg


def test_guard_still_blocks_genuine_replays(db_session):
    """The guard's real purpose must survive the logging change."""
    from app.models.phase_c import AssignmentStatus
    from app.services.assignment_status import set_status

    assignment = _seed(db_session)
    set_status(assignment, AssignmentStatus.ACCEPTED, db_session,
               actor_partner_id="p-1", reason="change_acknowledgement")
    set_status(assignment, AssignmentStatus.TESTED, db_session,
               actor_partner_id="p-1", reason="milestone_update")
    db_session.commit()

    # A duplicate/late ACCEPTED must not drag a tested partner backwards.
    assert set_status(assignment, AssignmentStatus.ACCEPTED, db_session,
                      actor_partner_id="p-1",
                      reason="replayed change_acknowledgement") is False
    assert assignment.status == AssignmentStatus.TESTED

    # And an unchanged value is a no-op that writes no history row.
    before = len(_history(db_session))
    assert set_status(assignment, AssignmentStatus.TESTED, db_session,
                      actor_partner_id="p-1", reason="same value") is False
    assert len(_history(db_session)) == before
