# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Send one message to one partner through the active domain pack's channel.

Extracted after the sixth occurrence, not the second. Every migrated call site
was repeating the same eight lines — resolve the pack, get the channel, handle
the no-channel case, build an OutboundMessage, deliver — and the no-channel
branch is the one that would rot if copied: a domain that publishes rather than
dispatches must not raise here, and that is easy to get wrong once per site.

This is NOT a wrapper around A2A. It resolves whatever channel the active pack
declares; for the network that happens to be A2A today.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.domain.contract import Delivery, OutboundMessage, Partner, channel_of
from app.core.domain.registry import get_active_pack

logger = logging.getLogger(__name__)


async def notify_partner(
    partner_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    change_id: str | None = None,
    label: str | None = None,
    correlation_id: str | None = None,
    context: str = "message",
) -> Delivery | None:
    """Deliver `payload` to one partner. Returns None when the domain has no channel.

    `kind` is the pack's message vocabulary (for the network, an A2ATaskType value).

    None means "this domain cannot reach partners at all" — the OCPP shape,
    where a spec is published and read rather than pushed. That is a legitimate
    domain, not a failure, so callers that merely notify should treat None as a
    no-op. A caller for whom delivery is the whole point (kit dispatch) should
    check for it and fail loudly instead.
    """
    channel = channel_of(get_active_pack())
    if channel is None:
        logger.info("%s not sent: active domain declares no partner channel", context)
        return None

    result = await channel.deliver(
        Partner(key=partner_id, label=label or partner_id),
        OutboundMessage(
            kind=kind,
            change_id=change_id,
            payload=payload,
            correlation_id=correlation_id,
        ),
    )
    if not result.delivered:
        # The direct A2A call raised on transport failure; the channel reports it
        # in the return value instead. Logging here means a caller that ignores
        # the result still leaves a trace, rather than failing silently.
        logger.warning(
            "%s NOT delivered: partner=%s change=%s status=%s error=%s",
            context, partner_id, change_id, result.status, result.error,
        )
    return result
