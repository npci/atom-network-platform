# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Video provider abstraction — one clip (≤8s) per call.

Each provider implements :meth:`VideoProvider.generate_clip`, which takes a
visual prompt + duration and writes a single MP4 to ``out_path``. Providers do
their own submit → poll → download internally. The runner
(:mod:`app.services.video_gen_runner`) calls this once per segment and then
merges the clips with ffmpeg.

Mirrors the provider-dispatch shape of :mod:`app.core.llm` (per-provider helper
+ shared httpx timeout + full-jitter retry), kept separate because the wire
shapes differ from chat completions.
"""

from __future__ import annotations

import abc
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Long read timeout: a single 8s clip blocks ~60–70s on the gateway.
CLIP_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)


@dataclass
class ClipResult:
    """Outcome of generating one clip."""

    path: Path
    duration_sec: int
    cost_usd: float = 0.0
    model: str = ""
    meta: dict = field(default_factory=dict)


class VideoProviderError(RuntimeError):
    """Raised when a provider cannot produce a clip (after retries)."""


class VideoProvider(abc.ABC):
    """Generates one ≤8s video clip per call."""

    name: str = "base"

    @abc.abstractmethod
    async def generate_clip(
        self, *, prompt: str, duration_sec: int, out_path: Path,
        aspect_ratio: str = "16:9", chat_id: str | None = None,
    ) -> ClipResult:
        """Generate a single clip for ``prompt`` and write it to ``out_path``."""
        raise NotImplementedError


async def backoff_sleep(attempt: int, server_wait: float | None = None) -> None:
    """Full-jitter exponential backoff, capped at 60s (matches llm.py)."""
    import asyncio

    backoff = random.uniform(0, 2 ** attempt)
    await asyncio.sleep(min(server_wait or backoff, 60.0))


async def download_to(client: httpx.AsyncClient, url: str, out_path: Path,
                      headers: dict | None = None) -> int:
    """Stream a URL to ``out_path``; returns bytes written.

    follow_redirects=True: provider download URLs commonly 302 to a CDN/file host
    (e.g. Veo's files/...:download → /download/v1beta/...).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    async with client.stream("GET", url, headers=headers or {}, follow_redirects=True) as resp:
        resp.raise_for_status()
        with out_path.open("wb") as fh:
            async for chunk in resp.aiter_bytes():
                fh.write(chunk)
                written += len(chunk)
    if written == 0:
        raise VideoProviderError(f"empty video body from {url}")
    return written
