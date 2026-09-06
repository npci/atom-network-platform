# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Egress: an inbound A2A exchange → a local HTTP call (ITA I-4).

The REVERSE direction's far end: the partner's External API originates a
callback, the partner's ingress carries it here as `http_exchange_request`,
and THIS side resolves the alias against ITS OWN allowlist and performs the
HTTP call — typically to the Simulator's callback API. The structured reply
rides home through the executor's receipt merge (ITA-2).

Mirror of `atom-partner-platform .../integration_testing/egress.py` (ITA I-1's
forward egress) — deliberately per-service rather than vendored: the two
differ ONLY in the settings import (`app.core.config` here, `app.config`
there), and the vendoring MANIFEST carries files that are byte-identical, not
files with one divergent line. Keep the bodies in step when either changes.

THE THREE THINGS THIS MUST GET RIGHT

1. **Resolve locally, never from the payload.** `resolve_alias` takes an alias
   and a path; there is no parameter a URL could arrive through. An unknown
   alias is a hard rejection with no fallback (ITA §2).
2. **Honour the inbound deadline.** The caller sends the budget it has left as
   `deadline_ms`; this side takes the SMALLER of that and its own ceiling, and
   refuses outright if nothing useful remains. Ignoring it means the far side
   times out first and reports a generic transport failure instead of the real
   `target_timeout` (ITA §6).
3. **Never raise.** Every failure becomes a structured `error` payload the
   caller can assert on. An exception here would surface as a generic executor
   error, losing the code that says what went wrong.

BLOCKING BY DESIGN: this makes a synchronous HTTP call that may legitimately
take 60 seconds. The executor dispatches it via `asyncio.to_thread` — running
it inline would freeze the platform for a minute.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from app.a2a_common.integration_allowlist import (
    build_target_url, load_allowlist, resolve_alias,
)
from app.a2a_common.integration_contract import (
    ErrorCode, HttpResponseSpec, TunnelError, classify_headers, decode_request,
    encode_error, encode_response,
)
from app.core.config import settings

logger = logging.getLogger(__name__)

__all__ = ["perform_exchange"]

# Below this there is no point starting a call — better to say so than to burn
# the caller's remaining window and return a timeout it could have had sooner.
_MIN_USEFUL_MS = 250

# ── Per-alias resilience gates (ITA I-5) ─────────────────────────────────────
# One breaker + one bulkhead per ALIAS, not per platform: ten stuck calls to a
# dead simulator must not stop a healthy External API exchange, and vice
# versa. Instances are created lazily with the limits current at first use.
_GATE_LOCK = threading.Lock()
_GATES: dict[str, tuple] = {}


def _gate_for(alias: str):
    from app.core.resilience import Bulkhead, CircuitBreaker

    with _GATE_LOCK:
        if alias not in _GATES:
            _GATES[alias] = (
                CircuitBreaker(
                    f"tunnel_egress:{alias}",
                    failure_threshold=settings.integration_testing_breaker_failure_threshold,
                    cooldown_s=settings.integration_testing_breaker_cooldown_s,
                ),
                Bulkhead(
                    f"tunnel_egress:{alias}",
                    max_concurrent=settings.integration_testing_max_concurrent_per_alias,
                ),
            )
        return _GATES[alias]


def _reset_gates_for_tests() -> None:
    with _GATE_LOCK:
        _GATES.clear()


def _effective_timeout_s(deadline_ms: int | None) -> float:
    """The smaller of our ceiling and what the caller says is left."""
    ceiling = float(settings.integration_testing_target_timeout_s)
    if deadline_ms is None:
        return ceiling
    return min(ceiling, max(0.0, float(deadline_ms) / 1000.0))


