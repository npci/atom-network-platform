# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic mock video provider for local dev / tests.

Writes a real (tiny) MP4 of the requested duration using ffmpeg's lavfi color
source — no external API, no keys. Selected when ``video_provider == "mock"``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.services.video.base import ClipResult, VideoProvider, VideoProviderError

logger = logging.getLogger(__name__)

_COLORS = ["red", "green", "blue", "orange", "purple", "teal", "navy", "maroon"]


class MockVideoProvider(VideoProvider):
    name = "mock"

    def __init__(self, model: str | None = None):
        self.model = model or "mock-video"

    async def generate_clip(
        self, *, prompt: str, duration_sec: int, out_path: Path,
        aspect_ratio: str = "16:9", chat_id: str | None = None,
    ) -> ClipResult:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        color = _COLORS[hash(prompt) % len(_COLORS)]
        size = "1280x720" if aspect_ratio == "16:9" else "720x1280"
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c={color}:s={size}:d={int(duration_sec)}",
            "-pix_fmt", "yuv420p", str(out_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not out_path.exists():
            raise VideoProviderError(f"mock ffmpeg failed: {stderr.decode()[:300]}")
        return ClipResult(path=out_path, duration_sec=int(duration_sec), model=self.model)
