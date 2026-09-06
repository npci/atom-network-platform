# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""AiNxt gateway video provider (proven path).

Two-call flow (schema confirmed against the live gateway):

  1. POST {ainxt_base_url}{ainxt_video_generate_path}
       body: {prompt, chat_id, aspect_ratio, duration_secs}
       -> blocks ~60–70s, returns metadata:
          {video_id, url:"/chat/video/{id}", mime:"video/mp4",
           duration_secs, model, cost_usd, billed, ...}
  2. GET  {ainxt_base_url}{ainxt_video_fetch_path}/{video_id}  -> MP4 bytes

Auth = ``Authorization: Bearer {ainxt_api_key}`` (same as the chat client).
The gateway selects the engine (veo / grok); we pass ``model`` in the body in
case the gateway honours it (the observed request omitted it — harmless if
ignored).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import httpx

from app.core.config import settings
from app.services.video.base import (
    CLIP_TIMEOUT, ClipResult, VideoProvider, VideoProviderError,
    backoff_sleep, download_to,
)

logger = logging.getLogger(__name__)


class AiNxtVideoProvider(VideoProvider):
    name = "ainxt"

    def __init__(self, model: str | None = None):
        self.model = (model or settings.video_model or "veo-3.1-generate-preview").strip()

    def _base(self) -> str:
        return (settings.ainxt_base_url or "").rstrip("/")

    def _headers(self) -> dict:
        # Dedicated video key if set, else fall back to the chat/LLM key.
        key = (settings.ainxt_video_api_key or settings.ainxt_api_key or "").strip()
        if not key:
            raise VideoProviderError("AINXT_VIDEO_API_KEY / AINXT_API_KEY is not configured")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def generate_clip(
        self, *, prompt: str, duration_sec: int, out_path: Path,
        aspect_ratio: str = "16:9", chat_id: str | None = None,
    ) -> ClipResult:
        base = self._base()
        gen_url = base + settings.ainxt_video_generate_path
        headers = self._headers()
        payload = {
            "prompt": prompt,
            "chat_id": chat_id or uuid.uuid4().hex,
            "aspect_ratio": aspect_ratio,
            "duration_secs": int(duration_sec),
            "model": self.model,
        }
        max_retries = int(getattr(settings, "engine_rate_limit_max_retries", 5))

        async with httpx.AsyncClient(timeout=CLIP_TIMEOUT, follow_redirects=True) as client:
            meta: dict | None = None
            for attempt in range(max_retries + 1):
                try:
                    resp = await client.post(gen_url, headers=headers, json=payload)
                    resp.raise_for_status()
                    meta = resp.json()
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429 and attempt < max_retries:
                        await backoff_sleep(attempt)
                        continue
                    raise VideoProviderError(
                        f"ainxt video-generate failed: {exc.response.status_code} {exc.response.text[:300]}"
                    ) from exc
                except httpx.HTTPError as exc:
                    if attempt < max_retries:
                        await backoff_sleep(attempt)
                        continue
                    raise VideoProviderError(f"ainxt video-generate error: {exc}") from exc

            if not meta:
                raise VideoProviderError("ainxt video-generate returned no metadata")

            video_id = meta.get("video_id")
            if not video_id:
                raise VideoProviderError(f"ainxt response missing video_id: {meta}")

            # Prefer the relative url the gateway hands back; fall back to the
            # configured fetch path.
            rel = meta.get("url") or f"{settings.ainxt_video_fetch_path}/{video_id}"
            fetch_url = base + rel if rel.startswith("/") else rel
            await download_to(client, fetch_url, out_path, headers=headers)

        return ClipResult(
            path=out_path,
            duration_sec=int(meta.get("duration_secs") or duration_sec),
            cost_usd=float(meta.get("cost_usd") or 0.0),
            model=str(meta.get("model") or self.model),
            meta={"video_id": video_id, "latency_ms": meta.get("latency_ms")},
        )
