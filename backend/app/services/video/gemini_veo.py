# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Google Veo (via the Gemini Generative Language API) video provider.

Long-running-operation flow:

  1. POST {base}/models/{model}:predictLongRunning
       body: {instances:[{prompt}], parameters:{aspectRatio, durationSeconds}}
       -> {"name": "operations/...."}
  2. GET  {base}/{operation_name}   (poll until {"done": true})
  3. download the returned video URI (append ?key=API_KEY)

Auth = ``x-goog-api-key`` header (reuses ``gemini_api_key``). The Veo response
field path can drift across preview revisions; ``_extract_video_uri`` is tolerant
and raises a clear error if no URI is found.
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

_POLL_INTERVAL_SEC = 8.0
_POLL_MAX_ATTEMPTS = 40   # ~5+ minutes ceiling per clip


def _extract_video_uri(op_response: dict) -> str | None:
    """Find the generated video URI in a Veo operation response (tolerant)."""
    resp = op_response.get("response") or {}
    # Common shapes across preview revisions.
    candidates = (
        resp.get("generateVideoResponse", {}).get("generatedSamples"),
        resp.get("generatedSamples"),
        resp.get("videos"),
    )
    for samples in candidates:
        if not samples:
            continue
        first = samples[0] if isinstance(samples, list) else samples
        if isinstance(first, dict):
            video = first.get("video") or first
            uri = video.get("uri") or video.get("url")
            if uri:
                return uri
    return None


class GeminiVeoProvider(VideoProvider):
    name = "gemini"

    def __init__(self, model: str | None = None):
        self.model = (model or settings.gemini_video_model or "veo-3.1-generate-preview").strip()

    def _base(self) -> str:
        return (settings.gemini_video_base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

    def _headers(self) -> dict:
        key = (settings.gemini_api_key or "").strip()
        if not key:
            raise VideoProviderError("GEMINI_API_KEY is not configured")
        return {"x-goog-api-key": key, "Content-Type": "application/json"}

    async def generate_clip(
        self, *, prompt: str, duration_sec: int, out_path: Path,
        aspect_ratio: str = "16:9", chat_id: str | None = None,
    ) -> ClipResult:
        base = self._base()
        headers = self._headers()
        submit_url = f"{base}/models/{self.model}:predictLongRunning"
        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {"aspectRatio": aspect_ratio, "durationSeconds": int(duration_sec)},
        }
        max_retries = int(getattr(settings, "engine_rate_limit_max_retries", 5))

        async with httpx.AsyncClient(timeout=CLIP_TIMEOUT, follow_redirects=True) as client:
            # 1. Submit
            op_name = None
            for attempt in range(max_retries + 1):
                try:
                    resp = await client.post(submit_url, headers=headers, json=payload)
                    resp.raise_for_status()
                    op_name = resp.json().get("name")
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429 and attempt < max_retries:
                        await backoff_sleep(attempt)
                        continue
                    raise VideoProviderError(
                        f"veo submit failed: {exc.response.status_code} {exc.response.text[:300]}"
                    ) from exc
            if not op_name:
                raise VideoProviderError("veo submit returned no operation name")

            # 2. Poll
            op_url = f"{base}/{op_name}"
            op_json: dict = {}
            for _ in range(_POLL_MAX_ATTEMPTS):
                await asyncio.sleep(_POLL_INTERVAL_SEC)
                resp = await client.get(op_url, headers=headers)
                resp.raise_for_status()
                op_json = resp.json()
                if op_json.get("done"):
                    break
            else:
                raise VideoProviderError(f"veo operation {op_name} did not complete in time")

            if op_json.get("error"):
                raise VideoProviderError(f"veo operation error: {op_json['error']}")

            # 3. Download
            uri = _extract_video_uri(op_json)
            if not uri:
                raise VideoProviderError(f"veo response missing video uri: {op_json.get('response')}")
            sep = "&" if "?" in uri else "?"
            dl_url = f"{uri}{sep}key={(settings.gemini_api_key or '').strip()}"
            await download_to(client, dl_url, out_path)

        return ClipResult(path=out_path, duration_sec=int(duration_sec), model=self.model)
