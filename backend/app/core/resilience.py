# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-provider circuit breaker and bulkhead for outbound LLM calls.

Closes architecture review findings A1/A2 (Critical #1, #2 —
"No Circuit Breaker on LLM Provider Calls", "No Bulkhead Isolation Between
LLM Consumers") and implements security_architecture_skills.md §5.4 (Adapter
Layer MUST apply timeouts/bulkheads/circuit breakers) and §11.3 (mandatory
resilience patterns: circuit breakers stop repeated calls to failing
dependencies; bulkheads isolate pools/threads to contain blast radius).

Design notes
------------
- **Keyed by provider name** ("claude", "gemini", "openai", "ainxt",
  "ollama") so one degraded provider does not throttle or trip a breaker
  for another — this is the isolation EA_Skills.md P2 calls "shared-nothing
  concurrency" applied to outbound dependencies.
- **Circuit breaker states**: CLOSED (normal) -> OPEN (fail fast) ->
  HALF_OPEN (single probe allowed) -> CLOSED | OPEN. This is the classic
  three-state breaker referenced in EA_Skills.md P8 ("circuit open/half-open/
  closed paths").
- **Bulkhead**: a bounded `asyncio.Semaphore` per provider. Saturation
  raises `LlmBulkheadFullError` immediately rather than queuing unboundedly
  (EA_Skills.md P2 "avoidance of unbounded queues"; security §3.1
  "bounded queues/pools").
- All thresholds are externally configurable via `Settings` (never
  hardcoded), and callers should not construct `_ProviderState` directly —
  use `breaker_guard()` / `bulkhead_guard()` as async context managers around
  the existing per-provider `_call_*` dispatch in `core/llm.py`.
- State is process-local (module-level dict), which is correct for a
  circuit breaker: each worker process should independently observe and
  react to its own view of a dependency's health. A cluster-wide breaker
  would require a shared store (e.g. Redis) and is not needed at this
  fan-out; documented as a future evolution in
  docs/ARCHITECTURE_REVIEW_REMEDIATION.md ADR-004.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from app.core.config import settings

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class LlmCircuitOpenError(RuntimeError):
    """Raised instead of making a network call when a provider's circuit is OPEN.

    Callers (agentic loop, orchestrator transient-resume path) should treat
    this the same as any other transient/infra failure — see
    `app/agents/infra_errors.py::classify_infra_error` — and back off rather
    than retrying immediately, since retrying immediately is exactly the
    thundering-herd behaviour this breaker exists to prevent.
    """

    def __init__(self, provider: str, opened_for_s: float):
        self.provider = provider
        self.opened_for_s = opened_for_s
        super().__init__(
            f"circuit breaker OPEN for provider={provider!r} "
            f"(tripped {opened_for_s:.1f}s ago); failing fast without a network call"
        )


class LlmBulkheadFullError(RuntimeError):
    """Raised when a provider's concurrent-call bulkhead is saturated.

    This is a deliberate rejection, not a queue — per security_architecture_
    skills.md §11.4 (pull-architecture rule) and EA_Skills.md P2, an
    overloaded dependency should shed load with an explicit signal rather
    than accept unbounded queuing that hides the saturation from the caller.
    """

    def __init__(self, provider: str, limit: int):
        self.provider = provider
        self.limit = limit
        super().__init__(
            f"bulkhead full for provider={provider!r} "
            f"(limit={limit} concurrent calls already in flight)"
        )


@dataclass
class _ProviderState:
    """Circuit-breaker bookkeeping for a single provider. Not thread-safe by
    itself — callers must hold `_STATE_LOCK` while mutating."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    half_open_successes: int = 0
    semaphore: asyncio.Semaphore | None = None


_STATE: dict[str, _ProviderState] = {}
_STATE_LOCK = threading.Lock()


def _get_state(provider: str) -> _ProviderState:
    with _STATE_LOCK:
        st = _STATE.get(provider)
        if st is None:
            st = _ProviderState()
            _STATE[provider] = st
        return st


def _get_semaphore(provider: str) -> asyncio.Semaphore | None:
    """Lazily create the per-provider semaphore on first use inside a running
    event loop (asyncio.Semaphore binds no loop until awaited in modern
    asyncio, but we still guard construction with the state lock to avoid a
    race between two first-callers)."""
    limit = int(getattr(settings, "llm_max_concurrent_calls_per_provider", 0) or 0)
    if limit <= 0:
        return None
    st = _get_state(provider)
    if st.semaphore is None:
        with _STATE_LOCK:
            if st.semaphore is None:
                st.semaphore = asyncio.Semaphore(limit)
    return st.semaphore


def circuit_state(provider: str) -> CircuitState:
    """Read-only snapshot for diagnostics/health endpoints."""
    return _get_state(provider).state


def reset_circuit(provider: str) -> None:
    """Operator/admin escape hatch (e.g. a `/admin/llm/circuit/{provider}/reset`
    endpoint) — forces a provider back to CLOSED after a manual fix. Emits a
    security-relevant telemetry line since this is a control-plane action per
    security_architecture_skills.md §5.1 (emergency controls)."""
    with _STATE_LOCK:
        st = _get_state(provider)
        st.state = CircuitState.CLOSED
        st.consecutive_failures = 0
        st.half_open_successes = 0
    logger.warning("LLM_CIRCUIT_RESET | provider=%s | actor=admin | reason=manual_reset", provider)


def _before_call(provider: str) -> None:
    """Raise LlmCircuitOpenError if the breaker should fail this call fast."""
    if not getattr(settings, "llm_circuit_breaker_enabled", True):
        return
    st = _get_state(provider)
    cooldown = float(getattr(settings, "llm_circuit_breaker_cooldown_s", 30.0))
    with _STATE_LOCK:
        if st.state == CircuitState.OPEN:
            elapsed = time.monotonic() - st.opened_at
            if elapsed >= cooldown:
                # Cooldown elapsed — allow exactly one probe through.
                st.state = CircuitState.HALF_OPEN
                st.half_open_successes = 0
                logger.info("LLM_CIRCUIT_HALF_OPEN | provider=%s | cooldown_elapsed_s=%.1f",
                            provider, elapsed)
            else:
                raise LlmCircuitOpenError(provider, elapsed)
        # HALF_OPEN and CLOSED both allow the call through (HALF_OPEN allows
        # probes serially since asyncio is single-threaded per call site;
        # concurrent probes are still gated by the bulkhead above this).


def _on_success(provider: str) -> None:
    if not getattr(settings, "llm_circuit_breaker_enabled", True):
        return
    st = _get_state(provider)
    needed = int(getattr(settings, "llm_circuit_breaker_half_open_successes", 1))
    with _STATE_LOCK:
        if st.state == CircuitState.HALF_OPEN:
            st.half_open_successes += 1
            if st.half_open_successes >= needed:
                st.state = CircuitState.CLOSED
                st.consecutive_failures = 0
                st.half_open_successes = 0
                logger.info("LLM_CIRCUIT_CLOSED | provider=%s | recovered", provider)
        else:
            st.consecutive_failures = 0


def _on_failure(provider: str, exc: BaseException) -> None:
    if not getattr(settings, "llm_circuit_breaker_enabled", True):
        return
    st = _get_state(provider)
    threshold = int(getattr(settings, "llm_circuit_breaker_failure_threshold", 5))
    with _STATE_LOCK:
        if st.state == CircuitState.HALF_OPEN:
            # Probe failed — re-open immediately, reset cooldown clock.
            st.state = CircuitState.OPEN
            st.opened_at = time.monotonic()
            st.half_open_successes = 0
            logger.warning(
                "LLM_CIRCUIT_REOPEN | provider=%s | probe_failed error=%s", provider, exc,
            )
            return
        st.consecutive_failures += 1
        if st.consecutive_failures >= threshold and st.state == CircuitState.CLOSED:
            st.state = CircuitState.OPEN
            st.opened_at = time.monotonic()
            logger.error(
                "LLM_CIRCUIT_OPEN | provider=%s | consecutive_failures=%d threshold=%d error=%s",
                provider, st.consecutive_failures, threshold, exc,
            )


_PARTNER_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_PARTNER_SEM_LOCK = threading.Lock()


def _get_partner_semaphore(partner_id: str) -> asyncio.Semaphore | None:
    """T4 (THREAT_MODEL.md — 'No per-partner bulkhead on outbound calls').

    Deliberately a BULKHEAD ONLY, not the full breaker+bulkhead
    `guarded_call()` gives LLM providers: partner delivery failures
    already have their own purpose-built retry/backoff system
    (`a2a_client.py`'s exponential backoff, `MAX_DELIVERY_ATTEMPTS=5`,
    the DLQ-equivalent resend path) tuned for delivery semantics, not
    5xx-storm semantics. Layering the LLM-tuned circuit breaker on top
    would double up on that existing, already-reviewed mechanism rather
    than complementing it. The bulkhead alone closes the actual gap this
    threat names: one very slow (but not hard-timing-out) partner
    endpoint consuming a disproportionate share of the outbound HTTP
    connection pool relative to other partners."""
    limit = int(getattr(settings, "partner_max_concurrent_calls", 0) or 0)
    if limit <= 0:
        return None
    with _PARTNER_SEM_LOCK:
        sem = _PARTNER_SEMAPHORES.get(partner_id)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            _PARTNER_SEMAPHORES[partner_id] = sem
        return sem


@contextlib.asynccontextmanager
async def partner_bulkhead(partner_id: str):
    """Per-partner outbound bulkhead — closes THREAT_MODEL.md T4.

    Usage (in `a2a_client.py`, wrapping the httpx call to a partner's
    endpoint)::

        async with partner_bulkhead(partner.id):
            resp = await client.post(partner.endpoint_url, ...)

    Fails fast with `LlmBulkheadFullError` (reused — the error's meaning
    "this dependency's concurrency ceiling is saturated" is identical
    for a partner as for an LLM provider) if the partner's concurrency
    limit is saturated, rather than queuing unboundedly. A limit of 0
    (the default) disables this bulkhead entirely — same convention as
    `llm_max_concurrent_calls_per_provider`, so existing deployments see
    no behavior change until an operator opts in per-partner.
    """
    sem = _get_partner_semaphore(partner_id)
    if sem is None:
        yield
        return
    if sem.locked():
        raise LlmBulkheadFullError(
            f"partner:{partner_id}", int(getattr(settings, "partner_max_concurrent_calls", 0) or 0)
        )
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


@contextlib.asynccontextmanager
async def guarded_call(provider: str):
    """Combined bulkhead + circuit-breaker guard for one outbound LLM call.

    Usage (inside `core/llm.py`'s `call_llm`/`stream_llm` dispatch, wrapping
    the existing `_call_claude` / `_call_gemini` / `_do_call_openai_compat`
    invocation)::

        async with guarded_call(provider):
            response = await _call_claude(...)

    Raises `LlmBulkheadFullError` immediately if the provider's concurrency
    limit is saturated, or `LlmCircuitOpenError` immediately if the
    provider's breaker is OPEN — in both cases WITHOUT making a network
    call, so a degraded/overloaded provider cannot be piled onto further.
    On successful completion of the `with` body, records a breaker success;
    on any exception, records a breaker failure and re-raises unchanged.
    """
    sem = _get_semaphore(provider)
    if sem is not None and sem.locked():
        # `locked()` means zero permits are currently available — fail fast
        # (security_architecture_skills.md §11.4: reject, do not queue
        # unboundedly) rather than blocking the caller behind an unbounded
        # wait. A permit freed between this check and `acquire()` below is
        # a benign race — the acquire below would then succeed immediately.
        raise LlmBulkheadFullError(
            provider, int(getattr(settings, "llm_max_concurrent_calls_per_provider", 0) or 0)
        )
    _before_call(provider)
    if sem is not None:
        await sem.acquire()
    try:
        yield
    except BaseException as e:  # noqa: BLE001 — must observe every failure, then re-raise
        _on_failure(provider, e)
        raise
    else:
        _on_success(provider)
    finally:
        if sem is not None:
            sem.release()


# ── Generic sync-side breaker + bulkhead (ITA I-5) ───────────────────────────
# Ported from the partner platform's `app/core/resilience.py`, where these
# classes have guarded the `npci_a2a_outbound` boundary since Finding 12. The
# LLM-side machinery above is asyncio-flavoured and provider-keyed; these are
# the thread-side primitives the tunnel egress needs (it runs its blocking
# HTTP call in `asyncio.to_thread` workers). Port delta: the partner emits a
# security event when a circuit opens; this platform has no
# `core/security_events` module, so the WARNING log is the record here.
from contextlib import contextmanager as _contextmanager


class CircuitOpenError(RuntimeError):
    """The circuit is open — the dependency is considered unhealthy and the
    call is refused fast instead of burning the caller's timeout budget."""


class CircuitBreaker:
    """closed -> open (after N consecutive failures) -> half-open (after
    cooldown, allows ONE trial call) -> closed (on trial success) or open
    again (on trial failure)."""

    def __init__(self, name: str, *, failure_threshold: int, cooldown_s: float):
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._state = "closed"
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if self._state == "open" and (time.monotonic() - self._opened_at) >= self.cooldown_s:
            self._state = "half_open"
            logger.info("circuit_breaker[%s]: open -> half_open (cooldown elapsed)", self.name)

    @_contextmanager
    def call(self):
        with self._lock:
            self._maybe_half_open()
            if self._state == "open":
                raise CircuitOpenError(
                    f"circuit '{self.name}' is open — dependency considered unhealthy, "
                    f"failing fast instead of calling it"
                )
        try:
            yield
        except Exception:
            with self._lock:
                self._consecutive_failures += 1
                if self._state == "half_open" or self._consecutive_failures >= self.failure_threshold:
                    if self._state != "open":
                        logger.warning(
                            "circuit_breaker[%s]: -> open (%d consecutive failures, threshold=%d)",
                            self.name, self._consecutive_failures, self.failure_threshold,
                        )
                    self._state = "open"
                    self._opened_at = time.monotonic()
            raise
        else:
            with self._lock:
                if self._consecutive_failures or self._state != "closed":
                    logger.info("circuit_breaker[%s]: -> closed (call succeeded)", self.name)
                self._consecutive_failures = 0
                self._state = "closed"


class Bulkhead:
    """Bounded concurrency gate — a thin wrapper over threading.BoundedSemaphore
    with a non-blocking-with-timeout acquire so callers get an immediate,
    clear rejection instead of silently queueing forever."""

    def __init__(self, name: str, *, max_concurrent: int):
        self.name = name
        self.max_concurrent = max(1, max_concurrent)
        self._sem = threading.BoundedSemaphore(self.max_concurrent)

    @_contextmanager
    def acquire(self, *, timeout: float | None = 30.0):
        acquired = self._sem.acquire(blocking=True, timeout=timeout)
        if not acquired:
            raise RuntimeError(
                f"bulkhead '{self.name}' saturated (max_concurrent={self.max_concurrent}) — "
                f"rejecting instead of queueing unbounded"
            )
        try:
            yield
        finally:
            self._sem.release()
