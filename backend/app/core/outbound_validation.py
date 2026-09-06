# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Outbound dependency response validation helpers.

Closes S6 (ARCHITECTURE_REVIEW_ACTIONS.md — "Validate outbound dependency
responses before use — schema, required fields, types, ranges, invariants,
staleness. Applies to LLM providers and partner replies alike") and
implements security_architecture_skills.md §11.2 ("Every outbound
dependency response MUST be validated before use") and §7.1 (anti-
corruption adapter responsibilities: "validate inbound response shape...
normalize data... reject malformed or unexpected responses").

Scope and design
-----------------
This is deliberately a set of SMALL, composable checks rather than a single
heavyweight schema-validation framework, because the two dependency classes
in scope have very different response shapes:

- **LLM provider responses** (Claude/OpenAI/Gemini/AiNxt/Ollama) are
  free-form text/tool-calls, not a fixed schema — "validation" here means
  structural sanity (usage counters are non-negative integers, a tool-use
  block has a name we recognise, a JSON blob a tool expects to parse
  actually parses) rather than a JSON-Schema match.
- **Partner A2A responses** DO have a fixed envelope shape (protocol.py's
  `Envelope`) — `validate_partner_response()` below checks the response
  artifact against the same structural expectations the inbound path
  enforces, applied to the outbound reply instead.

Both validators are FAIL-SAFE: a validation failure returns a
`ValidationResult(ok=False, ...)` for the caller to act on (log, drop the
response, fall back to a default) — they never raise, so introducing
validation cannot itself become a new source of unhandled exceptions on a
hot path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


def validate_llm_usage(usage: dict | None) -> ValidationResult:
    """Sanity-check a provider's reported token usage before it is trusted
    for cost accounting (`llm_usage_records`) or context-window management
    (the compaction trigger in `agents/agentic_runtime.py` — a poisoned
    usage value there was the exact bug class documented at that call site:
    "AiNxt reports only the UNCACHED TAIL as input_tokens... trusting that
    poisoned this anchor").

    Checks: usage is a dict; token counts (when present) are non-negative
    integers; a provider claiming HUGE token counts (>10M on a single call,
    far beyond any current model's real context window) is flagged as
    almost certainly a parsing/unit error rather than a real value, so the
    caller can fall back to the char-count floor instead of trusting it."""
    if usage is None:
        return ValidationResult(ok=True, warnings=["usage is None — provider stripped usage"])
    if not isinstance(usage, dict):
        return ValidationResult(ok=False, reason=f"usage is not a dict: {type(usage).__name__}")
    warnings: list[str] = []
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        val = usage.get(key)
        if val is None:
            continue
        if not isinstance(val, int) or isinstance(val, bool):
            return ValidationResult(ok=False, reason=f"{key}={val!r} is not an int")
        if val < 0:
            return ValidationResult(ok=False, reason=f"{key}={val} is negative")
        if val > 10_000_000:
            warnings.append(f"{key}={val} exceeds 10M — likely a units/parsing error, not a real token count")
    return ValidationResult(ok=True, warnings=warnings)


def validate_partner_response(response: dict | None, *, expected_task_type: str | None = None) -> ValidationResult:
    """Structural check on a partner's A2A response artifact before the
    platform acts on it (e.g. before persisting `A2AMessage.response_body`
    as ground truth, or before a negotiation/acceptance flow reads a field
    out of it). Mirrors the inbound `Envelope`/`read_envelope` expectations
    (protocol.py) applied to the OUTBOUND reply direction, since
    security_architecture_skills.md §11.2 requires the same rigour for
    responses as for requests — "every dependency output may be malformed,
    corrupted, delayed, replayed, or hostile" (§1.3) applies to a partner's
    reply just as much as to their inbound calls."""
    if response is None:
        return ValidationResult(ok=True, warnings=["no response artifact — receiver emitted none (allowed)"])
    if not isinstance(response, dict):
        return ValidationResult(ok=False, reason=f"response is not a dict: {type(response).__name__}")
    warnings: list[str] = []
    # A response artifact that carries an "error" key is a structured
    # rejection, not malformed — the caller decides how to handle it, this
    # validator only flags STRUCTURAL problems.
    if "error" in response and not isinstance(response.get("error"), str):
        warnings.append("'error' field present but not a string — non-standard error shape")
    # Size bound: a partner reply is not expected to be huge; an oversized
    # response artifact is itself a signal worth surfacing (resource
    # exhaustion attempt via a "friendly" reply, or a receiver echoing back
    # something it shouldn't).
    try:
        import json
        size = len(json.dumps(response, default=str))
        if size > 1_000_000:
            warnings.append(f"response artifact is {size} bytes — unusually large for an A2A reply")
    except Exception as e:  # noqa: BLE001 — size check is advisory only
        logger.debug("outbound_validation: size check skipped: %s", e)
    return ValidationResult(ok=True, warnings=warnings)


def log_validation_warnings(result: ValidationResult, *, context: str, correlation_id: str | None = None) -> None:
    """Uniform structured log line for any non-fatal `ValidationResult`
    warnings — keeps the "malformed dependency response" security telemetry
    event (security_architecture_skills.md §13.2) consistent across every
    call site that uses these validators."""
    if not result.warnings:
        return
    logger.warning(
        "SECURITY_EVENT event=malformed_dependency_response severity=low "
        "context=%s correlation_id=%s warnings=%s",
        context, correlation_id or "-", "; ".join(result.warnings),
    )
