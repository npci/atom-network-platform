# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2A channel — the existing network transport, behind the core Protocol.

A THIN adapter over `app.services.a2a_client.send_task_to_partner`, which stays
the single implementation of the wire: HMAC signing, bearer-JWT acquisition,
mTLS, retry/backoff, and the `A2AMessage` audit row. None of that is
reimplemented here, because none of it is generic — it is how *this* transport
happens to work, and duplicating it would create two things to keep correct.

Consumers so far: the counter-decision notify and the product-kit dispatch. The
CERTIFICATION task types (`cert_*`, 9 of the 25) deliberately do NOT come
through here — they belong to `certification()`, and routing them via the
partner channel would drag `cflow_id` / `cert_attempt` into the generic
contract.

Landing this adapter before migrating anything paid for itself twice: reading
the transport is what showed `deliver()` had the wrong signature (see
`OutboundMessage`), and the kit-dispatch call site is what showed `Delivery`
needed `status` / `error_code` / `change_id`.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Sequence

from app.core.domain.contract import Delivery, OutboundMessage, Partner, PartnerResponse

logger = logging.getLogger(__name__)


class A2AChannel:
    """Bidirectional, authenticated, machine-to-machine.

    `supports_responses = True`: partners reply with their own typed messages,
    which is what makes the negotiation loop possible.
    """

    key = "a2a"
    supports_responses = True

    def __init__(self, session_factory=None):
        # Injected so tests need no database, and so a caller that already holds
        # a session can share it instead of opening a second one per delivery.
        self._session_factory = session_factory

    def _session(self) -> tuple[Any, bool]:
        """Returns (session, we_opened_it).

        The adapter must close ONLY sessions it created. Closing an injected one
        would close the caller's session out from under them — and the kit
        dispatch delivers in a loop, so it would break on the second partner.
        """
        if self._session_factory is not None:
            return self._session_factory(), False
        from app.core.database import SessionLocal
        return SessionLocal(), True

    @staticmethod
    def _embed_attachments(message: OutboundMessage) -> dict[str, Any]:
        """Fold attachments into the payload as base64.

        The network wire format carries documents inline (see
        services/change_communication_wire.py). Other transports do not — the
        publish adapter writes them as files. Keeping this conversion in the
        ADAPTER rather than the contract is what lets both be true.
        """
        if not message.attachments:
            return dict(message.payload)
        payload = dict(message.payload)
        payload.setdefault("attachments", [])
        for name, blob in message.attachments.items():
            payload["attachments"].append({
                "filename": name,
                "b64": base64.b64encode(blob).decode("ascii"),
            })
        return payload

    async def deliver(self, partner: Partner, message: OutboundMessage) -> Delivery:
        from app.models.phase_c import A2ATaskType, PartnerAgent
        from app.services.a2a_client import send_task_to_partner

        try:
            task_type = A2ATaskType(message.kind)
        except ValueError:
            # The pack asked for a message kind this transport cannot carry.
            # Fail with the reason rather than guessing a nearest match.
            return Delivery(
                partner_key=partner.key, delivered=False,
                error=(f"A2A has no task type {message.kind!r}; "
                       f"known: {', '.join(t.value for t in A2ATaskType)}"),
            )

        db, owned = self._session()
        try:
            row = db.query(PartnerAgent).filter(
                PartnerAgent.id == partner.key
            ).first()
            if row is None:
                return Delivery(partner_key=partner.key, delivered=False,
                                error=f"no registered partner agent {partner.key!r}")

            msg = await send_task_to_partner(
                partner=row,
                task_type=task_type,
                payload=self._embed_attachments(message),
                db=db,
                change_request_id=message.change_id,
                correlation_id=message.correlation_id,
            )
            status = getattr(msg, "status", None)
            delivered = status == "delivered"
            error_code = getattr(msg, "error_code", None)
            return Delivery(
                partner_key=partner.key,
                delivered=delivered,
                reference=getattr(msg, "id", None),
                status=status,
                error_code=error_code,
                change_id=message.change_id,
                error=None if delivered else (error_code or f"status={status}"),
            )
        finally:
            if owned:
                db.close()

    async def poll_responses(self, partner: Partner) -> Sequence[PartnerResponse]:
        """A2A is PUSH, not poll: partners call our inbound handlers
        (`a2a_common/authority_handlers.py`) and the reply lands as an A2AMessage row.

        Returning empty here is therefore honest for this transport — there is
        nothing to poll. A channel whose replies arrive out-of-band still sets
        `supports_responses = True`, because the negotiation states are reachable;
        that flag is about whether replies EXIST, not how they are collected.
        """
        return ()
