# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Product Kit video upload/serve — promo_video / explainer_video.

The promo/explainer Product Kit docs generate a *script*. The PM produces the
actual video off-platform from that script and uploads the MP4 here; it attaches
to the doc's `file_path` and ships to the partner with the kit
(see services/change_dispatch.py). MP4 only, 25 MB cap.
"""
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.deps import CurrentUser, DbDep
from app.models.product_kit import ProductKitDocType, ProductKitDocument
from app.services import job_registry
from app.services.product_kit_query import (
    kit_doc_by_version, kit_doc_versions, latest_kit_doc,
)
from app.services.video import VIDEO_OPTIONS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["product-kit-video"])

VIDEO_DOC_TYPES = {"promo_video", "explainer_video"}
MAX_VIDEO_BYTES = 25 * 1024 * 1024  # 25 MB (confirmed with product)
_MB = 1024 * 1024


def _default_duration(doc_type: str) -> int:
    return (settings.promo_video_duration_sec if doc_type == "promo_video"
            else settings.explainer_video_duration_sec)


def _validate_doc_type(doc_type: str) -> ProductKitDocType:
    if doc_type not in VIDEO_DOC_TYPES:
        raise HTTPException(status_code=400, detail=f"Video upload not supported for '{doc_type}'.")
    return ProductKitDocType(doc_type)


@router.post("/changes/{change_id}/product-kit/{doc_type}/video")
async def upload_product_kit_video(
    change_id: str,
    doc_type: str,
    db: DbDep,
    current_user: CurrentUser,
    file: UploadFile = File(...),
):
    """Upload (or replace) the MP4 for a promo/explainer video. Attaches to the
    latest ProductKitDocument of that type so it ships with the kit."""
    dt_enum = _validate_doc_type(doc_type)

    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=415, detail="Only .mp4 files are accepted.")
    # Size guard: read one byte past the cap to detect oversize without buffering
    # an unbounded upload.
    raw = await file.read(MAX_VIDEO_BYTES + 1)
    if len(raw) > MAX_VIDEO_BYTES:
        raise HTTPException(status_code=413, detail=f"Video too large (max {MAX_VIDEO_BYTES // _MB} MB).")
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")

    # Path-traversal-safe: filename built from the validated enum only, never the
    # uploaded name (same guard as change_requests.upload_artifact).
    base = Path(settings.artifacts_dir) / "sessions" / change_id
    base.mkdir(parents=True, exist_ok=True)
    saved = base / f"video_{dt_enum.value}.mp4"
    saved.write_bytes(raw)

    # Attach to the latest doc row (carries the script); create one if the script
    # hasn't been generated yet.
    row = latest_kit_doc(db, change_id, dt_enum)
    if row is None:
        row = ProductKitDocument(change_request_id=change_id, doc_type=dt_enum, version=1)
        db.add(row)
    row.file_path = str(saved)
    db.commit()

    logger.info("Product-kit video uploaded: change=%s doc_type=%s bytes=%d by=%s",
                change_id, doc_type, len(raw), current_user.id)
    return {"ok": True, "doc_type": doc_type, "filename": filename, "size_bytes": len(raw)}


@router.get("/changes/{change_id}/product-kit/{doc_type}/video/options")
def get_video_gen_options(change_id: str, doc_type: str, current_user: CurrentUser):
    """Provider/model choices + the doc-type default duration, for the UI pickers.
    Driven from settings so config changes flow to the UI without a frontend edit."""
    _validate_doc_type(doc_type)
    return {
        "enabled": settings.video_generation_enabled,
        "providers": VIDEO_OPTIONS,
        "default_provider": settings.video_provider,
        "default_model": settings.video_model,
        "default_duration_sec": _default_duration(doc_type),
        "segment_max_sec": settings.video_segment_max_sec,
        "aspect_ratio": settings.video_aspect_ratio,
    }


@router.get("/changes/{change_id}/product-kit/{doc_type}/versions")
def list_video_script_versions(change_id: str, doc_type: str, db: DbDep, current_user: CurrentUser):
    """All saved script versions for a video doc (newest first), for the history UI.
    Each generation/revision is its own version (never overwritten)."""
    dt_enum = _validate_doc_type(doc_type)
    rows = kit_doc_versions(db, change_id, dt_enum)
    return [
        {
            "version": r.version,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "provider": r.video_provider,
            "model": r.video_model,
            "duration_sec": r.video_duration_sec,
            "has_video": bool(r.file_path and os.path.exists(r.file_path)),
            "segments": len((r.script_json or {}).get("segments", [])) if r.script_json else 0,
        }
        for r in rows
    ]


@router.get("/changes/{change_id}/product-kit/{doc_type}/versions/{version}")
def get_video_script_version(change_id: str, doc_type: str, version: int,
                             db: DbDep, current_user: CurrentUser):
    """Full content + structured script for one saved version (read-only view)."""
    dt_enum = _validate_doc_type(doc_type)
    row = kit_doc_by_version(db, change_id, dt_enum, version)
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found.")
    return {
        "version": row.version,
        "content": row.content,
        "script_json": row.script_json,
        "provider": row.video_provider,
        "model": row.video_model,
        "duration_sec": row.video_duration_sec,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "has_video": bool(row.file_path and os.path.exists(row.file_path)),
    }


@router.post("/changes/{change_id}/product-kit/{doc_type}/video/generate")
def generate_product_kit_video(
    change_id: str,
    doc_type: str,
    db: DbDep,
    current_user: CurrentUser,
    provider: str | None = Body(default=None),
    model: str | None = Body(default=None),
):
    """Kick off AI video generation for a doc whose segmented script exists.
    Creates a job, dispatches the Celery task, returns the job_id to poll."""
    if not settings.video_generation_enabled:
        raise HTTPException(status_code=403, detail="Video generation is disabled.")
    dt_enum = _validate_doc_type(doc_type)

    row = latest_kit_doc(db, change_id, dt_enum)
    if row is None or not row.script_json:
        raise HTTPException(status_code=409, detail="Generate the video script first.")

    prov = (provider or settings.video_provider).strip()
    mdl = (model or settings.video_model).strip()
    if prov not in VIDEO_OPTIONS or mdl not in VIDEO_OPTIONS.get(prov, []):
        raise HTTPException(status_code=400, detail=f"Unsupported provider/model: {prov}/{mdl}.")

    # Record the choice so the runner + UI agree on what's being generated.
    row.video_provider, row.video_model = prov, mdl
    db.commit()

    job_id = job_registry.create_job(
        db, change_request_id=change_id, module="video_gen", subtype=doc_type,
        started_by_user_id=current_user.id,
        metadata={"provider": prov, "model": mdl},
    )
    from app.services.celery_tasks import generate_video_task
    generate_video_task.delay(change_id, doc_type, job_id, prov, mdl)

    logger.info("Video generation dispatched: change=%s doc=%s job=%s provider=%s model=%s",
                change_id, doc_type, job_id, prov, mdl)
    return {"job_id": job_id, "provider": prov, "model": mdl}


@router.get("/changes/{change_id}/product-kit/{doc_type}/video")
def get_product_kit_video(
    change_id: str,
    doc_type: str,
    db: DbDep,
    current_user: CurrentUser,
):
    """Stream the uploaded MP4 for inline playback / download."""
    dt_enum = _validate_doc_type(doc_type)
    row = latest_kit_doc(db, change_id, dt_enum)
    path = row.file_path if row else None
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No video uploaded for this document.")
    return FileResponse(path, media_type="video/mp4", filename=f"{doc_type}.mp4")
