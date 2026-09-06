# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Unified LLM abstraction — supports Claude, OpenAI, AiNxt, Ollama, and Gemini.

Switch provider via settings.llm_provider ("claude", "openai", "ainxt", "ollama",
or "gemini"). All agents use these functions instead of calling provider APIs
directly.

AiNxt is an internal, self-hosted gateway (set AINXT_BASE_URL). It exposes two
compatible APIs:
  - OpenAI-compatible: /chat/completions (default, proven)
  - Anthropic-compatible: /v1/messages (native tool_use, stop_reason preserved)

Toggle via settings.ainxt_compat_mode ("openai" or "anthropic"). When set to
"anthropic", AiNxt calls use the Anthropic SDK pointed at the AiNxt base URL,
giving native tool_use streaming and reliable stop_reason  .
"""
import contextvars
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from functools import lru_cache

from app.core.config import settings

logger = logging.getLogger(__name__)

_PROVIDER_ALIASES = {
    "anthropic": "claude",
    "google": "gemini",
    "google_ai": "gemini",
    "googleai": "gemini",
}


# ── R-9 — job_id propagation via ContextVar ──────────────────────────────────
#
# `current_job_id` lets a tracked WS handler / REST endpoint / docgen pipeline
# set the parent agent_jobs.id once at the top of its scope, after which every
# nested `call_llm` / `stream_llm` invocation in that async task automatically
# tags its observability trace with the same job_id — no need to thread the
# argument through every helper layer.
#
# Usage from a handler that already has a job_id:
#
#     token = current_job_id.set(registry_job_id)
#     try:
#         async for chunk in stream_llm(system=..., messages=..., agent_name='brd'):
#             ...
#     finally:
#         current_job_id.reset(token)
#
# Calls outside a set() scope produce trace lines with `job_id` absent
# (as before R-9). The contextvar is async-safe: each asyncio Task has its
# own copy, so a parallel BRD + TSD generation in two tabs of the same
# event loop won't cross-tag.

current_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_job_id", default=None,
)


# ── Slice 24+ — per-call usage capture via ContextVar ────────────────────────
#
# Inner provider helpers (`_call_claude`, `_stream_claude`, etc.) set this
# in their `finally` block with `{"input_tokens": int, "output_tokens": int,
# "cache_read_tokens": int, "cache_write_tokens": int}` pulled from the
# provider response's `.usage`. The outer `call_llm` / `stream_llm`
# read it in their `finally` block to forward into
# `record_llm_call_kwargs`. ContextVar (instead of a return-tuple) keeps
# the existing `_call_*` signatures intact — they still return just the
# response text — and is async-task-isolated so parallel calls don't
# cross-pollute.
# Appended to non-stream LLM output when the provider reports the max_tokens/length stop:
# partial text must never travel downstream LOOKING complete. Sits OUTSIDE any JSON braces,
# so brace-extraction recovery paths are unaffected (a truncated JSON body already fails to
# parse with or without it) — prose consumers see the cut instead of silently storing it.
TRUNCATION_MARKER = ("\n\n[⚠ OUTPUT TRUNCATED — the model hit its max_tokens cap; "
                     "the text above is INCOMPLETE]")

# Usage of the MOST RECENT completed call_llm/stream_llm on this async context — unlike
# current_llm_usage (an internal mailbox the dispatchers reset in their finally), this one
# PERSISTS so callers can check `last_call_usage().get("stop_reason")` after the call.
_last_llm_usage: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_last_llm_usage", default=None,
)


def last_call_usage() -> dict:
    """Provider-reported usage (incl. stop_reason) of the most recent LLM call on this
    async context. Empty dict when the provider stripped usage (AiNxt) or no call ran."""
    return _last_llm_usage.get() or {}


current_llm_usage: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_llm_usage", default=None,
)



def normalize_provider(provider: str | None) -> str:
    """Return the canonical provider name accepted by the dispatcher."""
    p = (provider or "claude").lower().strip()
    return _PROVIDER_ALIASES.get(p, p)


def get_provider() -> str:
    return normalize_provider(settings.llm_provider or "claude")


# ── Phase 6.2 — System-prompt coercion helper ────────────────────────────────
#
# Anthropic supports system prompts as a list of `{"type": "text", "text": ...,
# "cache_control": ...}` segments, with up to 4 of those segments marked for
# ephemeral caching. Other providers (OpenAI / AiNxt / Ollama) take a single
# string. `call_llm` / `stream_llm` accept either form so an agent can pass
# the segmented list once and have it routed correctly.
#
# Callers should use `app.core.prompt_blocks.segments_for_anthropic_cache` to
# build the list rather than hand-writing the dicts.

def _coerce_system_to_str(system) -> str:
    """Collapse a segmented system prompt into a single string for non-Claude
    providers (and for token-budget accounting). Idempotent on strings."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for seg in system:
            if isinstance(seg, dict):
                t = seg.get("text") or ""
            elif isinstance(seg, str):
                t = seg
            else:
                t = ""
            if t:
                parts.append(t)
        # The segment boundary is implicit on Anthropic's side; on a flat
        # string we re-introduce the convention used by `prompt_blocks.
        # assemble_system_prompt` so the cache hash matches when the same
        # agent later switches caching off.
        return "\n\n---\n\n".join(parts)
    if system is None:
        return ""
    return str(system)


def _system_for_claude(system):
    """Return the value to pass as `system=` to Anthropic SDK.
    Strings stay strings; segmented lists are passed through verbatim."""
    if isinstance(system, list):
        # Defensive: copy + filter to ensure each entry has at least a
        # "type" field. Anthropic rejects malformed segments.
        out: list[dict] = []
        for seg in system:
            if isinstance(seg, dict) and seg.get("text"):
                seg = seg if seg.get("type") else {**seg, "type": "text"}
                # Global kill-switch: drop cache_control so Anthropic treats the
                # system prompt as ordinary (uncached) input.
                if not settings.prompt_cache_enabled and "cache_control" in seg:
                    seg = {k: v for k, v in seg.items() if k != "cache_control"}
                out.append(seg)
        if out:
            return out
        # Fall back to string if the list collapsed to nothing useful.
    return _coerce_system_to_str(system)


def get_model(provider: str | None = None) -> str:
    """Return the configured model name for the given provider.

    `provider` is the caller's override (e.g. when routing one specific
    long-output agent direct to Claude while global is AiNxt). None falls
    back to the global `settings.llm_provider`.
    """
    p = normalize_provider(provider or get_provider())
    if p == "openai":
        return settings.openai_model or "gpt-4o"
    if p == "gemini":
        return settings.gemini_model or "gemini-3.5-flash"
    if p == "ainxt":
        if _ainxt_uses_anthropic():
            return settings.ainxt_messages_model or "claude-sonnet-5"
        return settings.ainxt_model or "gpt-5.4"
    if p == "ollama":
        return settings.ollama_chat_model or "phi3:mini"
    return settings.claude_model or "claude-sonnet-5"


# ── Claude client ────────────────────────────────────────────────────────────

def _sdk_client_timeout():
    """Shared explicit timeout for Claude/OpenAI-family SDK clients — see
    Settings.llm_client_*_timeout_s. Closes architecture review Finding #3
    ("No Explicit Timeout on Claude/OpenAI SDK Calls"): both SDKs previously
    fell back to their own default, so a hung non-streaming call could block
    indefinitely instead of failing fast. `httpx.Timeout` is accepted
    directly by both the `anthropic` and `openai` SDK constructors."""
    import httpx
    return httpx.Timeout(
        connect=float(getattr(settings, "llm_client_connect_timeout_s", 10.0)),
        read=float(getattr(settings, "llm_client_read_timeout_s", 300.0)),
        write=float(getattr(settings, "llm_client_write_timeout_s", 30.0)),
        pool=float(getattr(settings, "llm_client_pool_timeout_s", 10.0)),
    )


@lru_cache(maxsize=1)
def _get_anthropic_client():
    import anthropic
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=_sdk_client_timeout())


# ── OpenAI client ────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_openai_client():
    import openai
    return openai.AsyncOpenAI(api_key=settings.openai_api_key, timeout=_sdk_client_timeout())


# ── AiNxt clients ───────────────────────────────────────────────────────────
#
# The AiNxt gateway (AINXT_BASE_URL) serves TWO compatible APIs:
#
#   1. OpenAI-compatible (default):
#      POST /ainxt/v1/api/chat/completions
#      Uses the OpenAI SDK; settings.ainxt_compat_mode="openai"
#
#   2. Anthropic-compatible:
#      POST /ainxt/v1/api/v1/messages
#      Uses the Anthropic SDK; settings.ainxt_compat_mode="anthropic"
#      Benefits: native tool_use streaming, stop_reason preserved,
#      no double format translation.
#
# Auth: JWT or API key passed as x-api-key (Anthropic SDK) or
#       Authorization: Bearer (OpenAI SDK). AiNxt accepts both.


def _ainxt_uses_anthropic() -> bool:
    return (getattr(settings, "ainxt_compat_mode", "") or "").strip().lower() == "anthropic"


def _ainxt_base_url() -> str:
    """Resolve the AiNxt gateway base URL, or fail with an actionable message.

    There is deliberately no fallback. The previous inline default named an
    internal host, which meant a misconfigured deployment produced a DNS/connect
    error against someone else's network instead of saying what was wrong.
    """
    base_url = (settings.ainxt_base_url or "").rstrip("/").strip()
    if not base_url:
        raise RuntimeError(
            "LLM_PROVIDER is 'ainxt' but AINXT_BASE_URL is not set. "
            "Set it to your gateway's OpenAI-compatible base URL "
            "(e.g. https://gateway.example.com/v1/api) in the environment."
        )
    return base_url


@lru_cache(maxsize=1)
def _get_ainxt_client():
    """Cached AiNxt OpenAI-compat client (ainxt_compat_mode=openai)."""
    import openai
    base_url = _ainxt_base_url()
    api_key = settings.ainxt_api_key or "no-key"
    return openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=_sdk_client_timeout())


@lru_cache(maxsize=1)
def _get_ainxt_anthropic_client():
    """Cached AiNxt Anthropic-compat client (ainxt_compat_mode=anthropic).

    AiNxt's /v1/messages route authenticates via ``Authorization: Bearer``
    (same scheme as the proven OpenAI-compat path). The Anthropic SDK's
    default ``api_key`` → ``x-api-key`` header is rejected by the gateway
    (401), so we pass the credential as ``auth_token``, which the SDK sends
    as Bearer."""
    import anthropic
    base_url = _ainxt_base_url()
    api_key = settings.ainxt_api_key or "no-key"
    return anthropic.AsyncAnthropic(auth_token=api_key, base_url=base_url, timeout=_sdk_client_timeout())


# ── Ollama (OpenAI-compatible) ────────────────────────────────────────────────
# Ollama exposes an OpenAI-compatible chat endpoint at <ollama_url>/v1.
# Reuses the existing OpenAI dispatch helpers — same wire format.

