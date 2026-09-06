# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The certification trigger, AUTHORITY side (ITA §3.5 + §3.6.1).

§3.6.1's point is that the trigger contract is SYMMETRIC. A deployed
application is a subject under test, not a test driver — swap a simulator for
one and the control API that made "just call it" work disappears. That is as
true of this platform's own application as of the partner's:

    side     simulator mode          application mode
    ──────────────────────────────────────────────────────────
    partner  bank-sim control API    certification trigger
    NPCI     precert / pack-sim      **certification trigger**  ← this module

So this is the partner's `fire_trigger` pointed the other way, deliberately
sharing its shape: one versioned POST, bearer-authenticated, that returns
**202 and never a verdict**. The outcome arrives separately as the
application's real outbound call travelling through the tunnel. A trigger that
returned a result would let an application report a pass without ever making
the call, and the certification would be testing the trigger.

`reply_via` is an ALIAS, never a URL — the application's own tunnel ingress
resolves it, so no counterparty address is embedded in the system under test.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

import httpx

logger = logging.getLogger(__name__)

__all__ = ["fire_trigger"]

# A small control message that must not linger: the case's real execution runs
# on the suite deadline's clock, not this call's.
_TRIGGER_TIMEOUT_S = 10.0


def fire_trigger(
    trigger_url: str,
    trigger_secret: str | None,
    *,
    test_case_id: str,
    cert_context: Mapping[str, Any],
    case_data: Mapping[str, Any] | None,
    reply_via: str,
) -> bool:
    """Ask this platform's deployed application to originate `test_case_id`.

    Returns whether the trigger was ACCEPTED — never an outcome. Never
    raises: an unreachable application is a case that will not report, which
    the suite deadline already handles honestly.
    """
    headers = {}
    if trigger_secret:
        headers["Authorization"] = f"Bearer {trigger_secret}"
    body = {
        "test_case_id": test_case_id,
        "cert_context": dict(cert_context or {}),
        "case_data": dict(case_data or {}),
        "reply_via": reply_via,
    }
    try:
        with httpx.Client(timeout=_TRIGGER_TIMEOUT_S,
                          follow_redirects=False) as client:
            reply = client.post(trigger_url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        # Type + message only; the URL is operator-supplied and may carry
        # credentials in userinfo.
        logger.warning("authority cert trigger unreachable for case=%s: %s",
                       test_case_id, type(exc).__name__)
        return False

    if 200 <= reply.status_code < 300:
        logger.info("authority cert trigger accepted case=%s (HTTP %d)",
                    test_case_id, reply.status_code)
        return True
    logger.warning("authority cert trigger refused case=%s: HTTP %d",
                   test_case_id, reply.status_code)
    return False
