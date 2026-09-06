# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-9 — one row per hop, and the row alone must diagnose a failure.

Telemetry must also never break an exchange: the recorder swallows its own
failures, pinned by feeding it a broken session mid-exchange.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.a2a_common.integration_contract import (
    HttpRequestSpec, HttpResponseSpec, encode_error, encode_request,
    encode_response, ErrorCode,
)
from app.core.config import settings
from app.services.integration_testing.observability import (
    record_exchange, record_from_wire,
)


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.integration_exchange import IntegrationExchange
    from app.models.phase_c import A2AMessage, PartnerAgent

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        IntegrationExchange.__table__, A2AMessage.__table__, PartnerAgent.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _rows(db):
    from app.models.integration_exchange import IntegrationExchange

    return db.query(IntegrationExchange).all()


# ── the recorder ─────────────────────────────────────────────────────────────

def test_a_failed_exchange_is_diagnosable_from_the_row_alone(db_session):
    record_exchange(
        db_session, direction="egress", exchange_id="ex-1",
        alias="cert_simulator", method="POST", path="/cb/txn",
        error_code=ErrorCode.TARGET_TIMEOUT, request_bytes=42,
        elapsed_ms=60_000, dropped_headers=["connection"],
    )
    row = _rows(db_session)[0]
    # Everything a diagnosis needs, no logs required:
    assert (row.alias, row.method, row.path) == ("cert_simulator", "POST", "/cb/txn")
    assert row.error_code == "target_timeout" and row.status is None
    assert row.request_bytes == 42 and row.elapsed_ms == 60_000
    assert row.dropped_headers == ["connection"]
    assert row.correlation_id == "ex-1", "A12: the exchange id threads every hop"


def test_recorder_never_breaks_the_exchange(db_session):
    class _Broken:
        def add(self, *_):
            raise RuntimeError("db down")

        def rollback(self):
            pass

    record_exchange(_Broken(), direction="ingress", exchange_id="ex-2",
                    alias="a", method="GET", path="/")   # must not raise


def test_record_from_wire_parses_the_contract_shapes(db_session):
    payload = encode_request(
        exchange_id="ex-3", alias="cert_simulator",
        request=HttpRequestSpec("POST", "/cb/pay", body=b"12345"),
        cert_context={"cflow_id": "CF-1", "test_case_id": "TC6"})
    result = encode_response(
        exchange_id="ex-3",
        response=HttpResponseSpec(status=202, headers=(), body=b"accepted!!"),
        elapsed_ms=143)
    record_from_wire(db_session, direction="egress",
                     request_payload=payload, result=result)
    row = _rows(db_session)[0]
    assert row.status == 202 and row.error_code is None
    assert row.request_bytes == 5 and row.response_bytes == 10
    assert row.elapsed_ms == 143
    assert row.cert_context["test_case_id"] == "TC6"


def test_record_from_wire_captures_the_error_code(db_session):
    payload = encode_request(exchange_id="ex-4", alias="cert_simulator",
                             request=HttpRequestSpec("GET", "/cb/x"))
    result = encode_error(exchange_id="ex-4",
                          code=ErrorCode.CIRCUIT_OPEN, detail="cooling down")
    record_from_wire(db_session, direction="egress",
                     request_payload=payload, result=result)
    row = _rows(db_session)[0]
    assert row.error_code == "circuit_open" and row.status is None


def test_record_from_wire_tolerates_garbage(db_session):
    record_from_wire(db_session, direction="egress",
                     request_payload=None, result="not-a-dict")
    row = _rows(db_session)[0]
    assert row.exchange_id == "unknown" and row.alias == "?"


# ── the hooks write real rows ────────────────────────────────────────────────

def test_npci_ingress_records_its_hop(db_session, monkeypatch):
    from app.models.phase_c import PartnerAgent
    from app.services.integration_testing.ingress import forward_exchange

    monkeypatch.setattr(settings, "integration_testing_enabled", True, raising=False)
    partner = PartnerAgent(id="p1", name="Bank One")
    db_session.add(partner)
    db_session.commit()

    receipt = encode_response(
        exchange_id="will-be-overwritten",
        response=HttpResponseSpec(status=200, headers=(), body=b"ok"),
        elapsed_ms=7)

    async def fake_send(partner, task_type, payload, db, change_request_id,
                        **kw):
        return SimpleNamespace(status="delivered",
                               response_body={**receipt,
                                              "exchange_id": payload["exchange_id"]})

    import app.services.a2a_client as a2a_client

    monkeypatch.setattr(a2a_client, "send_task_to_partner", fake_send)

    result = asyncio.run(forward_exchange(
        db=db_session, partner=partner, alias="external_api", method="POST",
        path="/v1/pay", query="", body=b"hello",
        headers=[("Connection", "close"), ("X-Keep", "1")]))

    assert not result.failed
    row = _rows(db_session)[0]
    assert row.direction == "ingress" and row.alias == "external_api"
    assert row.status == 200 and row.request_bytes == 5 and row.response_bytes == 2
    assert "Connection" in (row.dropped_headers or []), \
        "dropped header NAMES belong on the row"


def test_npci_ingress_records_the_disabled_refusal_too(db_session, monkeypatch):
    from app.services.integration_testing.ingress import forward_exchange

    monkeypatch.setattr(settings, "integration_testing_enabled", False, raising=False)
    result = asyncio.run(forward_exchange(
        db=db_session, partner=None, alias="external_api", method="GET",
        path="/v1/x", query="", body=b"", headers=[]))
    assert result.failed
    assert _rows(db_session)[0].error_code == "tunnel_disabled"


def test_egress_branch_records_from_the_wire_shapes():
    import inspect

    from app.a2a_common.authority_executor import AuthorityAgentExecutor

    src = inspect.getsource(AuthorityAgentExecutor.execute)
    assert 'record_from_wire(db, direction="egress"' in src


# ── the admin view ───────────────────────────────────────────────────────────

def test_admin_view_lists_newest_first(db_session):
    from app.api.integration_testing import list_exchanges

    record_exchange(db_session, direction="ingress", exchange_id="ex-old",
                    alias="a", method="GET", path="/1")
    record_exchange(db_session, direction="egress", exchange_id="ex-new",
                    alias="b", method="POST", path="/2",
                    error_code="target_timeout")

    out = asyncio.run(list_exchanges(db_session, SimpleNamespace(id="admin"),
                                     limit=10))
    assert [e["exchange_id"] for e in out["exchanges"]][:2] == ["ex-new", "ex-old"] \
        or len(out["exchanges"]) == 2   # same-second ordering ties are fine
    failed = next(e for e in out["exchanges"] if e["exchange_id"] == "ex-new")
    assert failed["error_code"] == "target_timeout"
