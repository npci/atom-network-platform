# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Persistence for the per-feature Decline & Timeout design artifact.

CRUD + approval for ``DeclineSpec`` rows. Only APPROVED specs drive
certification (same gate discipline as BRD/TSD). The cert engine reads the
latest APPROVED spec for a change via :func:`get_latest_approved`.
"""
import logging

from sqlalchemy.orm import Session

from app.models.approval import Approval, ApprovalArtifactType, ApprovalStatus
from app.models.base import utcnow
from app.models.decline_spec import DeclineSpec
from app.models.research import ArtifactStatus
from app.excel_testcase_engine.schemas.decline_spec import FeatureDeclineSpec

logger = logging.getLogger(__name__)


def _latest(db: Session, change_request_id: str) -> DeclineSpec | None:
    return (
        db.query(DeclineSpec)
        .filter(DeclineSpec.change_request_id == change_request_id)
        .order_by(DeclineSpec.version.desc())
        .first()
    )


def get_latest(db: Session, change_request_id: str) -> DeclineSpec | None:
    """Most recent spec row for a change (any status)."""
    return _latest(db, change_request_id)


def get_latest_approved(db: Session, change_request_id: str) -> DeclineSpec | None:
    """Most recent APPROVED spec — the only one the cert engine consumes."""
    return (
        db.query(DeclineSpec)
        .filter(
            DeclineSpec.change_request_id == change_request_id,
            DeclineSpec.status == ArtifactStatus.APPROVED,
        )
        .order_by(DeclineSpec.version.desc())
        .first()
    )


def create_draft(
    db: Session, change_request_id: str, spec: FeatureDeclineSpec
) -> DeclineSpec:
    """Persist a new DRAFT spec, version-bumped above any existing row."""
    prev = _latest(db, change_request_id)
    row = DeclineSpec(
        change_request_id=change_request_id,
        spec_json=spec.model_dump(),
        version=(prev.version + 1) if prev else 1,
        status=ArtifactStatus.DRAFT,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("DeclineSpec DRAFT created: change=%s v%d rows=%d",
                change_request_id, row.version, len(spec.rows))
    return row


def update_draft(db: Session, decline_spec_id: str, spec: FeatureDeclineSpec) -> DeclineSpec:
    """Overwrite a DRAFT spec's content after human keep/edit/drop. Approved specs
    are immutable — re-edits create a new draft, so callers must check status."""
    row = db.query(DeclineSpec).filter(DeclineSpec.id == decline_spec_id).first()
    if row is None:
        raise ValueError(f"DeclineSpec {decline_spec_id} not found")
    if row.status == ArtifactStatus.APPROVED:
        raise ValueError("Cannot edit an APPROVED decline spec; create a new draft instead")
    row.spec_json = spec.model_dump()
    db.commit()
    db.refresh(row)
    return row


def approve(db: Session, decline_spec_id: str, user_id: str) -> DeclineSpec:
    """Flip a spec to APPROVED and record the Approval. (Code auto-promotion into
    the catalog is wired in Phase 3 via services/decline_promote.)"""
    row = db.query(DeclineSpec).filter(DeclineSpec.id == decline_spec_id).first()
    if row is None:
        raise ValueError(f"DeclineSpec {decline_spec_id} not found")
    row.status = ArtifactStatus.APPROVED
    row.approved_by = user_id
    row.approved_at = utcnow()
    db.add(Approval(
        artifact_type=ApprovalArtifactType.DECLINE_SPEC,
        artifact_id=row.id,
        approver_id=user_id,
        status=ApprovalStatus.APPROVED,
        responded_at=utcnow(),
    ))
    db.commit()
    db.refresh(row)
    logger.info("DeclineSpec APPROVED: id=%s by=%s", row.id, user_id)
    return row


def load_spec(row: DeclineSpec) -> FeatureDeclineSpec:
    """Deserialize a stored row back into the validated Pydantic model."""
    return FeatureDeclineSpec.model_validate(row.spec_json or {})
