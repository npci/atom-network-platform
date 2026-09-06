# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The shared notify_partner helper — extracted after the sixth repetition.

The branch worth testing is the one that would rot if copied per call site: a
domain with no partner channel must be a no-op, not an exception. Six sites
each getting that right independently is six chances to get it wrong.
"""
import asyncio

import pytest

from app.core.domain.contract import Delivery


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _Chan:
    key = "stub"
    supports_responses = True

    def __init__(self, delivered=True):
        self.delivered = delivered
        self.seen = None

    async def deliver(self, partner, message):
        self.seen = (partner, message)
        return Delivery(partner_key=partner.key, delivered=self.delivered,
                        reference="r1", status="delivered" if self.delivered else "failed",
                        error=None if self.delivered else "boom",
                        change_id=message.change_id)

    async def poll_responses(self, partner):
        return ()


def test_returns_none_when_the_domain_has_no_channel(monkeypatch):
    """OCPP shape: publishing, nobody to notify. A no-op, not an error."""
    from app.services import partner_dispatch

    monkeypatch.setattr(partner_dispatch, "channel_of", lambda _p: None, raising=False)
    assert _run(partner_dispatch.notify_partner("p1", "round_opened", {})) is None


def test_delivers_through_the_active_channel(monkeypatch):
    from app.services import partner_dispatch

    chan = _Chan()
    monkeypatch.setattr(partner_dispatch, "channel_of", lambda _p: chan, raising=False)

    result = _run(partner_dispatch.notify_partner(
        "p1", "round_opened", {"round": 2},
        change_id="chg-1", label="Partner One", correlation_id="c-1",
    ))

    partner, message = chan.seen
    assert partner.key == "p1" and partner.label == "Partner One"
    assert message.kind == "round_opened"
    assert message.change_id == "chg-1"
    assert message.correlation_id == "c-1"
    assert message.payload == {"round": 2}
    assert result.delivered is True


def test_a_failed_delivery_is_logged_even_if_the_caller_ignores_it(monkeypatch, caplog):
    """The direct A2A call raised; the channel returns. Callers that only
    notified used to rely on the raise to surface problems, so the helper logs
    rather than letting failures vanish."""
    from app.services import partner_dispatch

    monkeypatch.setattr(partner_dispatch, "channel_of",
                        lambda _p: _Chan(delivered=False), raising=False)
    with caplog.at_level("WARNING"):
        _run(partner_dispatch.notify_partner("p1", "round_closed", {},
                                             context="round closed"))
    assert any("NOT delivered" in r.getMessage() for r in caplog.records)


def test_negotiation_module_no_longer_calls_the_transport_directly():
    """Guards the migration: all four sites in negotiation_extended go through
    the channel now."""
    import inspect

    from app.services import negotiation_extended

    src = inspect.getsource(negotiation_extended)
    assert "send_task_to_partner" not in src
    assert "notify_partner" in src
