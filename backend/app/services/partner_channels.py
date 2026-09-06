# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The platform's partner channels — named transports, selected by config.

The mirror of `cert_harnesses` for the DISTRIBUTION seam. A partner channel is
platform machinery bound to a TECHNOLOGY (A2A), not to a domain: the same
signed, authenticated wire carries a payments change or a library-loan change
without knowing which. It lived only in `app/adapters/channel/`, reachable
only through a Python pack's `channel()` method — so a YAML-configured domain
could never dispatch, only publish, however much it wanted the A2A transport.

A CONFIG pack now names a channel with `partner_channel: <key>` in its YAML —
the string keys into this registry, which owns the implementation. The pack
supplies the NAME of platform behaviour; the platform supplies the behaviour.
Omission still means "no machine-to-machine channel" (the OCPP shape:
publish-and-notify), and that stays a true statement, not an error.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["channel_by_key"]


def channel_by_key(key: str):
    """One named partner channel. An unknown key RAISES — dispatching over a
    different transport than the domain declared would send a partner's change
    somewhere it never agreed to receive it."""
    if key == "a2a":
        from app.adapters.channel.a2a import A2AChannel

        return A2AChannel()
    raise ValueError(f"unknown partner channel {key!r} — known: a2a")
