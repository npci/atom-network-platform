# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""xAI Grok video provider (grok-imagine-video).

Async generation flow at ``grok_base_url`` (Bearer ``grok_api_key``):

  1. POST {base}/video/generations
       body: {model, prompt, duration_seconds, aspect_ratio}
       -> {"id": "...", "status": "pending"|"processing"}
  2. GET  {base}/video/generations/{id}   (poll until status == "completed")
       -> {"status": "completed", "url"|"video":{"url"} ...}
  3. download the URL.

NOTE: xAI's video API is young and the exact paths/fields may differ from the
deployed revision — ``_extract_url`` is tolerant and errors clearly. Verify with
one smoke clip before relying on this path; AiNxt is the proven default.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from app.core.config import settings
from app.services.video.base import (
    CLIP_TIMEOUT, ClipResult, VideoProvider, VideoProviderError,
    backoff_sleep, download_to,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 6.0
_POLL_MAX_ATTEMPTS = 50


def _extract_url(body: dict) -> str | None:
    for key in ("url", "video_url", "download_url"):
        if body.get(key):
            return body[key]
    video = body.get("video") or {}
    if isinstance(video, dict) and (video.get("url") or video.get("uri")):
        return video.get("url") or video.get("uri")
    data = body.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("url") or data[0].get("uri")
    return None


class GrokVideoProvider(VideoProvider):
    name = "grok"

    def __init__(self, model: str | None = None):
        self.model = (model or settings.grok_video_model or "grok-imagine-video").strip()

    def _base(self) -> str:
        return (settings.grok_base_url or "https://api.x.ai/v1").rstrip("/")

    def _headers(self) -> dict:
        key = (settings.grok_api_key or "").strip()
        if not key:
            raise VideoProviderError("GROK_API_KEY is not configured")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def generate_clip(
        self, *, prompt: str, duration_sec: int, out_path: Path,
        aspect_ratio: str = "16:9", chat_id: str | None = None,
    ) -> ClipResult:
        base = self._base()
        headers = self._headers()
        submit_url = f"{base}/video/generations"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "duration_seconds": int(duration_sec),
            "aspect_ratio": aspect_ratio,
        }
        max_retries = int(getattr(settings, "engine_rate_limit_max_retries", 5))

        async with httpx.AsyncClient(timeout=CLIP_TIMEOUT, follow_redirects=True) as client:
            gen_id = None
            for attempt in range(max_retries + 1):
                try:
                    resp = await client.post(submit_url, headers=headers, json=payload)
                    resp.raise_for_status()
                    gen_id = resp.json().get("id")
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429 and attempt < max_retries:
                        await backoff_sleep(attempt)
                        continue
                    raise VideoProviderError(
                        f"grok submit failed: {exc.response.status_code} {exc.response.text[:300]}"
                    ) from exc
            if not gen_id:
                raise VideoProviderError("grok submit returned no id")

            poll_url = f"{submit_url}/{gen_id}"
            body: dict = {}
            for _ in range(_POLL_MAX_ATTEMPTS):
                await asyncio.sleep(_POLL_INTERVAL_SEC)
                resp = await client.get(poll_url, headers=headers)
                resp.raise_for_status()
                body = resp.json()
                status = (body.get("status") or "").lower()
                if status in ("completed", "succeeded", "success"):
                    break
                if status in ("failed", "error", "cancelled"):
                    raise VideoProviderError(f"grok generation {gen_id} {status}: {body}")
            else:
                raise VideoProviderError(f"grok generation {gen_id} did not complete in time")

            url = _extract_url(body)
            if not url:
                raise VideoProviderError(f"grok response missing video url: {body}")
            await download_to(client, url, out_path, headers=headers)

        return ClipResult(path=out_path, duration_sec=int(duration_sec), model=self.model)
