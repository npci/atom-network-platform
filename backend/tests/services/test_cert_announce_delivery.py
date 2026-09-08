# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""A certification round must not await a partner that was never reached.

`_announce` documents the intended degradation itself: "Returns an EMPTY set —
the whole scope stays this side's — when the tunnel is off, no partner agent
exists, or the send fails. A failed announcement must not strand the round
awaiting reports that can never arrive."

The "or the send fails" half was unreachable. It was guarded by
`except Exception:`, but `send_task_to_partner` ALWAYS RETURNS — its docstring
(a2a_client.py:427) says the row comes back with status 'delivery_failed' for
"SDK exception (auth, transport, server)". Nothing raised, so nothing was
caught, and a partner we could not reach at all was still reported as
`awaiting_partner` with HTTP 200. The round then sat until the suite deadline
turned those cases into `not_reported` — a 24h wait for something knowable at
dispatch.

Found live: Meridian Airways' agent card resolves to `atom_partner_backend`, a
compose-internal name. Both cert_setup_notification and cert_execution_start
failed with `[Errno -3] Temporary failure in name resolution`, two ERROR lines
were logged by the client, and the dispatch still returned
`status=awaiting_partner` over 12 partner cases.

Third instance of the same root cause as F-22 (kit dispatch counted attempts as
deliveries) and F-23 (ship_kit advanced state on a failed send): a delivery
result reported by RETURN VALUE, read as though it were reported by exception.
"""
from __future__ import annotations

import asyncio
import types

import pytest


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Msg:
    """Stand-in for the A2AMessage row send_task_to_partner returns."""

    def __init__(self, status: str):
        self.status = status


class _Variant:
    def __init__(self, vid: str, initiator: str):
        self.id = vid
        self.initiator = initiator


class _Scope:
    """One case, one partner-initiated variant — the minimum that makes
    `_announce` have something to hand over."""

    case_ids = {"MO_1"}
    variants_by_case = {"MO_1": [_Variant("v-mo-1", "partner")]}


@pytest.fixture
def announce_env(monkeypatch):
    """Neutralise everything `_announce` touches except the delivery result."""
    from app.services import cert_pack_run

    monkeypatch.setattr(cert_pack_run.settings, "integration_testing_enabled",
                        True, raising=False)
    monkeypatch.setattr(cert_pack_run.settings, "cert_suite_deadline_s", 60,
                        raising=False)

    # A partner row only has to be non-None; _announce just passes it through.
    monkeypatch.setattr(cert_pack_run, "db", None, raising=False)

    from app.services.cert_agent import setup as _setup
    monkeypatch.setattr(_setup, "simulator_block",
                        lambda **kw: {"alias": "sim"}, raising=False)

    return cert_pack_run


def _call(cert_pack_run, monkeypatch, statuses):
    """Drive _announce with `statuses` as consecutive send results."""
    sent: list[str] = []
    seq = list(statuses)

    async def _fake_send(*, partner, task_type, payload, db,
                         change_request_id, cflow_id, cert_attempt):
        sent.append(str(task_type))
        # Do NOT pop from an empty list on an unexpected extra send. Letting it
        # IndexError makes a real regression read as a broken stub; returning a
        # delivered row instead lets the `sent` assertions name the actual
        # defect ("we sent the start signal to a partner that never got the
        # scope") rather than an artefact of the harness.
        return _Msg(seq.pop(0) if seq else "delivered")

    import app.services.a2a_client as _client
    monkeypatch.setattr(_client, "send_task_to_partner", _fake_send,
                        raising=False)

    db = types.SimpleNamespace(get=lambda model, pid: object())
    modes = types.SimpleNamespace(authority="simulator", partner="simulator",
                                  as_dict=lambda: {})
    graded_by = types.SimpleNamespace(pack_ref="pack@1")

    owned = _run(cert_pack_run._announce(
        db, change_id="chg-1", partner_id="p-1", cflow_id="CF-1",
        run_number=1, scope=_Scope(), catalogue=[],
        modes=modes, graded_by=graded_by,
    ))
    return owned, sent


def test_undelivered_setup_drops_the_partner_class(announce_env, monkeypatch):
    """The regression: this used to return the partner's variants anyway."""
    owned, sent = _call(announce_env, monkeypatch, ["delivery_failed"])

    # Empty set => run_round keeps the whole scope on this side, so the round
    # reports a verdict instead of parking on `awaiting_partner`.
    assert owned == set()
    # And we must NOT go on to send the start signal to a partner that never
    # received the scope.
    assert len(sent) == 1


def test_undelivered_start_keeps_the_class_to_expire_honestly(announce_env,
                                                              monkeypatch):
    """Deliberately a DIFFERENT degradation from the setup failure.

    The partner holds the scope here, so the cases stay theirs and expire at
    the suite deadline as `not_reported`. What changed is that the operator is
    told at dispatch rather than inferring it from a silent wait.
    """
    owned, sent = _call(announce_env, monkeypatch,
                        ["delivered", "delivery_failed"])

    assert owned == {"v-mo-1"}
    assert len(sent) == 2


def test_full_delivery_is_unaffected(announce_env, monkeypatch):
    owned, sent = _call(announce_env, monkeypatch, ["delivered", "delivered"])

    assert owned == {"v-mo-1"}
    assert len(sent) == 2