def perform_exchange(payload: dict) -> dict:
    """Decode → resolve → call → encode. Returns a response or error payload."""
    exchange_id = str((payload or {}).get("exchange_id") or "unknown")

    if not settings.integration_testing_enabled:
        return encode_error(exchange_id=exchange_id, code=ErrorCode.TUNNEL_DISABLED,
                            detail="integration_testing_enabled is false on this platform")

    try:
        decoded = decode_request(
            payload,
            max_hops=settings.integration_testing_max_hops,
            max_body_bytes=settings.integration_testing_max_body_bytes,
        )
    except TunnelError as exc:
        logger.warning("tunnel egress exchange=%s rejected: %s", exchange_id, exc)
        return encode_error(exchange_id=exchange_id, code=exc.code, detail=exc.detail)

    exchange_id = decoded.exchange_id
    try:
        allowlist = load_allowlist(settings.integration_testing_allowlist)
        target = resolve_alias(allowlist, decoded.alias, decoded.request.path)
    except TunnelError as exc:
        logger.warning("tunnel egress exchange=%s alias=%s rejected: %s",
                       exchange_id, decoded.alias, exc)
        return encode_error(exchange_id=exchange_id, code=exc.code, detail=exc.detail)

    timeout_s = _effective_timeout_s(decoded.deadline_ms)
    if timeout_s * 1000 < _MIN_USEFUL_MS:
        return encode_error(
            exchange_id=exchange_id, code=ErrorCode.TARGET_TIMEOUT,
            detail=f"no useful budget remains (deadline_ms={decoded.deadline_ms})")

    # Drop hop-by-hop/recomputed headers plus this alias's strip list. httpx
    # sets Host and Content-Length for the new connection, which is exactly why
    # the originals must not be forwarded.
    forwarded, dropped = classify_headers(decoded.request.headers,
                                          strip=target.strip_headers)
    if dropped:
        logger.info("tunnel egress exchange=%s dropped header(s): %s",
                    exchange_id, [n for n, _ in dropped])

    url = build_target_url(target, decoded.request.path, decoded.request.query)
    started = time.perf_counter()

    # I-5: the per-alias gates. The bulkhead is entered with a SHORT wait — a
    # slot that does not free up almost immediately means the alias is
    # saturated, and queueing here would burn the caller's deadline on our
    # side of the wire. The breaker wraps the actual call so target failures
    # (timeouts included) count toward opening it.
    from app.core.resilience import CircuitOpenError

    breaker, bulkhead = _gate_for(decoded.alias)
    try:
        slot = bulkhead.acquire(timeout=1.0)
        slot.__enter__()
    except RuntimeError:
        return encode_error(
            exchange_id=exchange_id, code=ErrorCode.BULKHEAD_SATURATED,
            detail=f"{decoded.alias}: too many concurrent tunnelled calls "
                   f"(max {bulkhead.max_concurrent})")
    try:
        with breaker.call():
            with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
                # follow_redirects=False deliberately: a 302 to an address outside
                # the allowlist would walk straight around it, and the caller is
                # testing an API, not a browser.
                reply = client.request(
                    decoded.request.method,
                    url,
                    headers=list(forwarded),
                    content=decoded.request.body or None,
                )
    except CircuitOpenError:
        return encode_error(
            exchange_id=exchange_id, code=ErrorCode.CIRCUIT_OPEN,
            detail=f"{decoded.alias}: recent calls all failed — refusing fast; "
                   f"retry after {breaker.cooldown_s:.0f}s")
    except httpx.TimeoutException:
        return encode_error(exchange_id=exchange_id, code=ErrorCode.TARGET_TIMEOUT,
                            detail=f"{decoded.alias} did not respond in {timeout_s:.0f}s")
    except httpx.HTTPError as exc:
        return encode_error(exchange_id=exchange_id, code=ErrorCode.TARGET_UNREACHABLE,
                            detail=f"{decoded.alias}: {type(exc).__name__}: {exc}")
    finally:
        slot.__exit__(None, None, None)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    body = reply.content or b""
    if len(body) > settings.integration_testing_max_body_bytes:
        # §11.2 — validate the outbound response BEFORE putting it on the wire.
        return encode_error(
            exchange_id=exchange_id, code=ErrorCode.PAYLOAD_TOO_LARGE,
            detail=f"target response {len(body)}B exceeds the tunnel cap")

    resp_forwarded, _ = classify_headers(
        [(k, v) for k, v in reply.headers.multi_items()])
    logger.info("tunnel egress exchange=%s %s %s -> %d in %dms",
                exchange_id, decoded.request.method, decoded.alias,
                reply.status_code, elapsed_ms)
    return encode_response(
        exchange_id=exchange_id,
        response=HttpResponseSpec(status=reply.status_code,
                                  headers=tuple(resp_forwarded), body=body),
        elapsed_ms=elapsed_ms,
    )
