# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Ingress: a local HTTP request → an A2A exchange with the far platform.

This is the forward direction's near end (ITA I-1). A Simulator points at this
platform, and the request is carried verbatim to a target the FAR side resolves
against its own allowlist — the caller never names a URL.

What this module deliberately does NOT do:

* **It does not resolve the target.** The alias travels; the far side resolves
  it. Resolving here and sending a URL would make this platform the SSRF
  vector for the other one (ITA §2).
* **It does not touch the query string.** It hands the raw string to the
  contract encoder, which carries it opaquely. Contract selection rides on
  `?pack=`, and normalising it presents as "certified against baseline" — a
  false pass (ITA §12.5).
* **It does not retry.** A tunnelled POST is a business call on the far side;
  replaying it would duplicate whatever it did. The retry sweeper is excluded
  from these task types in ITA-5 for the same reason.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from app.a2a_common.integration_contract import (
    ErrorCode,
    HttpRequestSpec,
    HttpResponseSpec,
    TunnelError,
    classify_headers,
    decode_response,
    encode_request,
)
from app.a2a_common.protocol import A2ATaskType
from app.core.config import settings
from app.models.phase_c import PartnerAgent

logger = logging.getLogger(__name__)

__all__ = ["TunnelResult", "forward_exchange"]


class TunnelResult:
    """What the ingress route returns: either a response or a tunnel error."""

    def __init__(self, *, exchange_id: str, response: HttpResponseSpec | None = None,
                 error: Mapping[str, Any] | None = None, elapsed_ms: int | None = None):
        self.exchange_id = exchange_id
        self.response = response
        self.error = dict(error) if error else None
        self.elapsed_ms = elapsed_ms

    @property
    def failed(self) -> bool:
        return self.error is not None


def _error(exchange_id: str, code: str, detail: str) -> TunnelResult:
    logger.warning("tunnel exchange=%s failed code=%s detail=%s", exchange_id, code, detail)
    return TunnelResult(exchange_id=exchange_id, error={"code": code, "detail": detail})


async def forward_exchange(
    *,
    db: Session,
    partner: PartnerAgent,
    alias: str,
    method: str,
    path: str,
    query: str,
    headers: Sequence[Sequence[str]],
    body: bytes,
    cert_context: Mapping[str, Any] | None = None,
    exchange_id: str | None = None,
    change_request_id: str | None = None,
) -> TunnelResult:
    """Carry one HTTP exchange to `partner` and bring the response home.

    Returns a `TunnelResult` rather than raising: every failure the far side
    can name is a structured code the Simulator can assert on, and an exception
    here would collapse them all into a 500.
    """
    exchange_id = exchange_id or str(uuid.uuid4())
    dropped_names: list[str] = []

    def _finish(result: TunnelResult) -> TunnelResult:
        # I-9: one row per hop, best effort — a failed exchange must be
        # diagnosable from the row alone, without logs.
        from app.services.integration_testing.observability import record_exchange

        record_exchange(
            db, direction="ingress", exchange_id=result.exchange_id,
            alias=alias, method=method, path=path, query=query,
            status=result.response.status if result.response else None,
            error_code=(result.error or {}).get("code"),
            request_bytes=len(body or b""),
            response_bytes=len(result.response.body or b"") if result.response else 0,
            elapsed_ms=result.elapsed_ms,
            dropped_headers=dropped_names or None,
            cert_context=cert_context,
        )
        return result

    if not settings.integration_testing_enabled:
        return _finish(_error(exchange_id, ErrorCode.TUNNEL_DISABLED,
                              "integration_testing_enabled is false on this platform"))

    # Hop-by-hop and recomputed headers are stripped HERE, before the wire, so
    # the far side never has to guess which of them described our connection.
    forwarded, dropped = classify_headers(headers)
    dropped_names[:] = [n for n, _ in dropped]
    if dropped:
        # Logged per exchange: "my header vanished" is otherwise undiagnosable.
        logger.info("tunnel exchange=%s dropped %d hop-by-hop/recomputed header(s): %s",
                    exchange_id, len(dropped), dropped_names)

    budget_s = float(settings.integration_testing_a2a_timeout_s)
    try:
        payload = encode_request(
            exchange_id=exchange_id,
            alias=alias,
            request=HttpRequestSpec(
                method=method, path=path, query=query,
                headers=tuple((str(n), str(v)) for n, v in forwarded),
                body=body or b"",
            ),
            # The far side subtracts its own elapsed time from this and refuses
            # a call it cannot finish inside the remainder.
            deadline_ms=int(float(settings.integration_testing_target_timeout_s) * 1000),
            hop=1,
            cert_context=cert_context,
            max_body_bytes=settings.integration_testing_max_body_bytes,
        )
    except TunnelError as exc:
        return _finish(_error(exchange_id, exc.code, exc.detail))

    started = time.perf_counter()
    # Imported here so the module stays importable without the A2A SDK, which
    # the pure contract tests do not install.
    from app.services.a2a_client import send_task_to_partner

    message = await send_task_to_partner(
        partner,
        A2ATaskType.HTTP_EXCHANGE_REQUEST,
        payload,
        db,
        change_request_id,
        correlation_id=exchange_id,
        # The middle layer of the §6 budget. Without it the transport's own
        # 30s default fires below the 60s target ceiling and every slow case
        # fails as a transport error rather than a target timeout.
        timeout=budget_s,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if message.status != "delivered" or not message.response_body:
        return _finish(_error(exchange_id, ErrorCode.TARGET_UNREACHABLE,
                              f"A2A send status={message.status!r} (no reply body)"))

    body_out = message.response_body
    # The executor wraps a handler's dict under "message"; accept either shape
    # so this keeps working when ITA-2 makes the wrapping structured.
    if isinstance(body_out, Mapping) and "exchange_id" not in body_out:
        inner = body_out.get("message")
        if isinstance(inner, Mapping):
            body_out = inner

    try:
        decoded = decode_response(
            body_out, max_body_bytes=settings.integration_testing_max_body_bytes)
    except TunnelError as exc:
        return _finish(_error(exchange_id, exc.code, exc.detail))

    if decoded.failed:
        logger.info("tunnel exchange=%s far side returned %s", exchange_id,
                    decoded.error.get("code"))
        return _finish(TunnelResult(exchange_id=exchange_id, error=decoded.error,
                                    elapsed_ms=elapsed_ms))
    return _finish(TunnelResult(exchange_id=exchange_id, response=decoded.response,
                                elapsed_ms=decoded.elapsed_ms or elapsed_ms))
