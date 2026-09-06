# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Outbound `services.a2a_client.send_task_to_partner` smoke tests.

Originally Slice-5 tested both the legacy POST wire and the SDK wire.
Slice 8 deletes the legacy path; this file is rewritten to cover the
SDK-only behaviour:

  * Successful SDK send → status='delivered', task_state='completed',
                           protocol_ver='a2a_sdk', task_id_a2a populated.
  * SDK exception → status='delivery_failed', task_state='failed'.
  * Partner with no endpoint → status='pending'.
  * cert_engine without api_key → bypassed before sending; row marked
                                   'delivery_failed' with task_state='failed'.

Skipped cleanly when a2a-sdk isn't installed.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


pytest.importorskip("a2a")
pytest.importorskip("sqlalchemy")


@pytest.fixture
def db_session():
    """Fresh in-memory sqlite session with the phase_c models registered."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.core.database import Base
    # Force model imports so all tables register on Base.metadata.
    import app.models.phase_c  # noqa: F401
    import app.models.change_request  # noqa: F401
    import app.models.user  # noqa: F401

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_partner(db_session, *, is_cert_engine: bool = False, with_endpoint: bool = True):
    """Insert a minimal PartnerAgent row and return it.

    Slice 8: protocol_version is no longer a behavioural switch (always
    'a2a_sdk'), but the column still exists for audit. Default leaves
    the column at its server-side default 'legacy' to confirm that the
    dispatcher ignores it.
    """
    from app.models.phase_c import PartnerAgent, PartnerStatus

    p = PartnerAgent(
        id="p-test",
        name="Test Partner",
        partner_type=["cert_engine"] if is_cert_engine else ["bank"],
        endpoint_url="http://test-partner.local" if with_endpoint else None,
        api_key="a2a_testkey",
        status=PartnerStatus.ACTIVE,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def test_sdk_send_succeeds(db_session):
    """Successful SDK send populates task_id_a2a + task_state='completed'."""
    from app.models.phase_c import A2ATaskType
    from app.services import a2a_client

    partner = _make_partner(db_session)

    # send_a2a_message returns Optional[dict] — the receiver's artifact, which
    # Slice 25 persists onto response_body (a JSON column). A bare AsyncMock()
    # returns a mock object here and the flush dies on "not JSON serializable",
    # so the double has to honour the real return contract.
    artifact = {"ack": True, "received": "cr-1"}
    sdk_send = AsyncMock(return_value=artifact)
    with patch.object(a2a_client, "send_a2a_message", new=sdk_send) as mock_sdk_send:
        msg = asyncio.run(a2a_client.send_task_to_partner(
            partner=partner,
            task_type=A2ATaskType.CHANGE_COMMUNICATION,
            payload={"k": "v"},
            db=db_session,
            change_request_id="cr-1",
        ))

    assert msg.status == "delivered"
    assert msg.protocol_ver == "a2a_sdk"
    assert msg.task_id_a2a == msg.id
    assert msg.task_state == "completed"
    assert msg.response_body == artifact
    mock_sdk_send.assert_called_once()


def test_sdk_failure_marks_task_state_failed(db_session):
    """SDK exception → status='delivery_failed' AND task_state='failed'."""
    from app.models.phase_c import A2ATaskType
    from app.services import a2a_client

    partner = _make_partner(db_session)

    sdk_send = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(a2a_client, "send_a2a_message", new=sdk_send):
        msg = asyncio.run(a2a_client.send_task_to_partner(
            partner=partner,
            task_type=A2ATaskType.CHANGE_COMMUNICATION,
            payload={"k": "v"},
            db=db_session,
            change_request_id="cr-1",
        ))

    assert msg.status == "delivery_failed"
    assert msg.task_state == "failed"
    assert msg.protocol_ver == "a2a_sdk"


def test_partner_with_no_endpoint_marks_pending(db_session):
    """No endpoint configured → row persists as 'pending', SDK not touched."""
    from app.models.phase_c import A2ATaskType
    from app.services import a2a_client

    partner = _make_partner(db_session, with_endpoint=False)

    with patch.object(a2a_client, "send_a2a_message", new=AsyncMock()) as mock_sdk_send:
        msg = asyncio.run(a2a_client.send_task_to_partner(
            partner=partner,
            task_type=A2ATaskType.CHANGE_COMMUNICATION,
            payload={},
            db=db_session,
        ))

    assert msg.status == "pending"
    mock_sdk_send.assert_not_called()


def test_cert_engine_without_jwt_short_circuits(db_session):
    """cert_engine partners require a JWT. If the auth fetch returns None
    (mocked here as no api_key path), the dispatcher fails the row
    BEFORE attempting an SDK send."""
    from app.models.phase_c import A2ATaskType, PartnerAgent, PartnerStatus
    from app.services import a2a_client

    p = PartnerAgent(
        id="p-ce",
        name="Cert Engine",
        partner_type=["cert_engine"],
        endpoint_url="http://cert-agent:8000",
        api_key=None,                # forces _bearer_jwt_if_needed → None
        status=PartnerStatus.ACTIVE,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    with patch.object(a2a_client, "send_a2a_message", new=AsyncMock()) as mock_sdk_send:
        msg = asyncio.run(a2a_client.send_task_to_partner(
            partner=p,
            task_type=A2ATaskType.CERT_TEST_REQUEST,
            payload={},
            db=db_session,
            change_request_id="cr-1",
        ))

    assert msg.status == "delivery_failed"
    assert msg.task_state == "failed"
    mock_sdk_send.assert_not_called()
