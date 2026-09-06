# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Orchestrates segmented video generation for a Product Kit video doc.

For one ``promo_video`` / ``explainer_video``:
  1. load the latest ProductKitDocument's ``script_json`` (a VideoScript)
  2. generate one ≤8s clip per segment via the configured VideoProvider,
     emitting per-segment progress through job_registry
  3. ffmpeg-concat the clips into one final MP4
  4. set ``file_path`` to the merged MP4 (served by the existing video endpoint)

Async (provider calls are async httpx); the Celery task wraps it with
``asyncio.run``. Cooperative cancellation via ``job_registry.is_cancelled``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.product_kit import ProductKitDocType, ProductKitDocument
from app.services import job_registry
from app.services.product_kit_query import latest_kit_doc
from app.services.video import get_video_provider
from app.agents.video_script_schema import VideoScript

logger = logging.getLogger(__name__)


class VideoGenError(RuntimeError):
    pass


def _artifact_dir(change_id: str, job_id: str) -> Path:
    path = Path(settings.artifacts_dir) / "video" / change_id / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _merge_clips(clip_paths: list[Path], out_path: Path) -> None:
    """ffmpeg concat-demuxer merge, re-encoding to uniform h264/aac."""
    listing = out_path.parent / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in clip_paths))
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not out_path.exists():
        raise VideoGenError(f"ffmpeg concat failed: {stderr.decode()[:400]}")


async def generate_video(
    *, change_id: str, doc_type: str, job_id: str,
    provider: str | None = None, model: str | None = None,
) -> dict:
    """Generate + merge the video for one doc. Returns the job result dict."""
    if doc_type not in ("promo_video", "explainer_video"):
        raise VideoGenError(f"video generation not supported for '{doc_type}'")

    db = SessionLocal()
    try:
        row = latest_kit_doc(db, change_id, ProductKitDocType(doc_type))
        if row is None or not row.script_json:
            raise VideoGenError("no generated script found; generate the script first")
        script = VideoScript.model_validate(row.script_json)
        if not script.segments:
            raise VideoGenError("script has no segments")

        prov = provider or row.video_provider or settings.video_provider
        mdl = model or row.video_model or settings.video_model
        vp = get_video_provider(prov, mdl)
        chat_id = uuid.uuid4().hex
        out_dir = _artifact_dir(change_id, job_id)
        total = len(script.segments)
        clip_paths: list[Path] = []
        total_cost = 0.0

        for seg in script.segments:
            if job_registry.is_cancelled(db, job_id):
                raise VideoGenError("cancelled")
            job_registry.update_job(
                db, job_id, progress_pct=int((seg.index - 1) / total * 100),
                current_stage=f"Generating segment {seg.index}/{total}",
            )
            clip = out_dir / f"seg_{seg.index:02d}.mp4"
            result = await vp.generate_clip(
                prompt=seg.visual_prompt, duration_sec=seg.duration_sec,
                out_path=clip, aspect_ratio=script.aspect_ratio or settings.video_aspect_ratio,
                chat_id=chat_id,
            )
            total_cost += result.cost_usd
            clip_paths.append(clip)

        job_registry.update_job(
            db, job_id, progress_pct=95, current_stage=f"Merging {total} segments",
        )
        final = out_dir / "final.mp4"
        await _merge_clips(clip_paths, final)

        row.file_path = str(final)
        row.video_provider = prov
        row.video_model = mdl
        db.commit()

        return {
            "output_path": str(final), "segments": total,
            "cost_usd": round(total_cost, 4), "provider": prov, "model": mdl,
        }
    finally:
        db.close()
