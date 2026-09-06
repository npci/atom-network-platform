# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Generate an artifact for a golden case, by calling the real agent.

Capture goes through the AGENT FUNCTION, not the HTTP/WebSocket layer. The
golden is meant to defend generation quality; routing it through auth, sockets
and the job registry would make the suite fail for reasons that have nothing to
do with the model output, and those layers have their own tests.

Requires a configured LLM provider. Everything else in this package is offline —
this module is the one place that costs money and needs credentials.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable


async def _drain(agen) -> str:
    """Collect an async generator of text chunks into one document."""
    parts: list[str] = []
    async for chunk in agen:
        if chunk:
            parts.append(chunk)
    return "".join(parts)


async def _capture_canvas(inputs: dict[str, Any]) -> str:
    from app.agents.canvas import stream_canvas_turn

    return await _drain(stream_canvas_turn(
        enriched_prompt=inputs.get("enriched_prompt", ""),
        research_report=inputs.get("research_report", ""),
        conversation_history=[],
        new_user_message=inputs.get("instruction", "Generate the product canvas."),
    ))


async def _capture_xsd(inputs: dict[str, Any]) -> str:
    from app.agents.xsd import stream_xsd_assessment

    # Keyword names must match the agent exactly: `tech_spec_content` /
    # `brd_content`, not `tech_spec` / `brd`. The original wiring used the short
    # names and had never been executed — can_capture() only checks that an
    # artifact is registered and a provider exists, so the harness reported xsd
    # as capturable right up until the first real capture attempt TypeError'd.
    return await _drain(stream_xsd_assessment(
        tech_spec_content=inputs.get("tech_spec", ""),
        brd_content=inputs.get("brd", ""),
    ))


# artifact key → coroutine(inputs) -> text
GENERATORS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "canvas": _capture_canvas,
    "xsd": _capture_xsd,
}


class CaptureUnavailable(RuntimeError):
    """No provider configured, or no generator wired for this artifact."""


def can_capture(artifact: str) -> tuple[bool, str]:
    """(possible, why-not). Checked before spending anything."""
    if artifact not in GENERATORS:
        return False, (f"no generator wired for artifact {artifact!r}; "
                       f"available: {', '.join(sorted(GENERATORS)) or 'none'}")

    from app.core.config import settings

    provider = (getattr(settings, "llm_provider", "") or "").strip().lower()
    if not provider:
        return False, "LLM_PROVIDER is not set"

    # Catch the placeholder that ships in .env.example, so the failure is a clear
    # message here rather than a 401 several minutes into a capture run.
    if provider == "claude" and "replace-me" in (getattr(settings, "anthropic_api_key", "") or ""):
        return False, "ANTHROPIC_API_KEY is still the .env.example placeholder"
    if provider == "ainxt" and not (getattr(settings, "ainxt_base_url", "") or ""):
        return False, "LLM_PROVIDER=ainxt but AINXT_BASE_URL is unset"
    return True, ""


def capture(artifact: str, inputs: dict[str, Any]) -> str:
    ok, why = can_capture(artifact)
    if not ok:
        raise CaptureUnavailable(why)
    return asyncio.run(GENERATORS[artifact](inputs))