@lru_cache(maxsize=1)
def _get_ollama_client():
    """Cached Ollama client. Reuses settings.ollama_url (also used by embeddings)
    and appends /v1 for OpenAI-compatible chat. api_key is unused by Ollama
    but the SDK requires a non-empty value. Caching prevents the per-call
    httpx client creation that triggered uvloop "Task exception" warnings."""
    import openai
    base = (settings.ollama_url or "http://localhost:11434").rstrip("/").strip()
    base_url = f"{base}/v1"
    return openai.AsyncOpenAI(api_key="ollama", base_url=base_url, timeout=_sdk_client_timeout())


# Gemini (Google AI Studio / Gemini Developer API) -------------------------
# Uses the native REST API instead of introducing a new SDK dependency.

def _message_content_to_text(content) -> str:
    """Best-effort flattening for provider-agnostic chat message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def _gemini_generation_config(model: str, max_tokens: int) -> dict:
    config = {"maxOutputTokens": int(max_tokens)}
    model_id = (model or "").lower().strip()
    if model_id.startswith("gemini-3"):
        level = (getattr(settings, "gemini_thinking_level", "minimal") or "").strip()
        if level:
            config["thinkingConfig"] = {"thinkingLevel": level}
    elif model_id.startswith("gemini-2.5"):
        budget = int(getattr(settings, "gemini_thinking_budget", 0))
        config["thinkingConfig"] = {"thinkingBudget": budget}
    return config


def _build_gemini_payload(system: str, messages: list[dict], max_tokens: int,
                          model: str = "gemini-3.5-flash") -> dict:
    """Translate the platform's chat shape into Gemini GenerateContent JSON."""
    system_parts = [system.strip()] if isinstance(system, str) and system.strip() else []
    contents: list[dict] = []

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").lower().strip()
        text = _message_content_to_text(msg.get("content")).strip()
        if not text:
            continue
        if role in {"system", "developer"}:
            system_parts.append(text)
            continue
        gemini_role = "model" if role in {"assistant", "model"} else "user"
        contents.append({"role": gemini_role, "parts": [{"text": text}]})

    if not contents:
        contents.append({"role": "user", "parts": [{"text": ""}]})

    payload = {
        "contents": contents,
        "generationConfig": _gemini_generation_config(model, max_tokens),
    }
    system_text = "\n\n".join(p for p in system_parts if p).strip()
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    return payload


def _parse_gemini_response(data: dict) -> tuple[str, str | None, dict]:
    """Extract text, finish reason, and usage from a Gemini response body."""
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {str(data)[:500]}")

    candidate = candidates[0] or {}
    content = candidate.get("content") or {}
    parts = content.get("parts") or []
    text = "".join(
        part.get("text") or ""
        for part in parts
        if isinstance(part, dict)
    ).strip()
    finish_reason = candidate.get("finishReason")
    usage = data.get("usageMetadata") or {}
    return text, finish_reason, usage


