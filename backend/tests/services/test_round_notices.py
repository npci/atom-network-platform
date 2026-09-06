# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Round lifecycle events emitted by the sync helpers in negotiation_extended.

Exercises the pure DB logic — create_round_state / advance_round_no_change /
apply_silent_acceptances now return round-transition events that async callers
fan out over A2A as round_opened / round_closed. These tests don't touch the
actual A2A send path (unit-tested elsewhere via send_task_to_partner mocks);
they just guarantee the events are shaped and ordered correctly so downstream
handlers can rely on them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def db_session(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    # Short round window so silent-acceptance triggers deterministically in tests.
    monkeypatch.setenv("NEGOTIATION_ROUND_MINUTES", "1")
    monkeypatch.setenv("NEGOTIATION_MAX_ROUNDS", "2")

    import app.models  # noqa: F401 — register models
    from app.core.database import Base
    from app.models.change_request import ChangeRequest
    from app.models.phase_c import (
        CounterProposal,
        NegotiationRoundState,
        PartnerAgent,
    )

    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            ChangeRequest.__table__,
            PartnerAgent.__table__,
            NegotiationRoundState.__table__,
            CounterProposal.__table__,
        ],
    )
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed(db, change_id="chg-1", partner_id="p-1"):
    from app.models.change_request import ChangeRequest
    from app.models.phase_c import PartnerAgent

    db.add(ChangeRequest(
        id=change_id, initial_prompt="x", created_by="u1", negotiation_version=1,
    ))
    # No is_active: PartnerAgent tracks liveness via `status` (PartnerStatus enum,
    # defaults to ACTIVE) — passing is_active raised TypeError at construction, so
    # every test in this file errored before its body ran.
    db.add(PartnerAgent(
        id=partner_id, name="Bank X", endpoint_url="http://example",
        api_key="k",
    ))
    db.commit()


# ── create_round_state returns (state, was_created) ───────────────────────────

def test_create_round_state_flags_new_vs_idempotent(db_session):
    from app.services.negotiation_extended import create_round_state
    _seed(db_session)
    state, was_created = create_round_state("chg-1", "p-1", 1, db_session)
    assert was_created is True
    assert state.round_number == 1

    state2, was_created2 = create_round_state("chg-1", "p-1", 1, db_session)
    assert was_created2 is False
    assert state2.id == state.id


# ── advance_round_no_change: closed event + opened event, or frozen ───────────

def test_advance_round_no_change_emits_close_then_open(db_session):
    from app.services.negotiation_extended import (
        advance_round_no_change,
        create_round_state,
    )
    _seed(db_session)
    create_round_state("chg-1", "p-1", 1, db_session)
    db_session.commit()

    cr, events = advance_round_no_change("chg-1", db_session, actor_user_id="u1")
    db_session.commit()

    kinds = [(e.kind, e.round_number, e.reason) for e in events]
    assert ("closed", 1, "pm_forced") in kinds
    assert ("opened", 2, "pm_advance_no_change") in kinds
    assert cr.negotiation_frozen_at is None


def test_advance_round_no_change_at_cap_emits_frozen_close(db_session):
    """When advance would go beyond MAX_ROUNDS, no opened event fires — instead
    a symmetric round_closed(frozen) is appended so partners see the cap-hit."""
    from app.services.negotiation_extended import (
        advance_round_no_change,
        create_round_state,
    )
    _seed(db_session)
    # Pre-seed round 1 AND round 2 so we're already at the cap.
    create_round_state("chg-1", "p-1", 1, db_session)
    create_round_state("chg-1", "p-1", 2, db_session)
    db_session.commit()

    cr, events = advance_round_no_change("chg-1", db_session, actor_user_id="u1")
    db_session.commit()

    kinds = [(e.kind, e.round_number, e.reason) for e in events]
    assert ("closed", 2, "pm_forced") in kinds
    assert ("closed", 2, "frozen") in kinds
    assert all(e.kind != "opened" for e in events)
    assert cr.negotiation_frozen_at is not None


# ── apply_silent_acceptances emits close events per overdue state ─────────────

def test_apply_silent_acceptances_emits_close_events(db_session):
    from app.models.phase_c import NegotiationRoundState, RoundStatus
    from app.services.negotiation_extended import apply_silent_acceptances

    _seed(db_session)
    # Insert an overdue open round directly (bypass create_round_state so we
    # can set deadline_at in the past).
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.add(NegotiationRoundState(
        id="rs-1", change_request_id="chg-1", partner_id="p-1", round_number=1,
        started_at=past - timedelta(minutes=1),
        deadline_at=past, status=RoundStatus.OPEN.value,
    ))
    db_session.commit()

    affected, events = apply_silent_acceptances(db_session)
    db_session.commit()

    assert affected == ["chg-1:p-1"]
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "closed"
    assert ev.reason == "silent_acceptance"
    assert ev.round_number == 1


# ── close_open_rounds_for_version_ship retires live rounds only ───────────────

def test_close_open_rounds_for_version_ship(db_session):
    from app.models.phase_c import NegotiationRoundState, RoundStatus
    from app.services.negotiation_extended import (
        close_open_rounds_for_version_ship,
        create_round_state,
    )
    _seed(db_session)
    create_round_state("chg-1", "p-1", 1, db_session)
    db_session.commit()

    events = close_open_rounds_for_version_ship("chg-1", db_session)
    db_session.commit()

    assert len(events) == 1
    assert events[0].kind == "closed"
    assert events[0].reason == "superseded_by_version"

    # Second call is a no-op — the round is already CLOSED_BY_PM.
    events_again = close_open_rounds_for_version_ship("chg-1", db_session)
    assert events_again == []
    state = db_session.query(NegotiationRoundState).one()
    assert state.status == RoundStatus.CLOSED_BY_PM.value
