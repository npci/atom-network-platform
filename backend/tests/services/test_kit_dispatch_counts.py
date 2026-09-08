# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Kit dispatch must count DELIVERIES, not attempts.

`dispatch_kit_to_partners` already refuses to advance a partner's assignment or
start the negotiation-round clock on a failed send. The summary it returns did
not inherit that rule: it reported `len(results)`, one row per partner *tried*,
as `partners_notified` — so a kit that every partner refused came back HTTP 200
saying it had been notified, two log lines after the ERROR saying it had not.

Found live: PTNR's edge 503'd every RPC until its secret was installed, and the
authority reported `partners_notified: 1` alongside
`delivery_status: delivery_failed` for the same and only partner.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.domain.contract import Delivery


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register models
    from app.core.database import Base
    from app.models.change_request import ChangeRequest
    from app.models.notification import Notification
    from app.models.phase_c import (
        AssignmentStatusHistory,
        ChangePartnerAssignment,
        NegotiationRoundState,
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
            NegotiationRoundState.__table__,
            Notification.__table__,
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
        status=AssignmentStatus.ASSIGNED,
    )
    db.add(assignment)
    db.commit()
    return db.get(ChangeRequest, change_id), assignment


class _Chan:
    """Stub pack channel. `delivered=False` is PTNR's 503 fail-closed shape."""

    key = "stub"
    supports_responses = True

    def __init__(self, delivered: bool):
        self._delivered = delivered

    async def deliver(self, partner, message):
        return Delivery(
            partner_key=partner.key,
            delivered=self._delivered,
            reference="task-1",
            status="delivered" if self._delivered else "delivery_failed",
            error=None if self._delivered else "HTTP Error 503",
            error_code=None if self._delivered else "http_503",
            change_id=message.change_id,
        )

    async def poll_responses(self, partner):
        return ()


def _dispatch(db, monkeypatch, *, delivered: bool):
    from app.core.domain import contract as contract_mod
    from app.services import change_dispatch

    monkeypatch.setattr(contract_mod, "channel_of", lambda _p: _Chan(delivered),
                        raising=False)
    monkeypatch.setattr(change_dispatch, "get_active_pack", lambda: object(),
                        raising=False)

    cr, assignment = _seed(db)
    envelope = {"negotiation_version": 1, "documents": []}
    result = _run(change_dispatch.dispatch_kit_to_partners(
        cr, envelope, [assignment], db, "u1", mode="initial",
    ))
    return result, assignment


def test_a_refused_delivery_is_not_counted_as_notified(db_session, monkeypatch):
    from app.models.phase_c import AssignmentStatus

    result, assignment = _dispatch(db_session, monkeypatch, delivered=False)

    # The regression: this used to be 1, matching the single attempt.
    assert result["partners_notified"] == 0
    assert result["partners_failed"] == 1
    assert result["partners_skipped"] == 0
    # Per-partner detail was always correct; it is the summary that lied.
    assert result["results"][0]["delivery_status"] == "delivery_failed"
    assert result["results"][0]["delivered"] is False
    # The state machine already got this right — guard it stays right.
    assert assignment.status == AssignmentStatus.ASSIGNED


def test_a_real_delivery_is_counted_and_advances_the_assignment(db_session, monkeypatch):
    from app.models.phase_c import AssignmentStatus

    result, assignment = _dispatch(db_session, monkeypatch, delivered=True)

    assert result["partners_notified"] == 1
    assert result["partners_failed"] == 0
    assert result["results"][0]["delivered"] is True
    assert assignment.status == AssignmentStatus.RECEIVED
