# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-5 — the H3 obligations: per-alias gates, sweeper exclusion, budget.

The failure this slice exists to prevent is invisible in a green suite: a
tunnelled POST replayed by a retry sweeper is a DUPLICATE BUSINESS CALL, and a
dead target burning every caller's full timeout serially is a platform-wide
stall. So the tests here saturate, trip and exclude — and assert the refusals
are STRUCTURED (distinct §5.2 codes), because "the Simulator can assert on
them" is the contract.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.a2a_common.integration_contract import (
    ErrorCode, HttpRequestSpec, TUNNEL_TASK_TYPES, decode_response, encode_request,
)
from app.core.config import settings
from app.services.integration_testing import egress

POLICY = ('{"sim": {"scheme": "http", "host": "sim.internal", "port": 8090,'
          ' "path_prefixes": ["/cb/"]},'
          ' "other": {"scheme": "http", "host": "other.internal",'
          ' "path_prefixes": ["/cb/"]}}')


@pytest.fixture(autouse=True)
def _tunnel_on(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "integration_testing_allowlist", POLICY, raising=False)
    monkeypatch.setattr(settings, "integration_testing_target_timeout_s", 60.0, raising=False)
    monkeypatch.setattr(settings, "integration_testing_max_body_bytes", 1024 * 1024, raising=False)
    monkeypatch.setattr(settings, "integration_testing_max_hops", 1, raising=False)
    monkeypatch.setattr(settings, "integration_testing_breaker_failure_threshold", 2, raising=False)
    monkeypatch.setattr(settings, "integration_testing_breaker_cooldown_s", 300.0, raising=False)
    monkeypatch.setattr(settings, "integration_testing_max_concurrent_per_alias", 1, raising=False)
    egress._reset_gates_for_tests()
    yield
    egress._reset_gates_for_tests()


def _payload(alias="sim", exchange_id="ex-1"):
    return encode_request(exchange_id=exchange_id, alias=alias,
                          request=HttpRequestSpec("GET", "/cb/ping"))


def _stub_transport(monkeypatch, handler):
    real_client = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)


# ── the circuit breaker ──────────────────────────────────────────────────────

def test_breaker_opens_after_threshold_and_refuses_fast(monkeypatch):
    calls = {"n": 0}

    def failing(request):
        calls["n"] += 1
        raise httpx.ConnectError("dead", request=request)

    _stub_transport(monkeypatch, failing)
    for _ in range(2):   # threshold=2
        out = decode_response(egress.perform_exchange(_payload()))
        assert out.error["code"] == ErrorCode.TARGET_UNREACHABLE

    out = decode_response(egress.perform_exchange(_payload()))
    assert out.failed and out.error["code"] == ErrorCode.CIRCUIT_OPEN
    assert calls["n"] == 2, "the open circuit must refuse WITHOUT calling the target"


def test_gates_are_per_alias_not_per_platform(monkeypatch):
    """A dead simulator must not take the External API's exchanges with it."""
    def handler(request):
        if "sim.internal" in str(request.url):
            raise httpx.ConnectError("dead", request=request)
        return httpx.Response(200, content=b"ok")

    _stub_transport(monkeypatch, handler)
    for _ in range(2):
        egress.perform_exchange(_payload(alias="sim"))
    assert decode_response(egress.perform_exchange(_payload(alias="sim"))) \
        .error["code"] == ErrorCode.CIRCUIT_OPEN

    out = decode_response(egress.perform_exchange(_payload(alias="other")))
    assert not out.failed and out.response.body == b"ok"


def test_a_success_closes_the_failure_count(monkeypatch):
    responses = iter([None, "ok", None, None, None])   # fail, succeed, fail…

    def handler(request):
        step = next(responses)
        if step is None:
            raise httpx.ConnectError("blip", request=request)
        return httpx.Response(200)

    _stub_transport(monkeypatch, handler)
    egress.perform_exchange(_payload())                    # failure 1
    assert not decode_response(egress.perform_exchange(_payload())).failed  # success resets
    egress.perform_exchange(_payload())                    # failure 1 again
    out = decode_response(egress.perform_exchange(_payload()))             # failure 2 → opens
    assert out.error["code"] == ErrorCode.TARGET_UNREACHABLE, \
        "the opening call itself still reports the real failure"
    assert decode_response(egress.perform_exchange(_payload())) \
        .error["code"] == ErrorCode.CIRCUIT_OPEN


# ── the bulkhead ─────────────────────────────────────────────────────────────

def test_saturated_alias_is_refused_with_its_own_code(monkeypatch):
    _stub_transport(monkeypatch, lambda r: httpx.Response(200))
    _, bulkhead = egress._gate_for("sim")
    slot = bulkhead.acquire(timeout=1.0)
    slot.__enter__()   # occupy the only slot (max_concurrent=1)
    try:
        out = decode_response(egress.perform_exchange(_payload()))
        assert out.failed and out.error["code"] == ErrorCode.BULKHEAD_SATURATED
    finally:
        slot.__exit__(None, None, None)

    assert not decode_response(egress.perform_exchange(_payload())).failed, \
        "a released slot must serve the next call"


# ── the retry sweeper must never replay a tunnelled call ─────────────────────

def test_tunnel_task_types_are_never_scheduled_for_retry():
    from app.services.a2a_client import _record_attempt

    for task_type in sorted(TUNNEL_TASK_TYPES):
        message = SimpleNamespace(
            attempts=0, status="delivery_failed", error_code="http_503",
            task_type=task_type, next_retry_at="sentinel", last_error_at=None)
        _record_attempt(message)
        assert message.next_retry_at is None, \
            f"{task_type} was scheduled for retry — a replay is a duplicate business call"

    # …while an ordinary retryable failure still schedules.
    normal = SimpleNamespace(attempts=0, status="delivery_failed",
                             error_code="http_503", task_type="query",
                             next_retry_at=None, last_error_at=None)
    _record_attempt(normal)
    assert normal.next_retry_at is not None


def test_sweep_query_excludes_tunnel_task_types():
    """Belt to the scheduling braces — covers rows queued before the exclusion
    existed. Pinned on the filter expression itself (not docstring-matchable)."""
    import inspect

    from app.services import celery_tasks

    src = inspect.getsource(celery_tasks)
    assert "A2AMessage.task_type.notin_(list(TUNNEL_TASK_TYPES))" in src


# ── the §6 budget is refused at boot when it does not shrink inward ──────────

def test_inverted_budget_is_refused_at_startup():
    from app.core.config import Settings

    with pytest.raises(Exception) as exc:
        Settings(
            secret_key="dev-test-secret-key-0123456789abcdef0123456789abcdef",
            integration_testing_enabled=True,
            integration_testing_allowlist="{}",
            integration_testing_a2a_timeout_s=200.0,   # > ingress 105 — inverted
        )
    assert "shrink inward" in str(exc.value)


def test_default_budget_boots():
    from app.core.config import Settings

    s = Settings(
        secret_key="dev-test-secret-key-0123456789abcdef0123456789abcdef",
        integration_testing_enabled=True,
        integration_testing_allowlist="{}",
    )
    assert s.integration_testing_ingress_timeout_s \
        > s.integration_testing_a2a_timeout_s \
        > s.integration_testing_target_timeout_s
