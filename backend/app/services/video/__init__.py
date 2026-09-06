# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Video provider package — factory mirroring app.core.llm dispatch.

``get_video_provider(provider, model)`` returns a :class:`VideoProvider` for the
requested provider (defaults from settings). Providers:
  - ``ainxt``  — the Authority gateway (proven default)
  - ``gemini`` — Google Veo direct
  - ``grok``   — xAI grok-imagine-video direct
  - ``mock``   — local ffmpeg color clips (dev/tests, no keys)
"""

from __future__ import annotations

from app.core.config import settings
from app.services.video.base import (
    ClipResult, VideoProvider, VideoProviderError,
)

# Allowed (provider, model) options surfaced to the UI.
VIDEO_OPTIONS = {
    "ainxt": ["veo-3.1-generate-preview", "grok-imagine-video"],
    "gemini": ["veo-3.1-generate-preview"],
    "grok": ["grok-imagine-video"],
    "mock": ["mock-video"],
}


def get_video_provider(provider: str | None = None, model: str | None = None) -> VideoProvider:
    p = (provider or settings.video_provider or "ainxt").strip().lower()
    if p == "ainxt":
        from app.services.video.ainxt import AiNxtVideoProvider
        return AiNxtVideoProvider(model=model)
    if p == "gemini":
        from app.services.video.gemini_veo import GeminiVeoProvider
        return GeminiVeoProvider(model=model)
    if p == "grok":
        from app.services.video.grok import GrokVideoProvider
        return GrokVideoProvider(model=model)
    if p == "mock":
        from app.services.video.mock import MockVideoProvider
        return MockVideoProvider(model=model)
    raise VideoProviderError(f"unknown video provider: {provider!r}")


__all__ = [
    "ClipResult", "VideoProvider", "VideoProviderError",
    "get_video_provider", "VIDEO_OPTIONS",
]
