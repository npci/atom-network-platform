# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Two transports, one Protocol — the test that keeps the abstraction honest.

An interface validated against a single implementation is that implementation
with extra steps. These two were chosen because they disagree about almost
everything: A2A is bidirectional, authenticated and push-based; publish is a
one-way file drop with no reply path at all.

The assertions that matter are the ones where they DIFFER.
"""
import asyncio

import pytest

from app.adapters.channel.a2a import A2AChannel
from app.adapters.channel.publish import PublishChannel
from app.core.domain.contract import (
    Delivery, OutboundMessage, Partner, PartnerChannel,
)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


PARTNER = Partner(key="p1", label="Partner One")


# ── Both satisfy the Protocol ────────────────────────────────────────────────

@pytest.mark.parametrize("channel", [
    PublishChannel("/tmp/unused"),
    A2AChannel(session_factory=lambda: None),
], ids=["publish", "a2a"])
def test_channel_satisfies_the_protocol(channel):
    assert isinstance(channel, PartnerChannel)
    assert channel.key
    assert isinstance(channel.supports_responses, bool)


def test_the_two_transports_disagree_about_responses():
    """If these ever match, one of them is being modelled wrong — and the
    abstraction has stopped being tested against genuine variation."""
    assert A2AChannel().supports_responses is True
    assert PublishChannel("/tmp/unused").supports_responses is False


# ── Publish: the OCPP shape ──────────────────────────────────────────────────

def test_publish_writes_payload_and_attachments(tmp_path):
    ch = PublishChannel(tmp_path)
    msg = OutboundMessage(
        kind="spec_release",
        payload={"version": "2.1", "summary": "adds a message type"},
        attachments={"migration-guide.md": b"# Migration\n"},
    )
    d = _run(ch.deliver(PARTNER, msg))

    assert d.delivered and d.partner_key == "p1"
    out = tmp_path / "p1" / "spec_release"
    assert (out / "message.json").exists()
    # Attachments land as FILES here, not base64 — the representation is the
    # transport's choice, which is why OutboundMessage keeps them separate.
    assert (out / "migration-guide.md").read_bytes() == b"# Migration\n"


def test_publish_never_returns_responses(tmp_path):
    """Empty because this transport has no reply path — a true statement about
    the domain, not an unimplemented stub."""
    assert _run(PublishChannel(tmp_path).poll_responses(PARTNER)) == ()


def test_publish_cannot_escape_the_partner_directory(tmp_path):
    ch = PublishChannel(tmp_path)
    msg = OutboundMessage(kind="k", attachments={"../../escaped.txt": b"x"})
    _run(ch.deliver(PARTNER, msg))
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert (tmp_path / "p1" / "k" / "escaped.txt").exists()


def test_publish_reports_failure_rather_than_raising(tmp_path):
    ch = PublishChannel(tmp_path / "file-not-a-dir")
    (tmp_path / "file-not-a-dir").write_text("blocks mkdir")
    d = _run(ch.deliver(PARTNER, OutboundMessage(kind="k")))
    assert d.delivered is False and d.error


# ── A2A: The network shape ───────────────────────────────────────────────────────

def test_a2a_rejects_a_message_kind_the_transport_cannot_carry():
    """The pack asked for something A2A has no task type for. Say so, with the
    known values — do not guess a nearest match and deliver the wrong thing."""
    d = _run(A2AChannel(session_factory=lambda: None)
             .deliver(PARTNER, OutboundMessage(kind="not_a_task_type")))
    assert d.delivered is False
    assert "no task type" in d.error


def test_a2a_embeds_attachments_as_base64():
    """The network wire carries documents inline. That conversion belongs in the
    ADAPTER — putting it in the contract would leak this wire format into every
    other domain."""
    msg = OutboundMessage(kind="x", payload={"a": 1},
                          attachments={"brd.docx": b"\x00binary"})
    payload = A2AChannel._embed_attachments(msg)
    assert payload["a"] == 1
    assert payload["attachments"][0]["filename"] == "brd.docx"
    import base64
    assert base64.b64decode(payload["attachments"][0]["b64"]) == b"\x00binary"


def test_a2a_leaves_payload_alone_when_there_are_no_attachments():
    msg = OutboundMessage(kind="x", payload={"a": 1})
    assert A2AChannel._embed_attachments(msg) == {"a": 1}


def test_a2a_reports_an_unregistered_partner():
    class _NoRow:
        def query(self, *_a): return self
        def filter(self, *_a): return self
        def first(self): return None
        def close(self): pass

    d = _run(A2AChannel(session_factory=_NoRow)
             .deliver(PARTNER, OutboundMessage(kind="change_communication")))
    assert d.delivered is False
    assert "no registered partner agent" in d.error


# ── The contract change this workstream forced ───────────────────────────────

def test_outbound_message_carries_a_typed_kind_not_just_files():
    """Regression guard on the contract correction.

    `deliver()` originally took `artifacts: Mapping[str, bytes]` — a one-way
    file drop. Real dispatch is a typed, bidirectional exchange
    (`send_task_to_partner(task_type=..., payload=...)`), and modelling it as
    files made counter-proposals and acknowledgements inexpressible.
    """
    msg = OutboundMessage(kind="counter_decision",
                          payload={"decision": "accept"},
                          correlation_id="c-1")
    assert msg.kind == "counter_decision"
    assert msg.payload["decision"] == "accept"
    assert msg.correlation_id == "c-1"
    assert msg.attachments == {}          # optional, not the primary channel


def test_delivery_distinguishes_failure_from_silence():
    ok = Delivery(partner_key="p", delivered=True, reference="msg-1")
    bad = Delivery(partner_key="p", delivered=False, error="transport down")
    assert ok.delivered and ok.error is None
    assert not bad.delivered and bad.error
