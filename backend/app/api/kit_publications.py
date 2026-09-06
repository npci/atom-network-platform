# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Kit publications API — read access to the immutable per-version kit snapshots.

Endpoints:
  GET /changes/{id}/kit-publications                       — list all published versions
  GET /changes/{id}/kit-publications/{negotiation_version} — full envelope for one version
"""
import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.core.deps import AdminUser, DbDep
from app.models.kit_publication import KitPublication

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/changes", tags=["kit-publications"])


def _summary(row: KitPublication) -> dict:
    return {
        "id": row.id,
        "negotiation_version": row.negotiation_version,
        "envelope_sha256": row.envelope_sha256,
        "source_doc_versions": row.source_doc_versions,
        "revision_reason": row.revision_reason,
        "resolver_action": row.resolver_action,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "published_by": row.published_by,
        "doc_count": len((row.source_doc_versions or {})),
    }


@router.get("/{change_id}/kit-publications")
def list_publications(change_id: str, db: DbDep = None, _: AdminUser = None):
    """All published kit versions for a change, newest first."""
    rows = db.scalars(
        select(KitPublication)
        .where(KitPublication.change_request_id == change_id)
        .order_by(KitPublication.negotiation_version.desc())
    ).all()
    return {"change_request_id": change_id, "publications": [_summary(r) for r in rows]}


@router.get("/{change_id}/kit-publications/{negotiation_version}")
def get_publication(
    change_id: str, negotiation_version: int, db: DbDep = None, _: AdminUser = None,
):
    """Full snapshot (incl. the shipped envelope) for one published version."""
    row = db.scalars(
        select(KitPublication).where(
            KitPublication.change_request_id == change_id,
            KitPublication.negotiation_version == negotiation_version,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="no publication for that version")
    return {**_summary(row), "envelope": row.envelope}
