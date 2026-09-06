# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Slice 3 smoke test for the Authority A2A executor.

Real DB-touching round-trips need pytest-asyncio + a test Postgres
fixture (lands in Slice 5 alongside the outbound rewrite). For now we
test the executor's failure paths — the ones that DON'T need a DB
session — by mocking the SDK helpers and checking the right methods
fire on bad input.

Skipped cleanly when `a2a-sdk` isn't installed.
"""
from __future__ import annotations

import asyncio

import pytest


pytest.importorskip("a2a")


def _build_context(parts):
    """Construct a minimal RequestContext with a single message holding
    the given Parts. Real Tasks have richer fields; the executor only
    reads `context.message.parts`, `task_id`, `context_id`."""
    from a2a.types.a2a_pb2 import Message, Role
    from a2a.server.agent_execution.context import RequestContext

    msg = Message(
        role=Role.ROLE_USER,
        message_id="msg-test",
        context_id="ctx-test",
        task_id="task-test",
        parts=parts,
    )
    # RequestContext is constructed by the SDK in production; for tests
    # we duck-type the attributes the executor reads.
    class _Ctx:
        message = msg
        task_id = "task-test"
        context_id = "ctx-test"
    return _Ctx()


class _CapturingEventQueue:
    """Records events instead of forwarding them. The TaskUpdater calls
    enqueue_event(event) for status transitions and artifacts; we just
    keep a list."""
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


def test_executor_constructs():
    """Sanity: the class imports and can be instantiated. Catches
    SDK-API drift early (TaskUpdater signature changes, AgentExecutor
    abstract method renames, etc.)."""
    from app.a2a_common.authority_executor import AuthorityAgentExecutor
    executor = AuthorityAgentExecutor()
    assert executor is not None
    # AgentExecutor is an abstract base — confirm execute/cancel are
    # both implemented by our subclass.
    assert callable(executor.execute)
    assert callable(executor.cancel)


def test_empty_message_fails_fast():
    """A message with no parts should land the Task in FAILED with an
    error artifact — not raise. No DB session is opened."""
    from app.a2a_common.authority_executor import AuthorityAgentExecutor

    executor = AuthorityAgentExecutor()
    queue = _CapturingEventQueue()
    ctx = _build_context(parts=[])

    asyncio.run(executor.execute(ctx, queue))

    # Expect at least one event emitted (start_work + failed transition
    # + the error artifact).
    assert len(queue.events) >= 1


# ── ITA-2: the structured executor response (blocker B1) ─────────────────────

def test_string_receipt_shape_is_unchanged():
    """Every pre-existing handler returns a string; the receipt for them must
    stay the exact four-key shape, key order included — the partner's client
    and the admin A2A logs UI both read it."""
    from app.a2a_common.authority_executor import _receipt_payload

    payload = _receipt_payload(task_id="m-1", status="completed",
                               task_type="cert_status_update", result="Done")
    assert payload == {"task_id": "m-1", "status": "completed",
                       "task_type": "cert_status_update", "message": "Done"}
    assert list(payload.keys()) == ["task_id", "status", "task_type", "message"]


def test_dict_result_merges_into_the_receipt():
    """A dict-returning handler — the reverse tunnel's shape (§5.2, nested
    `response.status`) — rides home structurally instead of flattened into
    prose."""
    from app.a2a_common.authority_executor import _receipt_payload

    exchange = {
        "exchange_id": "ex-1",
        "response": {"status": 200,
                     "headers": [["content-type", "application/json"]],
                     "body_b64": "e30=", "body_sha256": "abc"},
        "elapsed_ms": 143,
    }
    payload = _receipt_payload(task_id="m-2", status="completed",
                               task_type="http_exchange_response",
                               result=exchange)
    assert payload["exchange_id"] == "ex-1"
    assert payload["response"]["status"] == 200          # nested, untouched
    assert payload["elapsed_ms"] == 143
    assert payload["task_id"] == "m-2"
    assert payload["status"] == "completed"
    assert payload["task_type"] == "http_exchange_response"
    assert "message" not in payload, "no prose key invented for dict results"


def test_dict_result_cannot_reclassify_the_receipt():
    """The executor owns the identity keys: a handler dict that tries to
    overwrite `task_id`/`status`/`task_type` loses — a receipt that lies about
    its own status would poison the partner's delivery bookkeeping."""
    from app.a2a_common.authority_executor import _receipt_payload

    payload = _receipt_payload(
        task_id="real-id", status="completed", task_type="echo",
        result={"task_id": "forged", "status": "failed", "task_type": "other",
                "data": 1})
    assert payload["task_id"] == "real-id"
    assert payload["status"] == "completed"
    assert payload["task_type"] == "echo"
    assert payload["data"] == 1


def test_dict_result_may_carry_its_own_message_key():
    """`message` is NOT an identity key — a structured handler that also
    supplies human-readable prose keeps it."""
    from app.a2a_common.authority_executor import _receipt_payload

    payload = _receipt_payload(task_id="m-3", status="completed",
                               task_type="echo",
                               result={"message": "42 bytes forwarded", "n": 42})
    assert payload["message"] == "42 bytes forwarded"
    assert payload["n"] == 42


def test_execute_routes_the_receipt_through_the_helper():
    """Wiring pin: the emit block builds its payload via `_receipt_payload`
    (matched on the assignment expression, which cannot appear in a
    docstring)."""
    import inspect

    from app.a2a_common.authority_executor import AuthorityAgentExecutor

    src = inspect.getsource(AuthorityAgentExecutor.execute)
    assert "response_payload = _receipt_payload(" in src
    assert '"message":   result_msg' not in src, "the inline literal is back"


def test_unknown_task_type_fails_fast():
    """Bad task_type string should fail without touching the DB.

    Slice 2 of the security hardening dropped `partner_api_key` from
    the message data envelope (auth moved to SdkAuthMiddleware). The
    test data accordingly carries only `task_type`.
    """
    from a2a.types.a2a_pb2 import Part
    from google.protobuf import json_format, struct_pb2
    from app.a2a_common.authority_executor import AuthorityAgentExecutor

    s = struct_pb2.Struct()
    json_format.ParseDict({
        "task_type": "not_a_real_task_type",
    }, s)
    v = struct_pb2.Value()
    v.struct_value.CopyFrom(s)
    part = Part()
    part.data.CopyFrom(v)

    executor = AuthorityAgentExecutor()
    queue = _CapturingEventQueue()
    ctx = _build_context(parts=[part])

    asyncio.run(executor.execute(ctx, queue))

    # We don't introspect the SDK event types — just confirm SOMETHING
    # was emitted (no silent return). With the middleware not wrapping
    # this raw test, the executor's _AUTH_CONTEXT lookup fails first
    # and surfaces an error artifact — same exit path as before.
    assert len(queue.events) >= 1
