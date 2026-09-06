# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: LLM boundary types.
#
# WHY this file owns the canonical definitions (not `llm/base.py` like the
# standalone build): we deliberately did NOT copy the engine's old multi-
# provider client layer because the host project owns LLM access. But the
# engine's agents still pass typed Messages and SystemBlocks around, so
# those types must live somewhere. The schemas package is the right home —
# it's the boundary-types module — and the LLM adapter imports from here.

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SystemBlock(BaseModel):
    """One block of system-prompt content. ``cache=True`` is a hint to the
    underlying provider's prompt-cache when supported."""

    text: str
    cache: bool = False


class Message(BaseModel):
    """Chat-style message used by every engine agent."""

    role: Literal["user", "assistant"]
    content: str


class LLMUsage(BaseModel):
    """Normalized token accounting. The host's observability layer is the
    authoritative cost source — these counts are informational only."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class LLMResponse(BaseModel):
    """Normalized provider response surfaced by the LLM adapter."""

    text: str
    usage: LLMUsage
    model: str
    provider: str
    raw: dict | None = None


__all__ = ["SystemBlock", "Message", "LLMUsage", "LLMResponse"]
