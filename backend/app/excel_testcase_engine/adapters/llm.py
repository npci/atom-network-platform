# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: LLM adapter.
#
# WHY: the engine's agents (enhancer/planner/writer/validator/repairer) were
# written against a per-role `get_client(role)` factory that returns an
# LLMClient with `.complete(system, messages, ...)`. The host project has a
# single global `stream_llm` / `call_llm` API in `app.core.llm`. We don't
# want to rewrite every agent — we wrap the host call behind a synthetic
# LLMClient so the existing engine code compiles unchanged.
#
# WHY not duplicate the engine's old multi-provider client layer:
#   The host project already chose a provider (Claude / OpenAI / AiNxt) via
#   `settings.llm_provider`. Honouring that single choice across all engine
#   roles keeps cost, observability, and key management centralised in
#   one place — the team only has to manage credentials for one provider.

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from app.excel_testcase_engine.schemas.llm import LLMResponse, LLMUsage, Message, SystemBlock

# Will be populated by `register_excel_testcase_engine(...)`. We keep them
# module-level so the engine's agents can call `get_client(role)` without
# threading the host's stream_llm/call_llm through every function.
_stream_fn: Callable | None = None
_call_fn: Callable | None = None
_role_max_tokens: dict[str, int] = {
    "enhancer":     2000,
    "planner":      8000,
    "writer":       8000,
    "validator":    4000,
    "repairer":     4000,
    "corpus_miner": 8000,
}

# Per-role streaming opt-in — experiment 2026-05-05.
# AiNxt's non-streaming response body is being truncated at ~24KB
# (confirmed via LLM_DIAG: cap_ratio=0.38, output cut mid-string at
# char 24408). Streaming may bypass that gateway-level body cap by
# passing chunks through as SSE rather than buffering. Roles set to
# True go through stream_llm + accumulator instead of call_llm.
#
# 2026-05-05 streaming experiment ran 3 streamed planner attempts —
# all cut at chars 24264 / 24322 / 24322 (within ~70 of the
# non-stream sample). AiNxt's 24KB cap applies to BOTH paths; chunks=1
# revealed the gateway fully buffers upstream, applies cap, then
# forwards a single chunk. Streaming workaround abandoned. Kept the
# dict for future experiments but no role uses it now.
_role_streaming: dict[str, bool] = {}

# Per-role provider override — plumbing kept, activation REVERTED 2026-05-05.
#
# Originally set `{"planner": "claude"}` to route the planner direct to
# Anthropic and bypass AiNxt's ~24KB response-body cap. This works on
# any host with internet egress, but the production Ubuntu deploy has
# DNS blocked for api.anthropic.com — `APIConnectionError: [Errno -3]
# Temporary failure in name resolution`. AiNxt is the only LLM endpoint
# this network reaches.
#
# Re-enable specific roles HERE the day AiNxt ships the gateway fix
# (their nginx catch-all proxy_buffering + non-streaming full_answer
# accumulator + dropped req.max_tokens — see AiNxt ticket). Until then
# all roles route via AiNxt and long-output agents truncate at ~24KB.
#
# Empty dict = identical behaviour to pre-2026-05-05 main; no host call
# carries a `provider=` kwarg, settings.llm_provider takes effect.
_role_provider: dict[str, str] = {}

# Tolerant JSON-fence stripper used when the host LLM emits ```json fences.
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def configure(stream_fn: Callable, call_fn: Callable) -> None:
    """Bind the host project's LLM functions. Called by the injector."""

    global _stream_fn, _call_fn
    _stream_fn = stream_fn
    _call_fn = call_fn

    # Validate _role_provider against what the host can actually reach.
    # Without this, a developer who enables `_role_provider = {"planner":
    # "claude"}` on a network where Anthropic DNS is blocked (the prod
    # box was the case) sees the engine fail at runtime with cryptic
    # APIConnectionError. Surface the misconfig at startup instead.
    if _role_provider:
        import logging as _logging
        _log = _logging.getLogger("excel_engine.adapter")
        _missing = []
        for role, provider_name in _role_provider.items():
            if provider_name == "claude":
                try:
                    from app.core.config import settings  # noqa: WPS433
                    if not (settings.anthropic_api_key or "").strip():
                        _missing.append((role, "anthropic_api_key empty"))
                except Exception as exc:  # noqa: BLE001
                    _missing.append((role, f"settings unreadable: {exc!r}"))
            elif provider_name == "openai":
                try:
                    from app.core.config import settings  # noqa: WPS433
                    if not (settings.openai_api_key or "").strip():
                        _missing.append((role, "openai_api_key empty"))
                except Exception as exc:  # noqa: BLE001
                    _missing.append((role, f"settings unreadable: {exc!r}"))
        if _missing:
            _log.warning(
                "excel_engine._role_provider misconfig: %s — these roles "
                "will fail at runtime. Either populate the missing keys or "
                "remove the override entries from adapters/llm.py:_role_provider.",
                _missing,
            )
        else:
            _log.info(
                "excel_engine._role_provider active: %s",
                {k: v for k, v in _role_provider.items()},
            )