async def _call_gemini(system: str, messages: list[dict], model: str, max_tokens: int,
                       agent_name: str | None = None) -> str:
    """Native Gemini GenerateContent call with bounded transient retry."""
    import asyncio as _asyncio
    import random as _random
    import time as _time
    import uuid as _uuid

    import httpx

    api_key = (settings.gemini_api_key or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    model_id = (model or "gemini-3.5-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    payload = _build_gemini_payload(system, messages, max_tokens, model=model_id)
    max_retries = int(getattr(settings, "engine_rate_limit_max_retries", 5))
    corr_id = f"gm-{_uuid.uuid4().hex[:12]}"
    started = _time.perf_counter()

    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            try:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                break
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else 0
                retryable = status in {429, 500, 502, 503, 504}
                if not retryable or attempt >= max_retries:
                    elapsed_ms = int((_time.perf_counter() - started) * 1000)
                    body_excerpt = ""
                    try:
                        body_excerpt = (e.response.text or "")[:500]
                    except Exception:  # noqa: BLE001
                        pass
                    logger.error(
                        "GEMINI_HARD_ERROR | corr_id=%s model=%s agent=%s "
                        "http_status=%s elapsed_ms=%d attempts=%d body=%r",
                        corr_id, model_id, agent_name or "?",
                        status, elapsed_ms, attempt + 1, body_excerpt,
                    )
                    raise
                server_wait = _retry_after_seconds(e)
                backoff = _random.uniform(0, 2 ** attempt)
                wait_s = min(server_wait or backoff, 60.0)
                logger.warning(
                    "GEMINI_RETRY | corr_id=%s model=%s agent=%s "
                    "attempt=%d/%d wait=%.1fs status=%s server_retry_after=%s",
                    corr_id, model_id, agent_name or "?",
                    attempt + 1, max_retries + 1, wait_s, status, server_wait,
                )
                await _asyncio.sleep(wait_s)
                continue

    text, finish_reason, usage = _parse_gemini_response(data)
    if finish_reason == "MAX_TOKENS":
        logger.warning(
            "Gemini TRUNCATED at max_tokens=%d - model=%s agent=%s output_chars=%d",
            max_tokens, model_id, agent_name or "?", len(text),
        )
    try:
        current_llm_usage.set({
            "input_tokens":       usage.get("promptTokenCount"),
            "output_tokens":      usage.get("candidatesTokenCount"),
            "cache_read_tokens":  usage.get("cachedContentTokenCount"),
            "cache_write_tokens": None,
            "stop_reason":        finish_reason,
        })
    except Exception:  # noqa: BLE001
        pass
    return text


async def _stream_gemini(system: str, messages: list[dict], model: str, max_tokens: int,
                         agent_name: str | None = None) -> AsyncGenerator[str, None]:
    """Compatibility stream: yield Gemini's full response as one chunk."""
    text = await _call_gemini(system, messages, model, max_tokens, agent_name=agent_name)
    if text:
        yield text


# ── Unified API ──────────────────────────────────────────────────────────────

async def call_llm(
    system,
    messages: list[dict],
    max_tokens: int = 4000,
    model: str | None = None,
    agent_name: str | None = None,
    provider: str | None = None,
) -> str:
    """
    Call the LLM and return the full response text (non-streaming).

    Slice 27 — Optional `model` override lets per-agent routing send
    cheap workloads (taxonomy, query-enrich, link-scoring, summarisation)
    to lighter models while frontier reasoning stays on the default.
    None falls back to `get_model()` so flag-off behaviour is unchanged.

    Slice 28 — Optional `agent_name` tags the call for the observability
    trace. None records as "unknown"; the structured trace is emitted
    only when `settings.use_observability_traces` is True.

    Truncation-fix (2026-05-05) — Optional `provider` override. AiNxt
    gateway has been observed truncating response bodies at ~24KB on
    BOTH streaming and non-streaming paths. Long-output agents
    (planner, writer, etc.) can pass provider="claude" to route direct
    to Anthropic and bypass the gateway cap. None preserves prior
    `settings.llm_provider` behaviour.
    """
    provider = normalize_provider(provider or get_provider())
    chosen_model = model or get_model(provider=provider)

    logger.info("LLM call: provider=%s model=%s messages=%d", provider, chosen_model, len(messages))
    model = chosen_model

    # Phase 6.2 — `system` may be a segmented list for Anthropic cache_control.
    # For observability / non-Claude providers / budget checks we need the
    # collapsed string form.
    system_str = _coerce_system_to_str(system)

    # Slice 28 — wrap the dispatch with timing + structured trace emit.
    # Failure path also gets traced so cost-per-purpose dashboards can
    # see error rates per model.
    from app.core.observability import (
        estimate_prompt_chars, now_monotonic_seconds, record_llm_call_kwargs,
        log_cache_probe,
    )
    started = now_monotonic_seconds()
    prompt_chars = estimate_prompt_chars(system_str, messages)
    err: BaseException | None = None
    response = ""

    # Phase 0.3 — pre-call context-budget assertion. Raises ContextOverflowError
    # before we waste a network round-trip on an oversized prompt. Disabled
    # via `settings.use_context_budget_check=False` for the legacy path.
    try:
        from app.agents._context_packing import (assert_within_context, ContextOverflowError,
                                                 trim_messages_to_fit)
        if getattr(settings, "use_context_budget_check", True):
            try:
                assert_within_context(system_str, messages, max_response=max_tokens, model=chosen_model)
            except ContextOverflowError:
                # LAST-RESORT graceful degradation (wires the tested packer path into
                # production): drop the OLDEST pair-safe history turns until the prompt
                # fits, rather than failing the call outright. Single-shot callers (one
                # user message, nothing droppable) re-raise — behaviour unchanged there.
                messages, _dropped = trim_messages_to_fit(
                    system_str, messages, max_tokens, chosen_model)
                logger.warning("context overflow: trimmed %d oldest history turn(s) to fit "
                               "%s — agent=%s", _dropped, chosen_model, agent_name or "?")
    except ContextOverflowError:
        raise
    except Exception as _bc_err:
        logger.debug("budget check skipped due to error: %s", _bc_err)

    try:
        # A1/A2 — per-provider circuit breaker + bulkhead (core/resilience.py).
        # Fails fast (LlmCircuitOpenError / LlmBulkheadFullError) WITHOUT a
        # network call when the provider is tripped or saturated, instead of
        # piling another retry burst onto an already-degraded dependency.
        from app.core.resilience import guarded_call
        async with guarded_call(provider):
            if provider == "claude":
                response = await _call_claude(system, messages, model, max_tokens, agent_name=agent_name)
            elif provider == "gemini":
                # _call_gemini takes a flat str; pass system_str like the other
                # non-Claude providers (raw `system` may be a segmented list).
                response = await _call_gemini(system_str, messages, model, max_tokens, agent_name=agent_name)
            elif provider == "ainxt":
                if _ainxt_uses_anthropic():
                    response = await _call_claude(system, messages, model, max_tokens, agent_name=agent_name, client=_get_ainxt_anthropic_client())
                else:
                    response = await _call_openai_compat(_get_ainxt_client(), system_str, messages, model, max_tokens, agent_name=agent_name)
            elif provider == "ollama":
                response = await _call_openai_compat(_get_ollama_client(), system_str, messages, model, max_tokens)
            else:
                response = await _call_openai_compat(_get_openai_client(), system_str, messages, model, max_tokens, agent_name=agent_name)
        return response
    except BaseException as e:
        err = e
        raise
    finally:
        elapsed_ms = int((now_monotonic_seconds() - started) * 1000)
        response_chars = len(response or "")
        # Truncation diagnostic — one-line per-call summary so we can tell
        # whether the cap, the gateway, or a timeout is the real bottleneck.
        # Compare response_chars/4 ≈ output_tokens against max_tokens: if
        # close, the cap IS the limit; if well below, look elsewhere.
        logger.info(
            "LLM_DIAG agent=%s provider=%s model=%s streaming=false "
            "input_chars=%d output_chars=%d max_tokens=%d "
            "approx_output_tokens=%d cap_ratio=%.2f elapsed_ms=%d success=%s",
            agent_name or "unknown", provider, model,
            prompt_chars, response_chars, max_tokens,
            response_chars // 4, (response_chars / 4) / max_tokens if max_tokens else 0.0,
            elapsed_ms, err is None,
        )
        log_cache_probe(system, agent_name)   # flag-gated cache-viability probe (log-only)
        # Slice 24+ — usage was stashed by the inner provider helper
        # via `current_llm_usage.set(...)`. Pull and reset; pass into
        # the trace alongside the head/size pair.
        usage = current_llm_usage.get() or {}
        current_llm_usage.set(None)
        _last_llm_usage.set(usage)      # persists for callers — last_call_usage()
        record_llm_call_kwargs(
            agent_name=agent_name or "unknown",
            provider=provider, model=model,
            streaming=False,
            prompt_chars=prompt_chars,
            response_chars=response_chars,
            response_chunks=0,
            elapsed_ms=elapsed_ms,
            success=err is None,
            error=err,
            job_id=current_job_id.get(),    # R-9 — auto-tag from contextvar
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_tokens"),
            cache_write_tokens=usage.get("cache_write_tokens"),
            stop_reason=usage.get("stop_reason"),
        )
        # Per-change transcript capture — one JSON file per call under
        # <change_id>/<stage>/ (app.core.transcripts). Best-effort.
        try:
            from app.core import transcripts as _transcripts
            _transcripts.capture_llm_call(
                agent_name=agent_name, system=system, messages=messages,
                response_text=response or "", usage=usage, streaming=False,
            )
        except Exception:  # noqa: BLE001 — never break the LLM path
            pass
        # Dev-only devlog capture (app._devlog is git-ignored; no-op if absent).
        try:
            from app._devlog import capture as _dc
            _dc.record_llm_io(
                agent_name=agent_name, provider=provider, model=model,
                system=system, messages=messages, response=response,
                streaming=False, elapsed_ms=elapsed_ms,
                input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                cache_read_tokens=usage.get("cache_read_tokens"),
                cache_write_tokens=usage.get("cache_write_tokens"),
                stop_reason=usage.get("stop_reason"), success=err is None, error=err,
            )
        except Exception:
            pass


async def stream_llm(
    system,
    messages: list[dict],
    max_tokens: int = 4000,
    model: str | None = None,
    agent_name: str | None = None,
    provider: str | None = None,
    temperature: float | None = None,
) -> AsyncGenerator[str, None]:
    """
    Stream the LLM response token by token.

    Slice 27 — Optional `model` override; see `call_llm` docstring.
    Slice 28 — Optional `agent_name` for observability trace tagging.
    Truncation-fix — Optional `provider` override; see `call_llm` docstring.
    Phase 6.2 — `system` may be `str` or a list of Anthropic system segments
    with `cache_control` markers. Non-Claude providers see the collapsed
    string form.

    `temperature` — optional, Claude/ainxt-anthropic only (see `_stream_claude`).
    None leaves it unset, which is the API default (1.0) — unchanged behaviour
    for every caller that doesn't pass it. Non-Claude providers ignore it.
    """
    provider = normalize_provider(provider or get_provider())
    chosen_model = model or get_model(provider=provider)

    logger.info("LLM stream: provider=%s model=%s messages=%d", provider, chosen_model, len(messages))
    model = chosen_model

    # Phase 6.2 — see call_llm for rationale.
    system_str = _coerce_system_to_str(system)

    # Slice 28 — accumulate stream stats for the trace. Emit at end of
    # iteration (success path) or in `finally` (error path).
    from app.core.observability import (
        estimate_prompt_chars, now_monotonic_seconds, record_llm_call_kwargs,
        log_cache_probe,
    )
    started = now_monotonic_seconds()
    prompt_chars = estimate_prompt_chars(system_str, messages)
    response_chars = 0
    response_chunks = 0
    _resp_parts: list[str] = []   # accumulate streamed text for transcript capture
    err: BaseException | None = None

    # Dev-only full-response capture (app._devlog is git-ignored; no-op if absent).
    _dc = None
    _dl_buf = None
    try:
        from app._devlog import capture as _dc
        _dl_buf = []
    except Exception:
        _dc = None

    # Phase 0.3 — pre-stream context-budget assertion. See call_llm above.
    try:
        from app.agents._context_packing import (assert_within_context, ContextOverflowError,
                                                 trim_messages_to_fit)
        if getattr(settings, "use_context_budget_check", True):
            try:
                assert_within_context(system_str, messages, max_response=max_tokens, model=chosen_model)
            except ContextOverflowError:
                # LAST-RESORT graceful degradation (wires the tested packer path into
                # production): drop the OLDEST pair-safe history turns until the prompt
                # fits, rather than failing the call outright. Single-shot callers (one
                # user message, nothing droppable) re-raise — behaviour unchanged there.
                messages, _dropped = trim_messages_to_fit(
                    system_str, messages, max_tokens, chosen_model)
                logger.warning("context overflow: trimmed %d oldest history turn(s) to fit "
                               "%s — agent=%s", _dropped, chosen_model, agent_name or "?")
    except ContextOverflowError:
        raise
    except Exception as _bc_err:
        logger.debug("budget check skipped due to error: %s", _bc_err)

    try:
        # A1/A2 — same per-provider circuit breaker + bulkhead as call_llm.
        # Wraps the whole streamed response: a breaker/bulkhead trip surfaces
        # before the first chunk is requested, so a caller mid-stream never
        # gets cut off by a guard that should have stopped it from starting.
        from app.core.resilience import guarded_call
        async with guarded_call(provider):
            if provider == "claude":
                async for chunk in _stream_claude(system, messages, model, max_tokens, agent_name=agent_name, temperature=temperature):
                    response_chars += len(chunk or "")
                    response_chunks += 1
                    _resp_parts.append(chunk or "")
                    if _dl_buf is not None:
                        _dl_buf.append(chunk)
                    yield chunk
            elif provider == "gemini":
                # Pass system_str (flat) — see the _call_gemini note above.
                async for chunk in _stream_gemini(system_str, messages, model, max_tokens, agent_name=agent_name):
                    response_chars += len(chunk or "")
                    response_chunks += 1
                    _resp_parts.append(chunk or "")
                    if _dl_buf is not None:
                        _dl_buf.append(chunk)
                    yield chunk
            elif provider == "ainxt":
                if _ainxt_uses_anthropic():
                    async for chunk in _stream_claude(system, messages, model, max_tokens, agent_name=agent_name, client=_get_ainxt_anthropic_client(), temperature=temperature):
                        response_chars += len(chunk or "")
                        response_chunks += 1
                        _resp_parts.append(chunk or "")
                        if _dl_buf is not None:
                            _dl_buf.append(chunk)
                        yield chunk
                else:
                    async for chunk in _stream_openai_compat(_get_ainxt_client(), system_str, messages, model, max_tokens, agent_name=agent_name):
                        response_chars += len(chunk or "")
                        response_chunks += 1
                        _resp_parts.append(chunk or "")
                        if _dl_buf is not None:
                            _dl_buf.append(chunk)
                        yield chunk
            elif provider == "ollama":
                async for chunk in _stream_openai_compat(_get_ollama_client(), system_str, messages, model, max_tokens):
                    response_chars += len(chunk or "")
                    response_chunks += 1
                    _resp_parts.append(chunk or "")
                    if _dl_buf is not None:
                        _dl_buf.append(chunk)
                    yield chunk
            else:
                async for chunk in _stream_openai_compat(_get_openai_client(), system_str, messages, model, max_tokens, agent_name=agent_name):
                    response_chars += len(chunk or "")
                    response_chunks += 1
                    _resp_parts.append(chunk or "")
                    if _dl_buf is not None:
                        _dl_buf.append(chunk)
                    yield chunk
    except BaseException as e:
        err = e
        raise
    finally:
        elapsed_ms = int((now_monotonic_seconds() - started) * 1000)
        # Truncation diagnostic — see call_llm for rationale. cap_ratio close
        # to 1.0 means the cap was the limit; well below means look at AiNxt
        # gateway limits, network timeouts, or model early-stop instead.
        logger.info(
            "LLM_DIAG agent=%s provider=%s model=%s streaming=true "
            "input_chars=%d output_chars=%d chunks=%d max_tokens=%d "
            "approx_output_tokens=%d cap_ratio=%.2f elapsed_ms=%d success=%s",
            agent_name or "unknown", provider, model,
            prompt_chars, response_chars, response_chunks, max_tokens,
            response_chars // 4, (response_chars / 4) / max_tokens if max_tokens else 0.0,
            elapsed_ms, err is None,
        )
        log_cache_probe(system, agent_name)   # flag-gated cache-viability probe (log-only)
        # Slice 24+ — read usage stashed by inner _stream_claude /
        # _stream_openai_compat helpers.
        usage = current_llm_usage.get() or {}
        current_llm_usage.set(None)
        _last_llm_usage.set(usage)      # persists for callers — last_call_usage()
        record_llm_call_kwargs(
            agent_name=agent_name or "unknown",
            provider=provider, model=model,
            streaming=True,
            prompt_chars=prompt_chars,
            response_chars=response_chars,
            response_chunks=response_chunks,
            elapsed_ms=elapsed_ms,
            success=err is None,
            error=err,
            job_id=current_job_id.get(),    # R-9 — auto-tag from contextvar
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_tokens"),
            cache_write_tokens=usage.get("cache_write_tokens"),
            stop_reason=usage.get("stop_reason"),
        )
        # Per-change transcript capture — reassemble the streamed text and write
        # one JSON file under <change_id>/<stage>/ (app.core.transcripts). Best-effort.
        try:
            from app.core import transcripts as _transcripts
            _transcripts.capture_llm_call(
                agent_name=agent_name, system=system, messages=messages,
                response_text="".join(_resp_parts), usage=usage, streaming=True,
            )
        except Exception:  # noqa: BLE001 — never break the LLM path
            pass
        # Dev-only devlog capture (app._devlog is git-ignored; no-op if absent).
        if _dl_buf is not None and _dc is not None:
            try:
                _dc.record_llm_io(
                    agent_name=agent_name, provider=provider, model=model,
                    system=system, messages=messages, response="".join(_dl_buf),
                    streaming=True, elapsed_ms=elapsed_ms,
                    input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                    cache_read_tokens=usage.get("cache_read_tokens"),
                    cache_write_tokens=usage.get("cache_write_tokens"),
                    stop_reason=usage.get("stop_reason"), success=err is None, error=err,
                )
            except Exception:
                pass
    return  # final return so the duplicate provider blocks below don't double-yield


# Legacy provider-block code below was replaced by the wrapped version above.
# Kept module-private dispatch helpers (_call_claude / _stream_claude /
# _call_openai_compat / _stream_openai_compat) — they are unaffected.
async def _legacy_stream_dispatch_unused(system, messages, max_tokens, model):  # pragma: no cover
    provider = get_provider()
    if provider == "claude":
        async for chunk in _stream_claude(system, messages, model, max_tokens):
            yield chunk
    elif provider == "gemini":
        async for chunk in _stream_gemini(system, messages, model, max_tokens):
            yield chunk
    elif provider == "ainxt":
        if _ainxt_uses_anthropic():
            async for chunk in _stream_claude(system, messages, model, max_tokens, client=_get_ainxt_anthropic_client()):
                yield chunk
        else:
            async for chunk in _stream_openai_compat(_get_ainxt_client(), system, messages, model, max_tokens):
                yield chunk
    else:
        async for chunk in _stream_openai_compat(_get_openai_client(), system, messages, model, max_tokens):
            yield chunk


# ── Claude implementation ────────────────────────────────────────────────────

def _warn_if_truncated(stop_reason: str | None, model: str, max_tokens: int,
                       output_chars: int, agent_name: str | None) -> None:
    """Emit a WARNING log when Claude reports stop_reason='max_tokens'.

    Claude tends to be more verbose than GPT for the same prompt — agents
    that comfortably finish under their `max_tokens` cap on GPT can
    silently truncate on Claude. Surfacing this as a structured warning
    lets ops grep for affected agents and bump caps precisely instead
    of raising every cap globally.
    """
    if stop_reason == "max_tokens":
        logger.warning(
            "Claude TRUNCATED at max_tokens=%d — model=%s agent=%s output_chars=%d. "
            "Output was cut off; consider raising max_tokens at this call site.",
            max_tokens, model, agent_name or "?", output_chars,
        )


# Placeholder error strings the AiNxt gateway has been observed to return
# verbatim with HTTP 200 OK when an upstream timeout or proxy error fires.
# We treat these as transient failures (raise so the caller can retry or
# fall through to JSON-recovery), NOT as legitimate model output.
# Detected via 2026-05-03 production run where multiple docgen sections came
# back with response_chars=25 — exactly the length of "Error generating response".
_AINXT_ERROR_PLACEHOLDERS = frozenset([
    "Error generating response",
    "error generating response",
])


def _is_placeholder_error(text: str) -> bool:
    """True if `text` looks like an AiNxt-gateway placeholder error string."""
    return bool(text) and text.strip() in _AINXT_ERROR_PLACEHOLDERS


def _is_gateway_error_text(text: str) -> bool:
    """AiNxt can surface an UPSTREAM failure as a 200 whose assistant text is the error string:
    its OpenAI proxy turns an exception into a normal `finish_reason:"stop"` chunk with
    `delta.content="[LLM error: …]"` (docs/ainxt_messages_compat.md F15/caveat 6). Accepting
    that as content is how a failed review silently reads as a clean review — detect and raise
    instead. Prefix match (not equality): the bracket text carries the upstream message."""
    t = (text or "").strip()
    return _is_placeholder_error(t) or t.startswith("[LLM error:")


def _truncated_by_budget(stop_reason: str | None, output_tokens: int | None, max_tokens: int) -> bool:
    """AiNxt collapses EVERY truncation to stop_reason='end_turn' — OpenAI `length` and Claude's
    native `max_tokens` alike (docs/ainxt_messages_compat.md B8b/caveat 1) — so truncation is
    invisible from stop_reason through the gateway. `usage.output_tokens` IS real on every AiNxt
    path (D10): a turn that consumed its ENTIRE output budget was truncated to within a token.
    Synthesize the missing signal from that. Redundant-but-harmless on direct Anthropic (which
    already reports max_tokens correctly, so this never fires there)."""
    return stop_reason != "max_tokens" and bool(output_tokens) and int(output_tokens) >= int(max_tokens)


async def _call_claude(system, messages: list[dict], model: str, max_tokens: int,
                       agent_name: str | None = None, client=None) -> str:
    """Anthropic call with intelligent 429 retry.

    Optional `client` overrides the default Anthropic client — used by the
    AiNxt anthropic-compat path to point the same call logic at a different
    base_url.
    """
    import asyncio as _asyncio
    import random as _random
    import time as _time
    import uuid as _uuid

    max_retries = int(getattr(settings, "engine_rate_limit_max_retries", 5))

    try:
        import anthropic as _anthropic  # noqa: WPS433
        rate_limit_exc = (_anthropic.RateLimitError,)
    except Exception:  # noqa: BLE001
        rate_limit_exc = ()  # type: ignore[assignment]

    client = client or _get_anthropic_client()
    corr_id = f"cl-{_uuid.uuid4().hex[:12]}"
    started = _time.perf_counter()

    # Phase 6.2 — segment list with cache_control markers, OR string (legacy).
    claude_system = _system_for_claude(system)

    for attempt in range(max_retries + 1):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=claude_system,
                messages=messages,
            )
            break
        except rate_limit_exc as e:  # type: ignore[misc]
            if attempt >= max_retries:
                elapsed_ms = int((_time.perf_counter() - started) * 1000)
                logger.error(
                    "RATE_LIMIT_EXHAUSTED | corr_id=%s provider=claude model=%s "
                    "agent=%s elapsed_ms=%d attempts=%d exc=%s",
                    corr_id, model, agent_name or "?",
                    elapsed_ms, attempt + 1, repr(e)[:200],
                )
                raise
            server_wait = _retry_after_seconds(e)
            # Full jitter: uniform(0, 2^attempt). Equal jitter (1×–2× of base)
            # leaves a synchronized cluster of retries that all wake up
            # within the same window, which is exactly what the rate-limit
            # window is trying to prevent. Full jitter spreads them across
            # the entire band. See AWS Architecture Blog "Exponential
            # Backoff and Jitter" for the analysis.
            backoff = _random.uniform(0, 2 ** attempt)
            wait_s = min(server_wait or backoff, 60.0)
            logger.warning(
                "RATE_LIMIT_RETRY | corr_id=%s provider=claude model=%s agent=%s "
                "attempt=%d/%d wait=%.1fs server_retry_after=%s",
                corr_id, model, agent_name or "?",
                attempt + 1, max_retries + 1, wait_s, server_wait,
            )
            await _asyncio.sleep(wait_s)
            continue

    # Guard a degenerate/empty response before indexing. The Anthropic-compat path
    # (incl. AiNxt in anthropic mode) can return NO content blocks — e.g. a reasoning
    # model spent the whole max_tokens budget before emitting any text, or the gateway
    # returned an empty body. `response.content[0]` then raised a cryptic
    # `IndexError: list index out of range`; surface a clear, actionable error instead
    # (mirrors the empty-`choices` guard on the OpenAI-compat path). Joining only text
    # blocks also skips a leading `thinking` block, which has `.thinking`, not `.text`.
    _blocks = getattr(response, "content", None) or []
    text = "".join(
        getattr(b, "text", "") for b in _blocks if getattr(b, "type", None) == "text"
    ).strip()
    if not text:
        _stop = getattr(response, "stop_reason", None)
        raise RuntimeError(
            f"LLM returned no text content (provider=claude/ainxt model={model} "
            f"stop_reason={_stop!r} content_blocks={len(_blocks)} max_tokens={max_tokens} "
            f"agent={agent_name or '?'}). If stop_reason=='max_tokens', raise the agent's "
            f"max_tokens — a reasoning model can consume the whole budget before any text."
        )
    # AiNxt F15/caveat-6 guard: upstream failure delivered as a 200 with the error string as
    # assistant text — raise loudly rather than hand an error message to the caller as content.
    if _is_gateway_error_text(text):
        raise RuntimeError(f"gateway returned an upstream error as assistant content "
                           f"(docs/ainxt_messages_compat.md F15): {text[:300]}")
    _warn_if_truncated(getattr(response, "stop_reason", None), model, max_tokens,
                       len(text), agent_name)
    if getattr(response, "stop_reason", None) == "max_tokens":
        # Typed-at-the-text-level failure signal: partial output must never pass downstream
        # looking complete (callers can also check last_call_usage()["stop_reason"]).
        text += TRUNCATION_MARKER
    # Slice 24+ — capture provider-reported usage for the trace.
    # Anthropic returns input_tokens / output_tokens (always) plus
    # cache_creation_input_tokens / cache_read_input_tokens when prompt
    # caching is in play. Stash on the contextvar; the outer call_llm's
    # finally block reads it.
    try:
        usage = getattr(response, "usage", None)
        current_llm_usage.set({
            "input_tokens":       getattr(usage, "input_tokens",  None) if usage else None,
            "output_tokens":      getattr(usage, "output_tokens", None) if usage else None,
            "cache_read_tokens":  getattr(usage, "cache_read_input_tokens",     None) if usage else None,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", None) if usage else None,
            # `stop_reason` (e.g. end_turn / max_tokens) — captured here
            # so the JSONL trace reveals truncation cause without a
            # second debugging pass.
            "stop_reason":        getattr(response, "stop_reason", None),
        })
    except Exception:  # noqa: BLE001 — never break the call on telemetry
        pass
    return text


# ── Tool-use (the agentic loop) — Anthropic only (§8/§10) ─────────────────────
#
# The agentic codegen loop (S6) needs structured tool-use: the model returns
# tool_use blocks, the runtime executes them and feeds tool_result back, repeat.
# The legacy call_llm/stream_llm return a plain string and are unchanged — this
# is a separate, Claude-only entry point (the agentic feature is Anthropic-only).
#
# Prompt caching: the system prompt already routes cache_control segments
# (_system_for_claude). Here we ALSO cache the tools block — large and static
# across loop turns — by marking the last tool ephemeral, so every turn after
# the first re-reads tools + system from cache and pays only for the growing
# message tail. cache_read/write tokens are captured + traced so hit-rate shows.

@dataclass
class ToolUseRequest:
    id: str
    name: str
    input: dict


@dataclass
class ClaudeToolTurn:
    """One assistant turn that may request tool calls."""
    text: str                       # concatenated text blocks (model prose)
    tool_uses: list[ToolUseRequest]
    stop_reason: str | None
    assistant_content: list[dict]   # raw blocks — append verbatim to messages
    usage: dict = field(default_factory=dict)
    thinking: str = ""              # concatenated extended-thinking text (if enabled)

    @property
    def wants_tools(self) -> bool:
        return self.stop_reason == "tool_use" or bool(self.tool_uses)


def _tools_with_cache(tools: list[dict]) -> list[dict]:
    """Copy `tools`, marking the LAST one ephemeral so Anthropic caches the whole
    tools prefix (the tool set is static across loop turns)."""
    if not tools:
        return tools
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


def _messages_with_cache_tail(messages: list[dict]) -> list[dict]:
    """Copy `messages`, marking the LAST content block of the LAST message ephemeral
    so Anthropic caches the ENTIRE conversation prefix — every turn after the first
    re-reads the prior file reads / tool results from cache instead of reprocessing
    them as fresh input tokens (Lever 1). Rolling: the conversation only grows by
    appending, so each turn the breakpoint moves to the new tail and the older prefix
    stays a cache hit.

    Accuracy-neutral by construction: the breakpoint changes nothing the model SEES —
    it replays byte-identical tokens. A content string is promoted to a single text
    block so the marker has somewhere to live (Anthropic requires cache_control on a
    block, not a bare string)."""
    if not messages:
        return messages
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return messages
        last["content"] = [{"type": "text", "text": content,
                            "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        blocks = list(content)
        for idx in range(len(blocks) - 1, -1, -1):     # mark the last DICT block
            if isinstance(blocks[idx], dict):
                blocks[idx] = {**blocks[idx], "cache_control": {"type": "ephemeral"}}
                break
        else:
            return messages                            # no dict block to mark
        last["content"] = blocks
    else:
        return messages
    out[-1] = last
    return out


def _trim_system_cache_breakpoints(claude_system, keep: int):
    """Keep cache_control on at most the LAST ``keep`` system segments, dropping it from
    earlier ones. Anthropic caps a request at 4 cache breakpoints; a multi-segment system
    prompt can spend the whole budget (observed: 3 static system segments + the tools block
    = 4), which then STARVES the rolling message-tail cache — the largest, growing, and
    highest-value cache in a tool loop. Trimming the earliest system breakpoints frees the
    budget for tools + tail. Accuracy-neutral: the full static system still caches from the
    last retained breakpoint (Anthropic caches the whole prefix up to a breakpoint)."""
    if not isinstance(claude_system, list) or keep < 0:
        return claude_system
    marked = [i for i, seg in enumerate(claude_system)
              if isinstance(seg, dict) and seg.get("cache_control")]
    if len(marked) <= keep:
        return claude_system
    drop = set(marked[:len(marked) - keep])       # drop the EARLIEST, keep the last `keep`
    return [({k: v for k, v in seg.items() if k != "cache_control"} if i in drop else seg)
            for i, seg in enumerate(claude_system)]


@lru_cache(maxsize=1)
def _claude_supports_thinking() -> bool:
    """Whether the installed Anthropic SDK accepts the `thinking` kwarg (extended
    thinking landed ~0.47). Feature-detected so an older SDK degrades to no-thinking
    instead of 400-ing the call — the agent still runs, just without reasoning blocks."""
    try:
        import inspect as _inspect
        import anthropic as _anthropic
        from anthropic.resources.messages import Messages as _Messages
        if "thinking" in _inspect.signature(_Messages.create).parameters:
            return True
        major_minor = tuple(int(x) for x in (_anthropic.__version__.split(".")[:2]))
        return major_minor >= (0, 47)
    except Exception:  # noqa: BLE001 — detection must never break the call path
        return False


def _count_system_cache_breakpoints(claude_system) -> int:
    """Number of cache_control markers already on the system prompt (segments)."""
    if isinstance(claude_system, list):
        return sum(1 for seg in claude_system if isinstance(seg, dict) and seg.get("cache_control"))
    return 0


def _assistant_blocks_to_dicts(content) -> tuple[str, str, list["ToolUseRequest"], list[dict]]:
    """Flatten Anthropic response.content into (text, thinking, tool_uses, dict blocks).
    The dict blocks are re-sendable as the assistant turn in the next request.

    Thinking / redacted_thinking blocks are PRESERVED verbatim (incl. the signature):
    with extended thinking + tool use, Anthropic requires the signed thinking blocks
    be echoed back in the next request's assistant turn, or it 400s. Text blocks are
    joined WITHOUT stripping — the agentic loop preserves the model's exact prose."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_uses: list[ToolUseRequest] = []
    blocks: list[dict] = []
    for block in content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            t = getattr(block, "text", "") or ""
            text_parts.append(t)
            blocks.append({"type": "text", "text": t})
        elif btype == "tool_use":
            tu = ToolUseRequest(id=block.id, name=block.name, input=dict(getattr(block, "input", {}) or {}))
            tool_uses.append(tu)
            blocks.append({"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input})
        elif btype == "thinking":
            thinking_parts.append(getattr(block, "thinking", "") or "")
            blocks.append({"type": "thinking", "thinking": getattr(block, "thinking", "") or "",
                           "signature": getattr(block, "signature", "")})
        elif btype == "redacted_thinking":
            blocks.append({"type": "redacted_thinking", "data": getattr(block, "data", "")})
    return "".join(text_parts), "".join(thinking_parts), tool_uses, blocks


async def _claude_create_with_retry(make_call, *, model, agent_name, corr_id):
    """Run `make_call()` (an Anthropic messages.create coroutine factory) with the
    same full-jitter 429 backoff as `_call_claude`. Kept separate so the legacy
    path stays byte-for-byte unchanged."""
    import asyncio as _asyncio
    import random as _random

    max_retries = int(getattr(settings, "engine_rate_limit_max_retries", 5))
    _rl = ()  # rate-limit type, for retry-after handling
    try:
        import anthropic as _anthropic
        _rl = (_anthropic.RateLimitError,)
        # Transient = absorbable network/API blips: retry in-loop with backoff so a
        # short outage never bubbles up as a run failure.
        transient_exc = (_anthropic.RateLimitError, _anthropic.APIConnectionError,
                         _anthropic.APITimeoutError, _anthropic.InternalServerError)
    except Exception:  # noqa: BLE001
        transient_exc = ()  # type: ignore[assignment]

    for attempt in range(max_retries + 1):
        try:
            return await make_call()
        except transient_exc as e:  # type: ignore[misc]
            if attempt >= max_retries:
                logger.error("TRANSIENT_EXHAUSTED | corr_id=%s provider=claude model=%s agent=%s attempts=%d err=%s",
                             corr_id, model, agent_name or "?", attempt + 1, type(e).__name__)
                raise
            server_wait = _retry_after_seconds(e) if isinstance(e, _rl) else None
            wait_s = min(server_wait or _random.uniform(0, 2 ** attempt), 60.0)
            logger.warning("TRANSIENT_RETRY | corr_id=%s model=%s err=%s attempt=%d/%d wait=%.1fs",
                           corr_id, model, type(e).__name__, attempt + 1, max_retries + 1, wait_s)
            await _asyncio.sleep(wait_s)
    raise RuntimeError("unreachable: retry loop exhausted without return/raise")


async def call_llm_structured(
    system,
    user: str,
    *,
    schema: dict,
    agent_name: str,
    tool_name: str = "record_output",
    tool_description: str = "Record the structured result.",
    model: str | None = None,
    max_tokens: int = 4000,
):
    """Structured JSON output with the malformed-JSON failure class removed where possible.

    On providers with native Anthropic tool support (claude / ainxt anthropic-compat) the
    ``schema`` is enforced via a FORCED tool call — the API validates the arguments, so the
    prose-JSON failure modes (markdown fences, trailing commas, truncation mid-string) and
    the json_recovery repair call that used to fix them simply never happen. On every other
    provider it falls back to the legacy prose-JSON + ``parse_llm_json`` path, so callers
    stay provider-agnostic (the ollama/openai overlays keep working unchanged).

    ``system`` may be a string or a cache-segmented list (same contract as call_llm).
    Returns the parsed object (the tool input on the forced path), or None on failure —
    callers keep their existing fail-open handling.
    """
    provider = normalize_provider(settings.llm_provider)
    if provider == "claude" or (provider == "ainxt" and _ainxt_uses_anthropic()):
        tool = {"name": tool_name, "description": tool_description, "input_schema": schema}
        turn = await call_claude_tools(
            system=system, messages=[{"role": "user", "content": user}],
            tools=[tool], tool_choice={"type": "tool", "name": tool_name},
            model=model, max_tokens=max_tokens, agent_name=agent_name)
        for tu in turn.tool_uses:
            if tu.name == tool_name and isinstance(tu.input, dict):
                return tu.input
        logger.warning("call_llm_structured[%s]: forced tool returned no %s input",
                       agent_name, tool_name)
        return None
    from app.core.json_recovery import parse_llm_json
    raw = await call_llm(system, [{"role": "user", "content": user}], max_tokens,
                         model=model, agent_name=agent_name)
    return await parse_llm_json(raw, fallback=None)


async def call_claude_tools(
    *,
    system,
    messages: list[dict],
    tools: list[dict],
    model: str | None = None,
    max_tokens: int = 8192,
    tool_choice: dict | None = None,
    agent_name: str | None = None,
    cache_tools: bool = True,
    thinking_budget: int | None = None,
) -> ClaudeToolTurn:
    """One Anthropic tool-use turn. Returns the assistant text + any tool_use
    requests + the raw assistant blocks (append before sending tool_results).

    Caller drives the loop: send messages+tools → if `wants_tools`, append
    `assistant_content`, execute each tool_use, append a user turn of
    tool_result blocks, repeat; else the run produced its final text.

    Requires provider='claude' or 'ainxt' with ainxt_compat_mode='anthropic'.
    Both paths use the Anthropic SDK — direct for Claude, via AiNxt gateway
    for ainxt (same /v1/messages protocol).
    """
    import time as _time
    import uuid as _uuid

    effective_provider = normalize_provider(settings.llm_provider)
    if effective_provider == "ainxt" and _ainxt_uses_anthropic():
        chosen_model = model or get_model(provider="ainxt")
        client = _get_ainxt_anthropic_client()
    elif effective_provider == "claude":
        chosen_model = model or get_model(provider="claude")
        client = _get_anthropic_client()
    else:
        raise ValueError(
            f"call_claude_tools requires provider='claude' or 'ainxt' "
            f"(anthropic compat mode), got '{effective_provider}'. "
            "Route through call_llm for other providers."
        )
    claude_system = _system_for_claude(system)
    # Reserve cache-breakpoint budget (max 4) for the ROLLING MESSAGE TAIL — the largest,
    # growing, highest-value cache in a tool loop. A multi-segment system prompt + the tools
    # block can spend all 4 breakpoints, starving the tail so the whole transcript is re-billed
    # UNCACHED every turn (observed: cache-read ~18% and falling as the transcript grows). Keep
    # ≤2 system breakpoints so system(≤2) + tools(1) + tail(1) ≤ 4; the static system still
    # caches from its last retained breakpoint (accuracy-neutral).
    cache_on = settings.prompt_cache_enabled          # global kill-switch
    if cache_on and settings.agentic_cache_message_tail:
        claude_system = _trim_system_cache_breakpoints(claude_system, keep=2)
    # Anthropic allows at most 4 cache breakpoints per request (tools + system +
    # messages). If the caller's system already spends the full budget, skip the
    # tools breakpoint rather than 400 the whole request mid-loop.
    use_tool_cache = cache_on and cache_tools and _count_system_cache_breakpoints(claude_system) < 4
    send_tools = _tools_with_cache(tools) if use_tool_cache else tools
    # Rolling message-tail cache (Lever 1, gated): cache the growing conversation
    # prefix so each turn reprocesses only the newest turn, not the whole transcript.
    # Only if a breakpoint slot is free (system + tools + 1 tail ≤ 4) — else skip
    # rather than 400 the request.
    used_breakpoints = _count_system_cache_breakpoints(claude_system) + (1 if use_tool_cache else 0)
    send_messages = messages
    if cache_on and settings.agentic_cache_message_tail and used_breakpoints < 4:
        send_messages = _messages_with_cache_tail(messages)
    corr_id = f"clt-{_uuid.uuid4().hex[:12]}"
    started = _time.perf_counter()

    create_kwargs: dict = dict(
        model=chosen_model, max_tokens=max_tokens,
        system=claude_system, messages=send_messages, tools=send_tools,
    )
    if tool_choice is not None:
        create_kwargs["tool_choice"] = tool_choice
    # Interleaved extended thinking (§reasoning): budget must be ≥1024 and strictly
    # below max_tokens (which must leave room for the answer); temperature stays
    # unset (=1, required with thinking). tool_choice must remain "auto" (it is here).
    if thinking_budget and thinking_budget > 0 and _claude_supports_thinking():
        budget = max(1024, int(thinking_budget))
        if create_kwargs["max_tokens"] <= budget:
            create_kwargs["max_tokens"] = budget + 4096
        create_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

    response = await _claude_create_with_retry(
        lambda: client.messages.create(**create_kwargs),
        model=chosen_model, agent_name=agent_name, corr_id=corr_id,
    )

    text, thinking, tool_uses, blocks = _assistant_blocks_to_dicts(getattr(response, "content", []))
    stop_reason = getattr(response, "stop_reason", None)
    # AiNxt F15/caveat-6 guard: an upstream failure surfaced as assistant text. Raising here
    # (instead of returning it as a turn) lets the loop's retry/transient handling act — the
    # alternative is a reviewer/agent reasoning over an error string as if it were content.
    if not tool_uses and _is_gateway_error_text(text):
        raise RuntimeError(f"gateway returned an upstream error as assistant content "
                           f"(docs/ainxt_messages_compat.md F15): {text[:300]}")
    # AiNxt caveat-1 mitigation: synthesize the truncation signal the gateway strips.
    _u0 = getattr(response, "usage", None)
    _out0 = getattr(_u0, "output_tokens", None) if _u0 else None
    if _truncated_by_budget(stop_reason, _out0, create_kwargs["max_tokens"]):
        logger.warning("call_claude_tools: output consumed the FULL budget (%s/%s) with stop_reason=%r "
                       "— synthesizing 'max_tokens' (gateway collapses truncation to end_turn)",
                       _out0, create_kwargs["max_tokens"], stop_reason)
        stop_reason = "max_tokens"
    if stop_reason == "max_tokens":
        # A tool_use cut off here may carry partial input — the caller's tool
        # executor validates inputs and returns is_error so the model recovers,
        # but raise max_tokens (or continue the turn) to avoid wasted rounds.
        logger.warning("call_claude_tools truncated by max_tokens=%d model=%s tool_uses=%d",
                       max_tokens, chosen_model, len(tool_uses))

    u = getattr(response, "usage", None)
    usage = {
        "input_tokens":       getattr(u, "input_tokens", None) if u else None,
        "output_tokens":      getattr(u, "output_tokens", None) if u else None,
        "cache_read_tokens":  getattr(u, "cache_read_input_tokens", None) if u else None,
        "cache_write_tokens": getattr(u, "cache_creation_input_tokens", None) if u else None,
        "stop_reason":        stop_reason,
    }
    try:
        current_llm_usage.set(dict(usage))
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.core.observability import record_llm_call_kwargs
        # Agentic-loop messages carry block-list content (tool_result/tool_use),
        # not just strings — count both so the char estimate isn't a gross
        # undercount (rough proxy; exact tokens come from the usage fields).
        prompt_chars = len(_coerce_system_to_str(system)) + sum(
            len(c) if isinstance(c := m.get("content"), str) else len(str(c or ""))
            for m in messages
        )
        record_llm_call_kwargs(
            agent_name=agent_name or "agentic", provider=effective_provider, model=chosen_model,
            streaming=False, prompt_chars=prompt_chars, response_chars=len(text),
            response_chunks=0, elapsed_ms=int((_time.perf_counter() - started) * 1000),
            success=True, input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_tokens"], cache_write_tokens=usage["cache_write_tokens"],
            stop_reason=stop_reason, extra={"tool_uses": [t.name for t in tool_uses]},
        )
    except Exception:  # noqa: BLE001 — telemetry never breaks the call
        pass

    try:
        from app._devlog import capture as _dc
        _dc.record_llm_io(
            agent_name=agent_name or "agentic", provider=effective_provider, model=chosen_model,
            system=system, messages=messages, response=text,
            streaming=False, elapsed_ms=int((_time.perf_counter() - started) * 1000),
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_tokens"),
            cache_write_tokens=usage.get("cache_write_tokens"),
            stop_reason=stop_reason, success=True, tool_uses=[t.name for t in tool_uses],
        )
    except Exception:
        pass

    return ClaudeToolTurn(text=text, tool_uses=tool_uses, stop_reason=stop_reason,
                          assistant_content=blocks, usage=usage, thinking=thinking)


async def call_llm_vision(system: str, prompt: str, image_bytes: bytes,
                          media_type: str = "image/jpeg", *,
                          model: str | None = None, max_tokens: int = 1200,
                          agent_name: str | None = None) -> str:
    """One vision turn: an image + a text prompt → the model's text description.

    Anthropic-protocol only (Claude direct, or AiNxt anthropic-compat — NOTE: whether
    AiNxt forwards image blocks is UNVERIFIED, see docs/ainxt_messages_compat.md
    'UNKNOWN'; callers must be fail-open). Used by image_understanding to turn
    figures embedded in uploaded documents into text the pipeline can consume."""
    import base64 as _b64

    effective_provider = normalize_provider(settings.llm_provider)
    if effective_provider == "ainxt" and _ainxt_uses_anthropic():
        chosen_model = model or get_model(provider="ainxt")
        client = _get_ainxt_anthropic_client()
    elif effective_provider == "claude":
        chosen_model = model or get_model(provider="claude")
        client = _get_anthropic_client()
    else:
        raise ValueError(f"call_llm_vision requires provider='claude' or 'ainxt' "
                         f"(anthropic compat mode), got '{effective_provider}'")

    response = await _claude_create_with_retry(
        lambda: client.messages.create(
            model=chosen_model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": _b64.b64encode(image_bytes).decode()}},
                {"type": "text", "text": prompt},
            ]}],
        ),
        model=chosen_model, agent_name=agent_name or "vision", corr_id="vis",
    )
    text = "".join(getattr(b, "text", "") for b in (getattr(response, "content", None) or [])
                   if getattr(b, "type", None) == "text").strip()
    if _is_gateway_error_text(text):
        raise RuntimeError(f"gateway returned an upstream error as vision content: {text[:200]}")
    return text


def tool_result_block(tool_use_id: str, content, is_error: bool = False) -> dict:
    """Build a tool_result content block for the next user turn. `content` may be
    a string or a list of content blocks; `is_error=True` signals a failed tool
    so the model can recover (the read-before-edit / git-guard denials use this)."""
    block: dict = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


async def _stream_claude(system, messages: list[dict], model: str, max_tokens: int,
                         agent_name: str | None = None, client=None,
                         temperature: float | None = None) -> AsyncGenerator[str, None]:
    import asyncio as _asyncio
    client = client or _get_anthropic_client()
    total_chars = 0
    # Phase 6.2 — pass segmented list verbatim when supplied so the SDK
    # honours cache_control on individual blocks.
    claude_system = _system_for_claude(system)
    # Streaming guard (P1): per-chunk idle timeout. A stream that stops yielding without
    # closing (gateway hang, dead upstream) otherwise blocks its caller FOREVER with no
    # diagnostic — fail loudly instead so the caller's retry/fallback logic can act.
    idle = int(getattr(settings, "llm_stream_idle_timeout_s", 300) or 0)
    stream_kwargs: dict = dict(
        model=model,
        max_tokens=max_tokens,
        system=claude_system,
        messages=messages,
    )
    # None leaves it unset (API default 1.0) — this path never enables extended
    # thinking, which is the only mode that would conflict with a custom value.
    if temperature is not None:
        stream_kwargs["temperature"] = temperature
    async with client.messages.stream(**stream_kwargs) as stream:
        agen = stream.text_stream.__aiter__()
        while True:
            # First-chunk grace (2×): time-to-first-TEXT includes prompt prefill and any
            # server-side reasoning phase — gating it at the per-chunk idle produced false
            # "stalled" on long-thinking calls that were progressing normally.
            _timeout = idle * (2 if total_chars == 0 else 1)
            try:
                text = (await _asyncio.wait_for(agen.__anext__(), timeout=_timeout)
                        if idle > 0 else await agen.__anext__())
            except StopAsyncIteration:
                break
            except _asyncio.TimeoutError:
                raise RuntimeError(
                    f"LLM stream stalled — no chunk for {_timeout}s (provider=claude/ainxt "
                    f"model={model} agent={agent_name or '?'} chars_so_far={total_chars})")
            total_chars += len(text)
            yield text
        # Anthropic SDK exposes stop_reason on the final assembled message.
        try:
            final = await stream.get_final_message()
            _warn_if_truncated(getattr(final, "stop_reason", None), model, max_tokens,
                               total_chars, agent_name)
            # Slice 24+ — capture usage + stop_reason from the final
            # assembled message.
            usage = getattr(final, "usage", None)
            current_llm_usage.set({
                "input_tokens":       getattr(usage, "input_tokens",  None) if usage else None,
                "output_tokens":      getattr(usage, "output_tokens", None) if usage else None,
                "cache_read_tokens":  getattr(usage, "cache_read_input_tokens",     None) if usage else None,
                "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", None) if usage else None,
                "stop_reason":        getattr(final, "stop_reason", None),
            })
        except Exception as e:
            # Fail-open: never let a stop_reason fetch error break the stream consumer.
            logger.debug("Claude stream final-message fetch failed (truncation check skipped): %s", e)
    # Terminal guard (P1): a stream that closed cleanly but yielded ZERO text is a failure
    # (reasoning-only budget exhaustion, gateway empty body), not a valid empty answer —
    # mirrors the non-streaming path's no-text guard so it can't masquerade as content.
    if total_chars == 0:
        raise RuntimeError(
            f"LLM stream ended with no text (provider=claude/ainxt model={model} "
            f"agent={agent_name or '?'} max_tokens={max_tokens}). If this recurs, raise the "
            f"call's max_tokens — a reasoning model can spend the whole budget before any text."
        )


# ── OpenAI-compatible implementation (used by both OpenAI and AiNxt) ────────

# Model families that require `max_completion_tokens` instead of the legacy
# `max_tokens`. OpenAI introduced the renamed parameter for reasoning-capable /
# frontier models (o1, o3, o4-mini, gpt-5 family). Older chat models (gpt-4o,
# gpt-4-turbo, gpt-3.5-turbo) still accept the legacy `max_tokens`. AiNxt
# generally proxies the legacy param. We auto-detect by model-id prefix.
_MAX_COMPLETION_TOKENS_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def _openai_token_kwarg(model: str, max_tokens: int) -> dict:
    """Return the right token-cap kwarg for the given OpenAI model id.

    Newer models (gpt-5.x, o1, o3, o4-mini) reject `max_tokens` with a 400
    `unsupported_parameter` error and require `max_completion_tokens`.
    """
    m = (model or "").lower().lstrip()
    if any(m.startswith(p) for p in _MAX_COMPLETION_TOKENS_PREFIXES):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}

async def _call_openai_compat(client, system: str, messages: list[dict], model: str, max_tokens: int,
                              agent_name: str | None = None) -> str:
    """Non-streaming OpenAI-compatible call. Includes one retry on AiNxt
    placeholder error responses (the gateway has been observed to return
    the literal string `"Error generating response"` with HTTP 200 OK when
    its upstream times out)."""
    import time as _time
    started = _time.perf_counter()
    text = await _do_call_openai_compat(client, system, messages, model, max_tokens, agent_name)
    if _is_placeholder_error(text):
        # Gateway-side failure. Capture everything ops needs to file a
        # ticket with the AiNxt team — base URL, model, agent, prompt
        # size, elapsed wall-clock, response, and a unique correlation
        # id that's also surfaced on retry so they can correlate logs.
        elapsed_ms_first = int((_time.perf_counter() - started) * 1000)
        prompt_chars = sum(len(m.get("content") or "") for m in messages) + len(system)
        gateway_url = str(getattr(client, "base_url", "?"))
        import uuid as _uuid
        corr_id = f"ainxt-{_uuid.uuid4().hex[:12]}"
        logger.warning(
            "AINXT_PLACEHOLDER_ERROR | corr_id=%s gateway=%s model=%s "
            "agent=%s prompt_chars=%d max_tokens=%d elapsed_ms=%d "
            "response=%r retry_after_ms=1500. Forward this line to the "
            "AiNxt team if it persists.",
            corr_id, gateway_url, model, agent_name or "?",
            prompt_chars, max_tokens, elapsed_ms_first, text,
        )
        import asyncio as _aio
        await _aio.sleep(1.5)
        retry_started = _time.perf_counter()
        text = await _do_call_openai_compat(client, system, messages, model, max_tokens, agent_name)
        if _is_placeholder_error(text):
            elapsed_ms_retry = int((_time.perf_counter() - retry_started) * 1000)
            logger.error(
                "AINXT_PLACEHOLDER_ERROR_PERSISTENT | corr_id=%s gateway=%s "
                "model=%s agent=%s prompt_chars=%d max_tokens=%d "
                "elapsed_ms_first=%d elapsed_ms_retry=%d response=%r. "
                "Both attempts returned the placeholder. Raising — caller "
                "will retry per its own policy. Forward both warning "
                "lines (same corr_id) to the AiNxt team.",
                corr_id, gateway_url, model, agent_name or "?",
                prompt_chars, max_tokens,
                elapsed_ms_first, elapsed_ms_retry, text,
            )
            raise RuntimeError(
                f"AiNxt placeholder error (corr_id={corr_id}, after 1 retry): {text!r}"
            )
    return text


def _retry_after_seconds(exc) -> float | None:
    """Extract a server-suggested wait from a 429 response.

    Per RFC 7231 §7.1.3, ``Retry-After`` may be either an integer
    (delta-seconds) OR an HTTP-date. We try seconds first (most providers
    use that), fall back to HTTP-date parsing (some CDNs / gateways use
    it). Returns None when not parseable so the caller falls back to its
    own exponential backoff.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        headers = getattr(resp, "headers", None) or {}
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra is None:
            return None
        ra = str(ra).strip()
        # Form 1: integer delta-seconds (e.g. "30").
        try:
            return max(0.0, float(ra))
        except ValueError:
            pass
        # Form 2: HTTP-date (e.g. "Wed, 21 Oct 2015 07:28:00 GMT"). Compute
        # the delta from now in UTC. negative deltas (past dates) clamp to 0
        # so the caller doesn't get a negative sleep.
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            target = parsedate_to_datetime(ra)
            if target is None:
                return None
            if target.tzinfo is None:  # RFC 7231 says GMT; be defensive
                target = target.replace(tzinfo=timezone.utc)
            delta = (target - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delta)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None


async def _do_call_openai_compat(client, system: str, messages: list[dict], model: str, max_tokens: int,
                                 agent_name: str | None = None) -> str:
    """Inner non-streaming helper — single attempt with intelligent 429
    retry. Used by `_call_openai_compat` which adds AiNxt-placeholder
    retry on top of this.

    429 strategy (intelligent — not a dumb sleep loop):
      - Wait `min(Retry-After, 2^attempt + jitter)` between attempts.
      - Cap total attempts at `engine_rate_limit_max_retries` (default 5).
      - Log each retry with the full request fingerprint so ops can
        forward a single line to the gateway team if rate limits persist.
      - Don't infinitely loop — failed retries propagate up so the
        engine's outer per-batch resilience can place a placeholder
        rendering instead of killing the whole workflow.
    """
    import time as _time
    import uuid as _uuid
    import random as _random
    import asyncio as _asyncio

    oai_messages = [{"role": "system", "content": system}] + messages

    # `settings` is already imported at module top (line 14). See the
    # matching block in _call_claude for the rationale on dropping the
    # inline import + try/except wrapper.
    max_retries = int(getattr(settings, "engine_rate_limit_max_retries", 5))

    # Lazy-import openai's exception class so we don't pay the cost when
    # the openai SDK isn't loaded (engines using the Anthropic SDK).
    try:
        import openai as _openai  # noqa: WPS433
        rate_limit_exc = (_openai.RateLimitError,)
    except Exception:  # noqa: BLE001
        rate_limit_exc = ()  # type: ignore[assignment]

    corr_id = f"gw-{_uuid.uuid4().hex[:12]}"
    gateway_url = str(getattr(client, "base_url", "?"))
    started = _time.perf_counter()

    # Route to correct token-cap kwarg per model family (gpt-5.x / o1 / o3 / o4
    # reject `max_tokens` and require `max_completion_tokens`).
    _token_kw = _openai_token_kwarg(model, max_tokens)
    for attempt in range(max_retries + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=oai_messages,
                **_token_kw,
            )
            break  # success — exit the retry loop
        except rate_limit_exc as e:  # type: ignore[misc]
            # 429 — back off and retry. We don't bail until we've burnt
            # the whole budget; the writer's outer resilience will
            # synthesise a placeholder if we eventually fail.
            if attempt >= max_retries:
                elapsed_ms = int((_time.perf_counter() - started) * 1000)
                prompt_chars = sum(len(m.get("content") or "") for m in messages) + len(system)
                logger.error(
                    "RATE_LIMIT_EXHAUSTED | corr_id=%s gateway=%s model=%s "
                    "agent=%s prompt_chars=%d max_tokens=%d elapsed_ms=%d "
                    "attempts=%d exc=%s. Bailing — caller will substitute "
                    "a placeholder rendering. Lower ENGINE_WRITER_MAX_"
                    "CONCURRENT_BATCHES if this is frequent.",
                    corr_id, gateway_url, model, agent_name or "?",
                    prompt_chars, max_tokens, elapsed_ms, attempt + 1, repr(e)[:200],
                )
                raise
            # Honour server's Retry-After if present; otherwise full jitter
            # backoff: uniform(0, 2^attempt) — spreads retries across the
            # whole window rather than clustering at the upper end. See
            # the matching block in _call_claude for the rationale.
            server_wait = _retry_after_seconds(e)
            backoff = _random.uniform(0, 2 ** attempt)
            wait_s = min(server_wait or backoff, 60.0)  # never sleep more than a minute
            logger.warning(
                "RATE_LIMIT_RETRY | corr_id=%s gateway=%s model=%s agent=%s "
                "attempt=%d/%d wait=%.1fs server_retry_after=%s exc=%s",
                corr_id, gateway_url, model, agent_name or "?",
                attempt + 1, max_retries + 1, wait_s,
                server_wait, repr(e)[:120],
            )
            await _asyncio.sleep(wait_s)
            continue
        except Exception as e:
            # Non-429 hard failure — same one-line ticket-ready log we had
            # before, then propagate.
            elapsed_ms = int((_time.perf_counter() - started) * 1000)
            prompt_chars = sum(len(m.get("content") or "") for m in messages) + len(system)
            status = getattr(getattr(e, "response", None), "status_code", "?")
            body_excerpt = ""
            try:
                body_excerpt = str(getattr(getattr(e, "response", None), "text", "") or "")[:500]
            except Exception:  # noqa: BLE001
                pass
            logger.error(
                "GATEWAY_HARD_ERROR | corr_id=%s gateway=%s model=%s agent=%s "
                "prompt_chars=%d max_tokens=%d elapsed_ms=%d http_status=%s "
                "exc=%s body=%r. Forward this line to the AiNxt team if the "
                "gateway is AiNxt and the error is gateway-side.",
                corr_id, gateway_url, model, agent_name or "?",
                prompt_chars, max_tokens, elapsed_ms, status, repr(e)[:200], body_excerpt,
            )
            raise
    # AiNxt sometimes returns SSE text/event-stream even for non-streaming
    # requests, causing the OpenAI SDK to hand back a raw string instead of
    # a completion object.  When that happens, try to extract the assistant
    # content from the SSE lines ourselves, then fall back to returning the
    # raw string (which is better than crashing with AttributeError).
    if isinstance(response, str):
        import json
        # SSE lines look like: data: {"choices":[{"delta":{"content":"..."}}]}
        # or Anthropic-style:  data: {"type":"content_block_delta","delta":{"text":"..."}}
        parts: list[str] = []
        truncated_reason: str | None = None
        for line in response.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("[DONE]", ""):
                continue
            try:
                obj = json.loads(payload)
                # OpenAI streaming format
                for ch in (obj.get("choices") or []):
                    txt = (ch.get("delta") or {}).get("content") or \
                          (ch.get("message") or {}).get("content") or ""
                    if txt:
                        parts.append(txt)
                    # OpenAI truncation marker.
                    if ch.get("finish_reason") == "length":
                        truncated_reason = "length"
                # Anthropic streaming format
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text", ""))
                # Anthropic message_delta event carries final stop_reason.
                if obj.get("type") == "message_delta":
                    sr = (obj.get("delta") or {}).get("stop_reason")
                    if sr == "max_tokens":
                        truncated_reason = "max_tokens"
            except (json.JSONDecodeError, KeyError):
                pass
        text = "".join(parts).strip() if parts else response.strip()
        if truncated_reason:
            logger.warning(
                "LLM TRUNCATED via SSE shim — reason=%s model=%s agent=%s "
                "max_tokens=%d output_chars=%d",
                truncated_reason, model, agent_name or "?", max_tokens, len(text),
            )
        # Slice 24+ — stash stop_reason from the SSE-shim path. No usage
        # info available here (the gateway returned text-as-string with
        # no usage trailer), so input/output tokens stay None.
        try:
            current_llm_usage.set({
                "input_tokens":       None,
                "output_tokens":      None,
                "cache_read_tokens":  None,
                "cache_write_tokens": None,
                "stop_reason":        truncated_reason,
            })
        except Exception:  # noqa: BLE001
            pass
        return text
    if not response.choices:
        raise RuntimeError(f"LLM returned empty choices: {response}")
    choice = response.choices[0]
    text = choice.message.content.strip()
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        logger.warning(
            "LLM TRUNCATED at max_tokens=%d — model=%s agent=%s output_chars=%d "
            "(finish_reason=length). Consider raising max_tokens at this call site.",
            max_tokens, model, agent_name or "?", len(text),
        )
        # Same typed-at-the-text-level signal as the Claude path: partial output must
        # never travel downstream looking complete. (AiNxt strips finish_reason entirely —
        # there this branch cannot fire; generous per-agent caps remain that mitigation.)
        text += TRUNCATION_MARKER
    # Slice 24+ — capture provider-reported usage. OpenAI-compat returns
    # `prompt_tokens` / `completion_tokens` (vs Anthropic's input/output).
    # Cache tokens are Anthropic-only; left None here.
    try:
        usage = getattr(response, "usage", None)
        current_llm_usage.set({
            "input_tokens":  getattr(usage, "prompt_tokens",     None) if usage else None,
            "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
            "cache_read_tokens":  None,
            "cache_write_tokens": None,
            # OpenAI's equivalent of stop_reason is `finish_reason` on
            # the choice. Native vocab: stop / length / content_filter /
            # tool_calls. `length` is the truncation case (= Anthropic
            # `max_tokens`); we keep the provider-native value for
            # operator clarity.
            "stop_reason":   finish_reason,
        })
    except Exception:  # noqa: BLE001
        pass
    return text


# Threshold above which we're confident a stream is real content (not a
# placeholder). Buffered chunks are released only after the accumulated text
# crosses this limit. Set tight to "Error generating response" length (25)
# plus a small margin so most streams (which produce meaningful output in the
# first 100 chars) feel essentially identical to before this patch.
_STREAM_BUFFER_HOLD_CHARS = 64


async def _stream_openai_compat(client, system: str, messages: list[dict], model: str, max_tokens: int,
                                agent_name: str | None = None) -> AsyncGenerator[str, None]:
    """Streaming OpenAI-compatible call with end-of-stream placeholder retry.

    Behaviour:
      - Chunks are buffered in-memory until accumulated output exceeds
        `_STREAM_BUFFER_HOLD_CHARS` (64). Once the threshold is crossed, the
        buffer is flushed and subsequent chunks are yielded live. This is
        invisible to the consumer for normal Claude streams (which produce
        >64 chars in the first second).
      - If the entire stream produces ≤ 64 chars AND the buffered text matches
        a known AiNxt placeholder ("Error generating response"), the buffer
        is DROPPED, a non-streaming retry is issued, and the recovered text
        is yielded as a single chunk. Same retry semantic as the non-streaming
        path's PLACEHOLDER-ERROR handling.
      - If the buffer is short (≤ 64 chars) but does NOT match a placeholder,
        the buffer is yielded as-is — we don't intercept legitimate short
        responses.
      - All previous truncation-detection logic preserved.
    """
    oai_messages = [{"role": "system", "content": system}] + messages
    # Slice 24+ — `stream_options.include_usage=True` makes OpenAI emit a
    # FINAL chunk after the content stream that carries `chunk.usage` with
    # `prompt_tokens` / `completion_tokens`. AiNxt is OpenAI-compatible
    # and forwards the kwarg; if a downstream gateway rejects it, the
    # except branch below falls back to a vanilla stream (no usage —
    # graceful degradation, just a null cost on those rows).
    # Route to correct token-cap kwarg per model family (gpt-5.x / o1 / o3 / o4
    # reject `max_tokens` and require `max_completion_tokens`).
    _token_kw = _openai_token_kwarg(model, max_tokens)
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=oai_messages,
            stream=True,
            stream_options={"include_usage": True},
            **_token_kw,
        )
    except TypeError:
        # Older OpenAI SDKs reject `stream_options` as an unknown kwarg.
        # Drop it and continue.
        stream = await client.chat.completions.create(
            model=model,
            messages=oai_messages,
            stream=True,
            **_token_kw,
        )

    buffer: list[str] = []
    buffer_released = False
    seen_any_chunk = False
    total_chars = 0
    truncated_reason: str | None = None
    # Slice 24+ — capture the provider-native stop reason (whichever
    # arrives last across the OpenAI / AiNxt-SSE / Anthropic-SSE
    # variants this stream may yield). Distinct from
    # `truncated_reason` which only fires on length / max_tokens —
    # this carries the normal "stop"/"end_turn" cases too.
    captured_stop_reason: str | None = None

    async def _emit(txt: str):
        """Emit text downstream — buffer until threshold, then live."""
        nonlocal buffer_released, total_chars
        total_chars += len(txt)
        if buffer_released:
            return txt
        buffer.append(txt)
        if total_chars > _STREAM_BUFFER_HOLD_CHARS:
            buffer_released = True
            return "".join(buffer)
        return None

    # Slice 24+ — usage from the final OpenAI / AiNxt chunk. OpenAI
    # emits one trailing chunk where .choices is empty and .usage is
    # populated (when stream_options.include_usage=True was honoured).
    # AiNxt's SSE-passthrough path may also expose `usage` on a
    # `message_stop` / final delta object.
    captured_input_tokens:  int | None = None
    captured_output_tokens: int | None = None

    # Streaming guard (parity with _stream_claude — this IS the prod path when
    # ainxt_compat_mode=openai): per-chunk idle timeout so a gateway that stops
    # yielding without closing fails loudly instead of blocking its caller forever.
    # First chunk gets 2× grace — TTFT includes prefill/upstream reasoning time.
    import asyncio as _asyncio
    _idle = int(getattr(settings, "llm_stream_idle_timeout_s", 300) or 0)
    _chunk_agen = stream.__aiter__()
    _seen_first = False
    while True:
        _timeout = _idle * (1 if _seen_first else 2)
        try:
            chunk = (await _asyncio.wait_for(_chunk_agen.__anext__(), timeout=_timeout)
                     if _idle > 0 else await _chunk_agen.__anext__())
        except StopAsyncIteration:
            break
        except _asyncio.TimeoutError:
            raise RuntimeError(
                f"LLM stream stalled — no chunk for {_timeout}s (provider=openai-compat "
                f"model={model} agent={agent_name or '?'} chars_so_far={total_chars})")
        _seen_first = True
        # OpenAI-format usage trailer — .choices is empty, .usage populated.
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            captured_input_tokens  = getattr(chunk_usage, "prompt_tokens",     captured_input_tokens)
            captured_output_tokens = getattr(chunk_usage, "completion_tokens", captured_output_tokens)
        # OpenAI-format chunk
        if hasattr(chunk, "choices") and chunk.choices:
            delta = chunk.choices[0].delta
            if delta and delta.content:
                seen_any_chunk = True
                out = await _emit(delta.content)
                if out:
                    yield out
            fr = getattr(chunk.choices[0], "finish_reason", None)
            if fr:
                captured_stop_reason = fr
                if fr == "length":
                    truncated_reason = "length"
            continue
        # AiNxt sometimes yields Anthropic-format dict/string chunks
        # instead of OpenAI completion objects — parse them directly.
        import json
        raw = chunk if isinstance(chunk, str) else str(chunk)
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("[DONE]", ""):
                continue
            try:
                obj = json.loads(payload)
                # OpenAI delta inside SSE
                for ch in (obj.get("choices") or []):
                    txt = (ch.get("delta") or {}).get("content") or ""
                    if txt:
                        seen_any_chunk = True
                        out = await _emit(txt)
                        if out:
                            yield out
                    if ch.get("finish_reason") == "length":
                        truncated_reason = "length"
                # Anthropic content_block_delta inside SSE
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    txt = delta.get("text", "")
                    if txt:
                        seen_any_chunk = True
                        out = await _emit(txt)
                        if out:
                            yield out
                # Anthropic final stop_reason event.
                if obj.get("type") == "message_delta":
                    sr = (obj.get("delta") or {}).get("stop_reason")
                    if sr:
                        captured_stop_reason = sr
                        if sr == "max_tokens":
                            truncated_reason = "max_tokens"
                # Slice 24+ — usage in SSE-form (covers both OpenAI's
                # stream-options trailer and Anthropic's message_delta
                # which carries `usage.output_tokens`). Keep whichever
                # arrives last; both paths populate the same target.
                u = obj.get("usage") or (obj.get("message_delta") or {}).get("usage")
                if isinstance(u, dict):
                    captured_input_tokens  = u.get("prompt_tokens")  or u.get("input_tokens")  or captured_input_tokens
                    captured_output_tokens = u.get("completion_tokens") or u.get("output_tokens") or captured_output_tokens
            except (json.JSONDecodeError, KeyError):
                pass

    # Slice 24+ — stash captured usage + stop_reason on the contextvar
    # so the outer stream_llm finally block picks it up alongside Claude
    # usage. Set unconditionally when stop_reason was seen, even with
    # no usage trailer (some gateway variants omit usage but still
    # report finish_reason on the last content chunk).
    if (
        captured_input_tokens is not None
        or captured_output_tokens is not None
        or captured_stop_reason is not None
    ):
        try:
            current_llm_usage.set({
                "input_tokens":       captured_input_tokens,
                "output_tokens":      captured_output_tokens,
                "cache_read_tokens":  None,
                "cache_write_tokens": None,
                "stop_reason":        captured_stop_reason,
            })
        except Exception:  # noqa: BLE001
            pass

    # End of stream. Three cases:
    #   1. buffer_released=True → already yielded live; just emit any final
    #      truncation/placeholder warning.
    #   2. buffer not released, has content → either short legitimate response
    #      OR placeholder. Inspect buffer.
    #   3. buffer empty AND seen_any_chunk=False → upstream produced nothing;
    #      fall back to non-streaming call (existing behaviour).
    buffered_text = "".join(buffer)

    if not buffer_released and buffered_text and _is_placeholder_error(buffered_text):
        # Case 2a — placeholder. Drop the buffer, retry non-streaming.
        logger.warning(
            "LLM PLACEHOLDER-ERROR in stream — model=%s agent=%s response=%r. "
            "Stream produced only the placeholder string; retrying non-streaming once before yielding.",
            model, agent_name or "?", buffered_text,
        )
        try:
            recovered = await _call_openai_compat(
                client, system, messages, model, max_tokens, agent_name=agent_name,
            )
            if recovered and not _is_placeholder_error(recovered):
                logger.info(
                    "LLM PLACEHOLDER-ERROR stream-retry recovered — model=%s agent=%s "
                    "output_chars=%d",
                    model, agent_name or "?", len(recovered),
                )
                yield recovered
                # Skip the rest of the diagnostic warnings — recovery succeeded.
                return
            else:
                logger.warning(
                    "LLM PLACEHOLDER-ERROR stream-retry also failed — model=%s agent=%s. "
                    "Yielding empty result; caller's validator/fallback layer will handle it.",
                    model, agent_name or "?",
                )
                # Don't yield the placeholder — drop entirely, agent will see empty stream
                # which matches the not-yielded fallback branch below.
        except Exception as e:
            logger.warning(
                "LLM PLACEHOLDER-ERROR stream-retry raised — model=%s agent=%s error=%s. "
                "Yielding the original placeholder as last-resort.",
                model, agent_name or "?", e,
            )
            # On retry exception, yield the original buffer so consumer at least
            # sees the placeholder text rather than nothing (preserves prior behaviour).
            yield buffered_text
    elif not buffer_released and buffered_text:
        # Case 2b — short legitimate response. Yield as-is.
        yield buffered_text

    if truncated_reason:
        logger.warning(
            "LLM TRUNCATED in stream — reason=%s model=%s agent=%s "
            "max_tokens=%d output_chars=%d. Consider raising max_tokens at this call site.",
            truncated_reason, model, agent_name or "?", max_tokens, total_chars,
        )

    # Last-resort: if stream yielded nothing AND nothing was buffered, fall
    # back to a non-streaming call so the agent gets SOMETHING rather than
    # an empty response.
    if not seen_any_chunk:
        try:
            result = await _call_openai_compat(client, system, messages, model, max_tokens, agent_name=agent_name)
            if result and not _is_placeholder_error(result):
                yield result
        except Exception:
            pass

    # Diagnostic warning for short non-placeholder responses (preserves
    # the existing TRUNCATED grep so ops continue to see something).
    if seen_any_chunk and 0 < total_chars <= 32 and not _is_placeholder_error(buffered_text):
        logger.warning(
            "LLM TRUNCATED via stream-short-output heuristic — model=%s agent=%s "
            "output_chars=%d. Short legitimate response; not retried.",
            model, agent_name or "?", total_chars,
        )
