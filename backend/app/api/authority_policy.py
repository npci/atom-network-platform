# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin API — Authority Policy doc.

Single-row table holding the authoritative AUTHORITY_POLICY.md content loaded
by the feasibility resolver. Admin-only. Three endpoints:

  GET  /admin/authority-policy        — current content + last-updated metadata
  PUT  /admin/authority-policy        — replace content via JSON body
  POST /admin/authority-policy/upload — replace content via multipart file upload

The PUT and upload endpoints both write the same singleton row (id=1).
The row is seeded from the bind-mounted file on first boot (see
`app.main._seed_authority_policy_from_file`); admins maintain it via this
API thereafter.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.deps import AdminUser, DbDep
from app.models.base import utcnow
from app.models.authority_policy import AuthorityPolicy

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/authority-policy", tags=["admin-authority-policy"])


MAX_POLICY_BYTES = 256 * 1024  # 256 KB ceiling — sane for a markdown brief


class PolicyResponse(BaseModel):
    content: str
    updated_by: str | None = None
    updated_at: str | None = None
    size_bytes: int


class PolicyUpdate(BaseModel):
    content: str = Field(..., description="Full markdown content; replaces existing")


def _get_or_create_row(db) -> AuthorityPolicy:
    """Fetch the singleton row, materialising an empty one if absent.

    `app.main._seed_authority_policy_from_file` normally creates this row on
    first boot. This fallback covers cases where the seed ran before the
    file was mounted (e.g. fresh DB + no seed file) — admin can still
    paste content into the UI.
    """
    row = db.get(AuthorityPolicy, 1)
    if row is None:
        row = AuthorityPolicy(id=1, content="")
        db.add(row)
        db.flush()
    return row


def _to_response(row: AuthorityPolicy) -> PolicyResponse:
    return PolicyResponse(
        content=row.content or "",
        updated_by=row.updated_by,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        size_bytes=len((row.content or "").encode("utf-8")),
    )


@router.get("", response_model=PolicyResponse)
def get_policy(db: DbDep, _: AdminUser):
    """Return the current policy doc. Admin-only — content is internal."""
    row = _get_or_create_row(db)
    db.commit()
    return _to_response(row)


@router.put("", response_model=PolicyResponse)
def update_policy(payload: PolicyUpdate, db: DbDep, current: AdminUser):
    """Replace the policy content. The whole document is rewritten on each
    save — no per-section patching to keep the contract simple."""
    raw = payload.content or ""
    if len(raw.encode("utf-8")) > MAX_POLICY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Policy exceeds {MAX_POLICY_BYTES} bytes ceiling",
        )
    row = _get_or_create_row(db)
    row.content = raw
    row.updated_by = current.id
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    logger.info(
        "Authority policy updated by %s (%d bytes)", current.id, len(raw.encode("utf-8")),
    )
    return _to_response(row)


@router.post("/upload", response_model=PolicyResponse)
async def upload_policy(db: DbDep, current: AdminUser, file: UploadFile = File(...)):
    """Replace the policy content from an uploaded markdown file. The file
    is read as UTF-8 text — same shape as the bind-mounted seed file."""
    raw = await file.read()
    if len(raw) > MAX_POLICY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {MAX_POLICY_BYTES} bytes ceiling",
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File is not valid UTF-8: {e}",
        )
    row = _get_or_create_row(db)
    row.content = content
    row.updated_by = current.id
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    logger.info(
        "Authority policy uploaded by %s from %r (%d bytes)",
        current.id, file.filename, len(raw),
    )
    return _to_response(row)


@router.post("/reset-to-seed", response_model=PolicyResponse)
def reset_to_seed(db: DbDep, current: AdminUser):
    """Restore content from the bind-mounted seed file. Useful when an
    edit needs to be reverted to the shipped baseline."""
    seed_path = Path(settings.authority_policy_path)
    if not seed_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Seed file not found at {seed_path}",
        )
    content = seed_path.read_text(encoding="utf-8")
    row = _get_or_create_row(db)
    row.content = content
    row.updated_by = current.id
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    logger.info(
        "Authority policy reset to seed by %s (%d bytes)",
        current.id, len(content.encode("utf-8")),
    )
    return _to_response(row)
