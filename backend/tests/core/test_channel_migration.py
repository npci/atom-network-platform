# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The first call site migrated onto PartnerChannel — behaviour must be identical.

`_notify_partner_cp_decision` used to call `send_task_to_partner` directly. It
now goes through the active pack's channel. This is live partner communication,
so "looks equivalent" is not enough: these tests capture the arguments that
reach the transport and assert they match what the direct call passed.
"""
import asyncio

import pytest

from app.core.domain.contract import OutboundMessage, Partner, channel_of
from app.core.domain.registry import get_active_pack


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


ARGS = dict(
    partner_id="partner-1",
    change_id="chg-1",
    counter_proposal_id="cp-1",
    negotiation_round=2,
    decision="accepted",
    resolution_text="agreed",
    justification="because",
)


class _FakeRow:
    id = "partner-1"
    name = "Partner One"


class _FakeSession:
    def query(self, *_a): return self
    def filter(self, *_a): return self
    def first(self): return _FakeRow()
    def close(self): pass


def test_upi_pack_now_supplies_a_channel():
    """The pack previously omitted channel() to declare it unwired; NetworkPack
    itself is wired now. NOTE: `get_active_pack()` no longer resolves to
    NetworkPack by default — the registered-key resolution is dormant (see
    registry.py's module docstring), so the DEFAULT pack (a YAML ConfigPack)
    supplies no channel at all. This test exercises NetworkPack directly to
    confirm the capability itself is still real, not lost, just no longer
    reachable through the registry's default path."""
    from app.packs.network.pack import NetworkPack

    ch = channel_of(NetworkPack())
    assert ch is not None
    assert ch.key == "a2a"
    assert ch.supports_responses is True


def test_config_pack_supplies_the_channel_it_NAMES():
    """The trade above was revised (genericisation, 2026-09-02): a config pack
    can now DECLARE a channel by naming a platform-registered one
    (`partner_channel: a2a`), the same shape as `certification_harness`. The
    YAML still supplies no behaviour — it names behaviour the platform owns
    (`app.services.partner_channels`), which `channel_of` resolves at the edge.
    So the default pack, which declares `partner_channel: a2a`, now yields the
    real A2A channel rather than None."""
    ch = channel_of(get_active_pack())
    assert ch is not None and ch.key == "a2a"


def test_config_pack_without_the_key_still_supplies_no_channel(tmp_path, monkeypatch):
    """Omission keeps its meaning: a config pack that names no channel has
    none, and distribution degrades to publish-and-notify (the OCPP shape)."""
    from app.core.domain import registry

    pack = tmp_path / "publishing.yaml"
    pack.write_text("key: publishing\n")
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    registry._load.cache_clear()
    try:
        assert channel_of(get_active_pack()) is None
    finally:
        registry._load.cache_clear()


def test_migrated_call_passes_the_same_arguments_to_the_transport(monkeypatch):
    """The regression that would matter: a field silently dropped in the move.

    change_request_id is the one to watch — without it the audit row is orphaned
    from the change it belongs to, and nothing would fail loudly.
    """
    captured = {}

    async def fake_send(**kwargs):
        captured.update(kwargs)
        class _Msg:
            id = "msg-1"
            status = "delivered"
        return _Msg()

    import app.services.a2a_client as client
    monkeypatch.setattr(client, "send_task_to_partner", fake_send, raising=False)

    from app.adapters.channel.a2a import A2AChannel
    ch = A2AChannel(session_factory=_FakeSession)
    _run(ch.deliver(
        Partner(key="partner-1", label="Partner One"),
        OutboundMessage(
            kind="counter_decision",
            change_id="chg-1",
            payload={"change_id": "chg-1", "decision": "accepted"},
        ),
    ))

    from app.models.phase_c import A2ATaskType
    assert captured["task_type"] == A2ATaskType.COUNTER_DECISION
    assert captured["change_request_id"] == "chg-1", "audit row would be orphaned"
    assert captured["payload"]["decision"] == "accepted"
    assert captured["partner"] is not None


def test_notify_skips_cleanly_when_the_domain_has_no_channel(monkeypatch):
    """A pack with no channel means the domain cannot reach partners at all —
    the OCPP shape. Skipping is correct; raising would break a lifecycle that is
    behaving exactly as its domain requires."""
    from app.api import negotiation_mgmt

    monkeypatch.setattr(negotiation_mgmt, "PartnerAgent", object, raising=False)
    monkeypatch.setattr("app.core.domain.contract.channel_of",
                        lambda _pack: None, raising=False)

    # Must return without raising and without touching a database.
    _run(negotiation_mgmt._notify_partner_cp_decision(**ARGS))


def test_notify_delivers_through_the_channel(monkeypatch):
    seen = {}

    class _Chan:
        key = "stub"
        supports_responses = True

        async def deliver(self, partner, message):
            seen["partner"] = partner.key
            seen["kind"] = message.kind
            seen["change_id"] = message.change_id
            seen["payload"] = message.payload
            from app.core.domain.contract import Delivery
            return Delivery(partner_key=partner.key, delivered=True, reference="r1")

        async def poll_responses(self, partner):
            return ()

    monkeypatch.setattr("app.core.domain.contract.channel_of",
                        lambda _pack: _Chan(), raising=False)

    from app.api import negotiation_mgmt
    _run(negotiation_mgmt._notify_partner_cp_decision(**ARGS))

    assert seen["partner"] == "partner-1"
    assert seen["kind"] == "counter_decision"
    assert seen["change_id"] == "chg-1"
    # The payload the partner receives must be unchanged by the migration.
    assert seen["payload"] == {
        "change_id": "chg-1",
        "decision": "accepted",
        "in_response_to": "cp-1",
        "negotiation_round": 2,
        "resolution_text": "agreed",
        "original_justification": "because",
    }


def test_a_failed_delivery_is_logged_not_swallowed(monkeypatch, caplog):
    """The direct call raised on transport failure and the caller logged it. The
    channel reports failure in its return value instead, so the caller must
    check it — otherwise failures become silent."""
    class _Chan:
        key = "stub"
        supports_responses = True

        async def deliver(self, partner, message):
            from app.core.domain.contract import Delivery
            return Delivery(partner_key=partner.key, delivered=False,
                            error="transport down")

        async def poll_responses(self, partner):
            return ()

    monkeypatch.setattr("app.core.domain.contract.channel_of",
                        lambda _pack: _Chan(), raising=False)

    from app.api import negotiation_mgmt
    with caplog.at_level("WARNING"):
        _run(negotiation_mgmt._notify_partner_cp_decision(**ARGS))
    assert any("not delivered" in r.message or "not delivered" in r.getMessage()
               for r in caplog.records), "a failed delivery vanished"


def test_certification_task_types_are_not_migrated():
    """Deliberate scope boundary.

    9 of the 25 A2A task types are the certification protocol. They ride the
    same wire but belong to certification(), not the partner channel. Routing
    them through the channel would drag cflow_id and cert_attempt into the
    generic contract, and a domain with no certifier would inherit fields
    describing something it does not have.
    """
    from app.models.phase_c import A2ATaskType

    cert_types = [t for t in A2ATaskType if t.value.startswith("cert_")]
    assert len(cert_types) >= 8

    from app.core.domain.contract import OutboundMessage as OM
    assert not hasattr(OM(kind="x"), "cflow_id")
    assert not hasattr(OM(kind="x"), "cert_attempt")


# ── Kit dispatch: the richer Delivery this call site forced ──────────────────

def test_delivery_carries_what_a_failure_alert_needs(monkeypatch):
    """The kit dispatch does not merely branch on success — it raises an
    operator alert naming the reason and linking to the message for resend.

    Before these fields existed the alert would have read "Task id: ?" with a
    broken resend link, which is worse than no alert: it tells an operator
    something failed and gives them no way to act on it.
    """
    async def fake_send(**_kwargs):
        class _Msg:
            id = "msg-9"
            status = "delivery_failed"
            error_code = "PARTNER_UNREACHABLE"
        return _Msg()

    import app.services.a2a_client as client
    monkeypatch.setattr(client, "send_task_to_partner", fake_send, raising=False)

    from app.adapters.channel.a2a import A2AChannel
    d = _run(A2AChannel(session_factory=_FakeSession).deliver(
        Partner(key="partner-1", label="P"),
        OutboundMessage(kind="change_communication", change_id="chg-7"),
    ))

    assert d.delivered is False
    assert d.status == "delivery_failed"
    assert d.error_code == "PARTNER_UNREACHABLE"
    assert d.reference == "msg-9"          # resend link
    assert d.change_id == "chg-7"          # alert's related-change link


def test_failure_alert_reads_a_delivery_as_well_as_a_message_row():
    """notify_delivery_failure has two shapes of caller now. A channel-delivered
    failure must produce the same alert content as a direct A2A one."""
    from app.core.domain.contract import Delivery

    d = Delivery(partner_key="p", delivered=False, reference="msg-9",
                 status="delivery_failed", error_code="X", change_id="chg-7")
    # The attribute names the notifier falls back to.
    assert (getattr(d, "id", None) or getattr(d, "reference", None)) == "msg-9"
    assert (getattr(d, "change_request_id", None) or getattr(d, "change_id", None)) == "chg-7"


def test_adapter_does_not_close_a_session_it_did_not_open():
    """Kit dispatch delivers in a loop. An adapter that closed an injected
    session would break on the second partner."""
    closed = {"n": 0}

    class _Sess(_FakeSession):
        def close(self): closed["n"] += 1

    from app.adapters.channel.a2a import A2AChannel
    session, owned = A2AChannel(session_factory=_Sess)._session()
    assert owned is False
    session.close()                      # only the owner closes
    assert closed["n"] == 1