@dataclass
class _HostLLMClient:
    """Synthetic LLMClient: same signature as the standalone engine expected.

    Internally calls `app.core.llm.call_llm(system: str, messages: list[dict],
    max_tokens, agent_name)`. We stitch SystemBlocks into a single string and
    translate Messages into the dict shape the host expects.
    """

    role: str
    provider_name: str = "host"
    supports_cache_control: bool = False
    supports_extended_thinking: bool = False
    supports_json_mode: bool = True

    @property
    def model(self) -> str:
        # The host picks the model itself based on `settings.llm_provider`.
        # We surface "host:<role>" to keep cost-tracking / logging readable.
        return f"host:{self.role}"

    async def complete(
        self,
        system: list[SystemBlock],
        messages: list[Message],
        max_tokens: int,
        temperature: float = 0.0,
        response_format: Literal["text", "json"] = "text",
        extended_thinking: bool = False,
    ) -> LLMResponse:
        if _call_fn is None:
            raise RuntimeError(
                "excel_testcase_engine LLM adapter not configured. "
                "Call register_excel_testcase_engine(...) at startup.",
            )

        # WHY join system blocks: the host accepts a single `system: str`
        # parameter, not a list. We preserve order — cached blocks first if
        # the engine ever flags them — so the host LLM's prefix-cache still
        # benefits when supported.
        ordered = sorted(system, key=lambda b: 0 if b.cache else 1)
        system_text = "\n\n".join(b.text for b in ordered)

        if response_format == "json":
            # WHY append JSON-only instruction: the host doesn't expose a
            # provider-native JSON mode flag through stream_llm/call_llm.
            # We add the same instruction the engine already used for
            # Anthropic newer-model compat (which dropped assistant prefill).
            system_text += (
                "\n\nRespond with valid JSON only. "
                "Do not include any prose or markdown fences outside the JSON document."
            )

        host_messages = [{"role": m.role, "content": m.content} for m in messages]

        # Per-role provider override — long-output roles route direct to
        # Claude to bypass AiNxt's 24KB response-body cap. None lets the
        # host settings.llm_provider take effect (existing behaviour).
        provider_override = _role_provider.get(self.role)

        started = time.perf_counter()
        # Streaming roles accumulate SSE chunks into a string; non-streaming
        # roles do a single buffered call. Same return shape either way.
        if _role_streaming.get(self.role) and _stream_fn is not None:
            parts: list[str] = []
            async for chunk in _stream_fn(
                system=system_text,
                messages=host_messages,
                max_tokens=max_tokens,
                agent_name=f"excel_engine.{self.role}",
                provider=provider_override,
            ):
                if chunk:
                    parts.append(chunk)
            text = "".join(parts)
        else:
            text = await _call_fn(
                system=system_text,
                messages=host_messages,
                max_tokens=max_tokens,
                agent_name=f"excel_engine.{self.role}",
                provider=provider_override,
            )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # WHY tolerant JSON parse on the way OUT: hosts that wrap output in
        # ```json ... ``` fences (some OpenAI variants do) trip the engine's
        # downstream Pydantic validation. We strip fences here so each agent
        # doesn't have to.
        if response_format == "json":
            stripped = (text or "").strip()
            m = _FENCE_RE.match(stripped)
            if m:
                text = m.group(1)

        # WHY zero-token usage: the host's call_llm() returns the string only.
        # Token counts live in the host's observability traces (Slice 28).
        # The engine's cost ledger is informational; we leave it at 0 here
        # and rely on the host's centralised metering.
        return LLMResponse(
            text=text or "",
            usage=LLMUsage(input_tokens=0, output_tokens=0),
            model=self.model,
            provider="host",
            raw={"adapter": "host", "elapsed_ms": elapsed_ms},
        )


# WHY a small registry: tests can override per-role with a fake client by
# calling `set_test_client(role, client)`. The engine's existing
# `tests/conftest.py` autouse fixture relies on this seam.
_OVERRIDES: dict[str, "_HostLLMClient"] = {}


def set_test_client(role: str, client: object | None) -> None:
    if client is None:
        _OVERRIDES.pop(role, None)
    else:
        _OVERRIDES[role] = client  # type: ignore[assignment]


def reset_test_clients() -> None:
    _OVERRIDES.clear()


def get_client(role: str):
    """Return an LLMClient for the given engine role. Used by every agent."""

    if role in _OVERRIDES:
        return _OVERRIDES[role]
    return _HostLLMClient(role=role)


__all__ = ["configure", "get_client", "set_test_client", "reset_test_clients"]
