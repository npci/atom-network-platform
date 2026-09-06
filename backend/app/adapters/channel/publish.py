# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Publish channel — one-way file drop, no responses.

This is the OCPP shape, and it exists to keep the abstraction honest. A standards
body publishes a spec release; charge-point vendors read it. There is no agent to
call, no authenticated session, and nothing comes back. If `PartnerChannel` were
validated only against A2A it would silently assume a bidirectional session, and
the first non-network domain would not fit.

`supports_responses = False` is the load-bearing part: the lifecycle reads it and
skips the negotiation states entirely rather than waiting on replies that will
never arrive.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Sequence

from app.core.domain.contract import Delivery, OutboundMessage, Partner, PartnerResponse

logger = logging.getLogger(__name__)


class PublishChannel:
    """Write each message to a directory tree, one folder per partner.

    Deliberately dumb. A real deployment would point this at an object store, a
    static site, or a git repository — the point is that "delivery" can mean
    "made available" rather than "handed to a listening service".
    """

    key = "publish"
    supports_responses = False

    def __init__(self, root: str | Path):
        self.root = Path(root)

    async def deliver(self, partner: Partner, message: OutboundMessage) -> Delivery:
        try:
            out = self.root / partner.key / message.kind
            out.mkdir(parents=True, exist_ok=True)

            body = out / "message.json"
            body.write_text(json.dumps(message.payload, indent=2, default=str),
                            encoding="utf-8")

            # Attachments are written as real files rather than base64-embedded.
            # That is the whole reason OutboundMessage keeps them separate from
            # `payload`: each transport picks the representation that suits it.
            for name, blob in message.attachments.items():
                safe = Path(name).name          # never escape the partner dir
                if not safe:
                    continue
                (out / safe).write_bytes(blob)

            return Delivery(partner_key=partner.key, delivered=True,
                            reference=str(body))
        except OSError as exc:
            logger.warning("publish channel: delivery failed for %s: %s",
                           partner.key, exc)
            return Delivery(partner_key=partner.key, delivered=False, error=str(exc))

    async def poll_responses(self, partner: Partner) -> Sequence[PartnerResponse]:
        """Always empty — and that is a true statement about this transport, not
        a stub. Publishing does not create a reply path."""
        return ()
