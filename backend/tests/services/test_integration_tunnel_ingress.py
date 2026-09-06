# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-1 — the ingress half of the forward direction (NPCI side).

The ingress turns a local HTTP request into an A2A exchange. What matters:
it carries the request VERBATIM, it sends an alias rather than a URL, it
passes the §6 budget timeout the transport would otherwise default below, and
it never turns a far-side failure into an exception.
"""
from __future__ import annotations

import base64

import pytest

from app.a2a_common.integration_contract import (
    ErrorCode, HttpResponseSpec, decode_request, encode_error, encode_response,
)
from app.core.config import settings
from app.services.integration_testing import ingress


class _FakePartner:
    id = "p1"
    name = "Partner One"
    endpoint_url = "https://partner.example"


class _FakeMessage:
    def __init__(self, body, status="delivered"):
        self.status = status
        self.response_body = body


@pytest.fixture(autouse=True)
def _tunnel_on(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "integration_testing_max_body_bytes", 1024 * 1024, raising=False)
    monkeypatch.setattr(settings, "integration_testing_a2a_timeout_s", 90.0, raising=False)
    monkeypatch.setattr(settings, "integration_testing_target_timeout_s", 60.0, raising=False)


def _capture(monkeypatch, reply):
    """Intercept send_task_to_partner and record what the tunnel sent."""
    seen: dict = {}

    async def fake_send(partner, task_type, payload, db, change_request_id=None, **kw):
        seen.update(partner=partner, task_type=task_type, payload=payload, kwargs=kw)
        return _FakeMessage(reply)

    import app.services.a2a_client as a2a_client

    monkeypatch.setattr(a2a_client, "send_task_to_partner", fake_send)
    return seen


async def _forward(monkeypatch, reply, **kw):
    seen = _capture(monkeypatch, reply)
    kw.setdefault("method", "GET")
    kw.setdefault("path", "/v1/ping")
    kw.setdefault("query", "")
    kw.setdefault("headers", [])
    kw.setdefault("body", b"")
    result = await ingress.forward_exchange(
        db=None, partner=_FakePartner(), alias="external_api", **kw)
    return seen, result


@pytest.mark.anyio
async def test_response_comes_home_on_the_synchronous_reply(monkeypatch):
    reply = encode_response(exchange_id="x", elapsed_ms=12,
                            response=HttpResponseSpec(200, (("X-A", "1"),), b"pong"))
    _seen, result = await _forward(monkeypatch, reply)
    assert not result.failed
    assert result.response.status == 200 and result.response.body == b"pong"


@pytest.mark.anyio
async def test_the_alias_travels_and_a_url_never_does(monkeypatch):
    seen, _ = await _forward(monkeypatch, encode_response(
        exchange_id="x", response=HttpResponseSpec(200)))
    assert seen["payload"]["target"] == {"alias": "external_api"}
    assert "url" not in str(seen["payload"])


@pytest.mark.anyio
async def test_the_a2a_send_carries_the_budget_timeout(monkeypatch):
    """THE §4.1 gap this slice closes. The transport default is 30s — BELOW the
    60s target ceiling — so without an explicit timeout every slow case fails
    at the transport before the target ever answers."""
    seen, _ = await _forward(monkeypatch, encode_response(
        exchange_id="x", response=HttpResponseSpec(200)))
    assert seen["kwargs"]["timeout"] == 90.0


@pytest.mark.anyio
async def test_the_deadline_sent_is_the_target_ceiling(monkeypatch):
    seen, _ = await _forward(monkeypatch, encode_response(
        exchange_id="x", response=HttpResponseSpec(200)))
    assert seen["payload"]["deadline_ms"] == 60_000


@pytest.mark.anyio
@pytest.mark.parametrize("query", [
    "pack=CHG-4711%403", "a=1&a=2", "z=1&a=2", "flag", "",
])
async def test_query_is_carried_verbatim(monkeypatch, query):
    seen, _ = await _forward(monkeypatch, encode_response(
        exchange_id="x", response=HttpResponseSpec(200)), query=query)
    assert decode_request(seen["payload"]).request.query == query


@pytest.mark.anyio
async def test_binary_body_and_repeated_headers_survive(monkeypatch):
    blob = bytes(range(256))
    seen, _ = await _forward(
        monkeypatch, encode_response(exchange_id="x", response=HttpResponseSpec(200)),
        method="POST", body=blob,
        headers=[("X-Multi", "a"), ("X-Multi", "b"), ("Authorization", "Bearer t")])
    decoded = decode_request(seen["payload"])
    assert decoded.request.body == blob
    assert decoded.request.headers == (("X-Multi", "a"), ("X-Multi", "b"),
                                       ("Authorization", "Bearer t"))


@pytest.mark.anyio
async def test_hop_by_hop_headers_are_stripped_before_the_wire(monkeypatch):
    seen, _ = await _forward(
        monkeypatch, encode_response(exchange_id="x", response=HttpResponseSpec(200)),
        headers=[("Connection", "close"), ("Host", "old"), ("Accept", "*/*")])
    names = {n.lower() for n, _ in decode_request(seen["payload"]).request.headers}
    assert names == {"accept"}


@pytest.mark.anyio
async def test_far_side_error_is_returned_not_raised(monkeypatch):
    reply = encode_error(exchange_id="x", code=ErrorCode.UNKNOWN_ALIAS,
                         detail="alias 'nope' is not in the allowlist")
    _seen, result = await _forward(monkeypatch, reply)
    assert result.failed and result.error["code"] == ErrorCode.UNKNOWN_ALIAS


@pytest.mark.anyio
async def test_executor_wrapped_reply_is_unwrapped(monkeypatch):
    """The executor nests a handler's dict under `message`; both shapes must
    decode so this keeps working when ITA-2 makes the wrapping structured."""
    inner = encode_response(exchange_id="x", response=HttpResponseSpec(204))
    _seen, result = await _forward(monkeypatch, {"status": "ok", "message": inner})
    assert not result.failed and result.response.status == 204


@pytest.mark.anyio
async def test_undelivered_send_is_target_unreachable(monkeypatch):
    async def fake_send(*a, **kw):
        return _FakeMessage(None, status="delivery_failed")

    import app.services.a2a_client as a2a_client

    monkeypatch.setattr(a2a_client, "send_task_to_partner", fake_send)
    result = await ingress.forward_exchange(
        db=None, partner=_FakePartner(), alias="external_api",
        method="GET", path="/v1/x", query="", headers=[], body=b"")
    assert result.failed and result.error["code"] == ErrorCode.TARGET_UNREACHABLE


@pytest.mark.anyio
async def test_disabled_tunnel_never_sends(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_enabled", False, raising=False)

    async def fake_send(*a, **kw):
        pytest.fail("a disabled tunnel must not put anything on the wire")

    import app.services.a2a_client as a2a_client

    monkeypatch.setattr(a2a_client, "send_task_to_partner", fake_send)
    result = await ingress.forward_exchange(
        db=None, partner=_FakePartner(), alias="external_api",
        method="GET", path="/v1/x", query="", headers=[], body=b"")
    assert result.failed and result.error["code"] == ErrorCode.TUNNEL_DISABLED


@pytest.mark.anyio
async def test_oversize_body_is_refused_before_the_wire(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_max_body_bytes", 10, raising=False)

    async def fake_send(*a, **kw):
        pytest.fail("an oversize body must not reach the wire")

    import app.services.a2a_client as a2a_client

    monkeypatch.setattr(a2a_client, "send_task_to_partner", fake_send)
    result = await ingress.forward_exchange(
        db=None, partner=_FakePartner(), alias="external_api",
        method="POST", path="/v1/x", query="", headers=[], body=b"x" * 100)
    assert result.failed and result.error["code"] == ErrorCode.PAYLOAD_TOO_LARGE


@pytest.mark.anyio
async def test_corrupt_reply_is_reported_as_digest_mismatch(monkeypatch):
    reply = encode_response(exchange_id="x", response=HttpResponseSpec(200, (), b"real"))
    reply["response"]["body_b64"] = base64.b64encode(b"tampered").decode()
    _seen, result = await _forward(monkeypatch, reply)
    assert result.failed and result.error["code"] == ErrorCode.DIGEST_MISMATCH


# ── the transport-level plumbing (§4.1) ──────────────────────────────────────

def test_send_task_to_partner_accepts_and_forwards_a_timeout():
    """The parameter did not exist; §6's middle layer depends on it."""
    import inspect

    from app.services.a2a_client import send_task_to_partner

    assert "timeout" in inspect.signature(send_task_to_partner).parameters
    src = inspect.getsource(send_task_to_partner)
    assert 'send_kwargs["timeout"]' in src


def test_timeout_is_omitted_when_not_asked_for():
    """Every non-tunnel caller must keep the transport default — passing None
    through would override it with a null."""
    import inspect

    from app.services.a2a_client import send_task_to_partner

    src = inspect.getsource(send_task_to_partner)
    assert "if timeout is not None:" in src


def test_the_budget_shrinks_inward():
    """ingress > a2a send > target. Equal timeouts mean the OUTER layer fires
    first and the operator sees a generic 504 with no inner detail."""
    assert (settings.integration_testing_ingress_timeout_s
            > settings.integration_testing_a2a_timeout_s
            > settings.integration_testing_target_timeout_s)
