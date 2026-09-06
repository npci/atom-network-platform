# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import io
import logging
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as urlquote

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, func, text
from app.core.deps import DbDep, CurrentUser, AdminUser
from app.core.error_taxonomy import client_safe_detail

logger = logging.getLogger(__name__)
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.phase_b import PhaseBRun, PhaseBRunStatus, PhaseBStep
from app.models.phase_c import ChangePartnerAssignment, AssignmentStatus
from app.schemas.change_request import (
    ChangeRequestCreate,
    ChangeRequestUpdate,
    ChangeRequestResponse,
    ChangeRequestDetailResponse,
    ChangeRequestListResponse,
    PhaseSummary,
)


# ── Phase summary helpers ────────────────────────────────────────────────────

_PHASE_A_LABELS = {
    "prompt_enhancement": "Prompt Enhancement",
    "research":           "Research",
    "canvas":             "Canvas",
    "clarification":      "Clarification",
    "brd":                "BRD",
    "tech_spec":          "Tech Spec",
    "xsd":                "XSD",
    "product_kit":        "Product Kit",
    "completed":          "Completed",
}

_PHASE_B_STEP_LABELS = {
    "code_change":  "Code Generation",
    "code_review":  "Code Review",
    "is_review":    "IS Review",
    "git":          "GitLab",
    "build":        "Build",
    "deploy":       "Deploy",
    "test_gen":     "UAT Test Gen",
    "test_exec":    "UAT Execution",
    "triage":       "Triage",
    "completed":    "Completed",
}


def _phase_a_summary(change: ChangeRequest) -> PhaseSummary:
    status_value = change.status.value if hasattr(change.status, "value") else str(change.status)
    label = _PHASE_A_LABELS.get(status_value, status_value.replace("_", " ").title())
    if status_value == "completed":
        return PhaseSummary(state="completed", label="Completed")
    return PhaseSummary(state="in_progress", label=label)


def _phase_b_summary(run: PhaseBRun | None) -> PhaseSummary:
    if not run:
        return PhaseSummary(state="not_started", label="Not Started")
    run_status = run.status.value if hasattr(run.status, "value") else str(run.status)
    step_value = run.current_step.value if hasattr(run.current_step, "value") else str(run.current_step)
    step_label = _PHASE_B_STEP_LABELS.get(step_value, step_value.replace("_", " ").title())
    if run_status == "completed":
        return PhaseSummary(state="completed", label="Completed")
    if run_status == "blocked":
        return PhaseSummary(state="blocked", label="Blocked", detail=f"at {step_label}")
    return PhaseSummary(state="in_progress", label=step_label)


def _phase_c_summary(assignments: list[ChangePartnerAssignment]) -> PhaseSummary:
    if not assignments:
        return PhaseSummary(state="not_started", label="Not Started")
    total = len(assignments)
    statuses = [a.status.value if hasattr(a.status, "value") else str(a.status) for a in assignments]
    certified = sum(1 for s in statuses if s == "certified")
    only_assigned = all(s == "assigned" for s in statuses)
    if certified == total:
        return PhaseSummary(state="completed", label="Certified", detail=f"{total}/{total} partners")
    if only_assigned:
        return PhaseSummary(state="in_progress", label="Assigned", detail=f"{total} partner{'s' if total != 1 else ''}")
    return PhaseSummary(state="in_progress", label="In Progress", detail=f"{certified}/{total} certified")

router = APIRouter(prefix="/changes", tags=["change-requests"])


@router.post("", response_model=ChangeRequestResponse, status_code=status.HTTP_201_CREATED)
def create_change_request(payload: ChangeRequestCreate, db: DbDep, current_user: CurrentUser):
    # v2 (BRD→XSD→TSD) is the default flow for every new change — the XSD is decided
    # before the Tech Spec so the TSD is authored against approved real schemas.
    # The title is GENERATED from the idea, never typed (see ChangeRequestCreate).
    #
    # Runs on the HOST event loop via anyio.from_thread, NOT asyncio.run. Upstream
    # shipped this call site with asyncio.run and fixed the identical pattern in
    # api/agents.py one commit later: every provider client in core/llm.py is a
    # module-level @lru_cache AsyncAnthropic/AsyncOpenAI whose httpx pool binds to the
    # loop that first used it, so asyncio.run drives that shared client from a second
    # loop and then CLOSES it — poisoning the cached client for the WHOLE PROCESS, not
    # merely failing this request. Fail-open: on any LLM failure the change is created
    # untitled and titled later (source-document upload below, or enhancement
    # acceptance) rather than blocking creation.
    from functools import partial as _partial

    import anyio.from_thread as _from_thread

    from app.agents.prompt_enhancer import generate_change_title
    try:
        _title = _from_thread.run(_partial(generate_change_title, payload.initial_prompt))
    except Exception as e:  # noqa: BLE001 — fail-open; an untitled change still creates
        logger.warning("create: title generation failed (%s) — creating untitled", e)
        _title = None
    change = ChangeRequest(
        title=_title,
        initial_prompt=payload.initial_prompt,
        status=ChangeStatus.PROMPT_ENHANCEMENT,
        created_by=current_user.id,
        workflow_version=2,
    )
    db.add(change)
    db.commit()
    db.refresh(change)
    logger.info("Change request created: id=%s title='%s' user=%s", change.id, change.title, current_user.username)
    return ChangeRequestResponse.model_validate(change)


def _can_read_all_changes(user) -> bool:
    """Roles with read-only visibility into ALL change requests (not just their
    own): admin + the Risk/InfoSec/Tech review teams. The teams need to SEE the
    change list and reach each change's Product Kit — but ONLY the Product Kit,
    not the BRD/TSD/Phase-B/C artifacts. So this gates the list + product_kit
    downloads only; full change detail / artifact summary use
    `_is_creator_or_admin` below."""
    from app.models.user import UserRole
    return user.role in (
        UserRole.ADMIN,
        UserRole.RISK_REVIEWER,
        UserRole.INFOSEC_REVIEWER,
        UserRole.TECH_LEAD,
    )


def _is_creator_or_admin(user, change) -> bool:
    """Full read access — the change creator or an admin. Review teams are
    intentionally excluded (they get product-kit-only access)."""
    from app.models.user import UserRole
    return user.role == UserRole.ADMIN or change.created_by == user.id


@router.get("", response_model=ChangeRequestListResponse)
def list_change_requests(
    db: DbDep,
    current_user: CurrentUser,
    skip: int = 0,
    limit: int = 20,
    status_filter: ChangeStatus | None = None,
):
    query = select(ChangeRequest)

    # Read-all roles (admin + review teams) see every change; everyone else
    # only their own.
    if not _can_read_all_changes(current_user):
        query = query.where(ChangeRequest.created_by == current_user.id)

    if status_filter:
        query = query.where(ChangeRequest.status == status_filter)

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(ChangeRequest.created_at.desc()).offset(skip).limit(limit)
    ).all()

    # Batch-load phase B/C data for the page to avoid N+1 queries.
    change_ids = [c.id for c in items]
    phase_b_by_change: dict[str, PhaseBRun] = {}
    phase_c_by_change: dict[str, list[ChangePartnerAssignment]] = {cid: [] for cid in change_ids}
    if change_ids:
        # Phase B: take the most-recent run per change (one row dict keyed by change_id).
        pb_runs = db.scalars(
            select(PhaseBRun)
            .where(PhaseBRun.change_request_id.in_(change_ids))
            .order_by(PhaseBRun.started_at.desc())
        ).all()
        for run in pb_runs:
            # Keep the latest (first encountered due to desc ordering)
            phase_b_by_change.setdefault(run.change_request_id, run)

        pc_assignments = db.scalars(
            select(ChangePartnerAssignment)
            .where(ChangePartnerAssignment.change_request_id.in_(change_ids))
        ).all()
        for a in pc_assignments:
            phase_c_by_change.setdefault(a.change_request_id, []).append(a)

    response_items: list[ChangeRequestResponse] = []
    for c in items:
        base = ChangeRequestResponse.model_validate(c)
        response_items.append(
            base.model_copy(update={
                "phase_a": _phase_a_summary(c),
                "phase_b": _phase_b_summary(phase_b_by_change.get(c.id)),
                "phase_c": _phase_c_summary(phase_c_by_change.get(c.id, [])),
            })
        )

    return ChangeRequestListResponse(total=total, items=response_items)


@router.get("/{change_id}", response_model=ChangeRequestDetailResponse)
def get_change_request(change_id: str, db: DbDep, current_user: CurrentUser):
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")

    if not _is_creator_or_admin(current_user, change):
        raise HTTPException(status_code=403, detail="Access denied")

    return ChangeRequestDetailResponse.model_validate(change)


@router.patch("/{change_id}", response_model=ChangeRequestResponse)
def update_change_request(
    change_id: str, payload: ChangeRequestUpdate, db: DbDep, current_user: CurrentUser
):
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    updates = payload.model_dump(exclude_none=True)
    for field, value in updates.items():
        setattr(change, field, value)

    db.commit()
    db.refresh(change)
    logger.info("Change request updated: id=%s fields=%s status=%s", change_id, list(updates.keys()), change.status.value)
    return ChangeRequestResponse.model_validate(change)


# ── Context cache endpoints (Sprint 3) ────────────────────────────────────────

@router.get("/{change_id}/context")
def get_context(change_id: str, db: DbDep, current_user: CurrentUser):
    """Return the cached context for a change request: taxonomy + proposals + chunks.

    Returns 200 with {"cached": false} if no row exists yet.
    """
    from app.models.user import UserRole
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    from app.models.change_request_context import ChangeRequestContext
    row = db.get(ChangeRequestContext, change_id)
    if row is None:
        return {"cached": False}

    return {
        "cached":              True,
        "taxonomy_primary":    row.taxonomy_primary,
        "taxonomy_labels":     row.taxonomy_labels,
        "taxonomy_confidence": row.taxonomy_confidence,
        "taxonomy_rationale":  row.taxonomy_rationale,
        "retrieved_chunks":    row.retrieved_chunks,
        "proposals":           row.proposals,
        "proposals_confidence": row.proposals_confidence,
        "last_refreshed_at":   row.last_refreshed_at.isoformat() if row.last_refreshed_at else None,
        "source_version":      row.source_version,
    }


@router.get("/blueprints/{doc_type}")
def get_blueprint(doc_type: str, _: CurrentUser):
    """Return the structural blueprint for a document type."""
    from app.agents.blueprints import get as bp_get
    bp = bp_get(doc_type)
    if bp is None:
        raise HTTPException(status_code=404, detail=f"No blueprint for doc_type={doc_type}")
    return bp


@router.post("/{change_id}/context/refresh")
async def refresh_context(change_id: str, db: DbDep, current_user: CurrentUser):
    """Force-rebuild the context cache for this change request.

    Triggers: taxonomy classification → 3-stage hybrid retrieval → proposals extraction.
    Usually auto-called at end of Research stage; expose this for manual refresh
    after research feedback, canvas revision, etc.
    """
    from app.models.user import UserRole
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    from app.services.context_cache import refresh
    row = await refresh(change_id, db)
    if row is None:
        raise HTTPException(status_code=400, detail="Context build failed — check backend logs")
    return {
        "ok":                  True,
        "taxonomy_primary":    row.taxonomy_primary,
        "proposals_confidence": row.proposals_confidence,
        "last_refreshed_at":   row.last_refreshed_at.isoformat(),
    }


# ── Artifact resolver + DOCX assembler (shared by download + build-all) ───────

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_XSD_MIME  = "application/xml"
_ZIP_MIME  = "application/zip"
_MD_MIME   = "text/markdown; charset=utf-8"


# XSD schema extraction lives in `app.services.xsd_bundle` so the operator
# download here and the partner kit shipment (`services.change_dispatch`) pull
# the same `.xsd` files from `XSD.content`. See that module for the format.
from app.services.xsd_bundle import extract_xsd_blocks as _extract_xsd_blocks


def _resolve_artifact_row(change_id: str, doc_type: str, db, subtype: str | None):
    """Return (row, docx_builder_doc_type, file_label) for the requested artifact.

    Raises HTTPException on invalid doc_type / missing subtype for product_kit.
    Row may be None (no artifact yet) — caller decides whether that's a 404.
    """
    doc_type_norm = (doc_type or "").lower()
    if doc_type_norm == "brd":
        from app.models.brd import BRD
        row = (db.query(BRD).filter(BRD.change_request_id == change_id)
               .order_by(BRD.version.desc()).first())
        label = f"brd_v{row.version}" if row else "brd"
        return row, "BRD", label
    if doc_type_norm in ("tech_spec", "tech-spec"):
        from app.models.tech_spec import TechSpec
        row = (db.query(TechSpec).filter(TechSpec.change_request_id == change_id)
               .order_by(TechSpec.version.desc()).first())
        label = f"tech_spec_v{row.version}" if row else "tech_spec"
        return row, "Technical Specification", label
    if doc_type_norm == "xsd":
        from app.models.xsd import XSD
        row = (db.query(XSD).filter(XSD.change_request_id == change_id)
               .order_by(XSD.version.desc()).first())
        label = f"xsd_v{row.version}" if row else "xsd"
        return row, "XSD", label
    if doc_type_norm == "canvas":
        from app.models.canvas import ProductCanvas
        row = (db.query(ProductCanvas).filter(ProductCanvas.change_request_id == change_id)
               .order_by(ProductCanvas.version.desc()).first())
        label = f"canvas_v{row.version}" if row else "canvas"
        return row, "Product Canvas", label
    if doc_type_norm == "product_kit":
        if not subtype:
            raise HTTPException(status_code=400, detail="subtype query param required for product_kit")
        from app.models.product_kit import ProductKitDocType
        from app.services.product_kit_query import latest_kit_doc
        try:
            dt_enum = ProductKitDocType(subtype)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown product-kit subtype: {subtype}")
        row = latest_kit_doc(db, change_id, dt_enum)
        label = f"product_kit_{subtype}_v{row.version}" if row else f"product_kit_{subtype}"
        return row, "Product Kit", label
    raise HTTPException(status_code=400, detail=f"Unknown doc_type: {doc_type}")


# ── Artifact upload (Generate-or-Upload) ──────────────────────────────────────

def _next_artifact_version(model, change_id: str, db, *, doc_type_enum=None) -> int:
    q = db.query(model).filter(model.change_request_id == change_id)
    if doc_type_enum is not None:
        q = q.filter(model.doc_type == doc_type_enum)
    latest = q.order_by(model.version.desc()).first()
    return (latest.version + 1) if latest else 1


# Documents that support Generate-or-Upload (and revert): BRD, Tech Spec, and
# the Product Kit's Product Note ("Product Document") + Circular.
_UPLOADABLE_PRODUCT_KIT_SUBTYPES = {"product_note", "circular"}

# Max accepted upload size. Keep nginx `client_max_body_size` aligned with this
# (the prod nginx.conf / nginx.dev.conf should allow at least this much).
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB


def _resolve_uploadable(doc_type: str, subtype: str | None):
    """Return (model, builder_label, draft_status, dt_enum) for an uploadable
    artifact, or raise HTTPException if the doc_type/subtype isn't allowed."""
    from app.models.brd import BRD, BRDStatus
    from app.models.tech_spec import TechSpec
    from app.models.product_kit import ProductKitDocument, ProductKitDocType
    from app.models.research import ArtifactStatus

    doc_type_norm = (doc_type or "").lower().replace("-", "_")
    if doc_type_norm == "brd":
        return BRD, "BRD", BRDStatus.DRAFT, None
    if doc_type_norm == "tech_spec":
        return TechSpec, "Technical Specification", ArtifactStatus.DRAFT, None
    if doc_type_norm == "product_kit" and (subtype or "") in _UPLOADABLE_PRODUCT_KIT_SUBTYPES:
        try:
            dt_enum = ProductKitDocType(subtype)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown product-kit subtype: {subtype}")
        return ProductKitDocument, "Product Kit", ArtifactStatus.DRAFT, dt_enum
    raise HTTPException(
        status_code=400,
        detail="Only supported for BRD, Tech Spec, Product Document, and Circular.",
    )


@router.post("/{change_id}/artifacts/{doc_type}/upload")
async def upload_artifact(
    change_id: str, doc_type: str, db: DbDep, current_user: CurrentUser,
    file: UploadFile = File(...),
    subtype: str | None = Form(None),
):
    """Upload a document in place of generating it.

    The file is parsed to text and stored as a NEW highest-version row in the
    matching artifact table with source=UPLOADED. Because every downstream
    consumer reads the latest version's `content`, the upload transparently
    substitutes the generated document everywhere (TSD gen, Product Kit gen,
    Phase-B codegen, UAT test gen). The original file is kept on disk for
    fidelity; an uploaded BRD still flows through the normal approval gate
    (status=DRAFT — the existing "Submit for approval" path is unchanged).
    """
    from app.models.user import UserRole
    from app.models.document_source import DocumentSource
    from app.services.text_extraction import extract_full_text, ALLOWED_UPLOAD_EXTENSIONS
    from app.core.config import settings

    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")

    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext or '(none)'}. "
                   f"Allowed: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}",
        )

    # Size guard. Reject early on the declared size when present, and cap the
    # actual read at the limit + 1 byte so an oversized payload can never be
    # pulled fully into memory before we bail.
    declared = getattr(file, "size", None)
    if declared is not None and declared > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        text_content = extract_full_text(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not text_content.strip():
        raise HTTPException(status_code=400, detail="Could not extract any text from the uploaded file.")

    doc_type_norm = (doc_type or "").lower().replace("-", "_")
    model, builder_label, draft_status, dt_enum = _resolve_uploadable(doc_type_norm, subtype)

    next_ver = _next_artifact_version(model, change_id, db, doc_type_enum=dt_enum)

    # Persist the original file alongside other session artifacts.
    base = Path(settings.artifacts_dir) / "sessions" / change_id
    base.mkdir(parents=True, exist_ok=True)
    slug = builder_label.lower().replace(" ", "_")
    # Build the filename's subtype component from the VALIDATED enum, never the
    # raw `subtype` form value — otherwise a value like "../../.." escapes the
    # session dir and becomes an arbitrary-write primitive. dt_enum is None for
    # BRD/Tech Spec (subtype is meaningless there) and a known ProductKitDocType
    # literal for product_kit.
    sub = f"_{dt_enum.value}" if dt_enum is not None else ""
    saved = base / f"uploaded_{slug}{sub}_v{next_ver}{ext}"
    try:
        saved.write_bytes(raw)
    except Exception as e:
        logger.warning("Failed to persist uploaded original for change=%s %s: %s", change_id, doc_type_norm, e)
        saved = None

    row = model(
        change_request_id=change_id,
        content=text_content,
        version=next_ver,
        status=draft_status,
        source=DocumentSource.UPLOADED,
        original_filename=filename,
        uploaded_by=current_user.id,
        uploaded_at=datetime.now(timezone.utc),
    )
    if saved is not None:
        row.file_path = str(saved)
    if dt_enum is not None:
        row.doc_type = dt_enum
    db.add(row)
    db.commit()
    db.refresh(row)

    # Async reconciliation gate (BRD only for now): check the upload against the
    # ratified plan OFF the request path (Celery). Never blocks the upload; the
    # task short-circuits when there is no ratified plan. TSD is a later switch-on.
    if doc_type_norm in ("brd", "tech_spec"):
        try:
            from app.services.celery_tasks import reconcile_upload_task
            reconcile_upload_task.delay(change_id, doc_type_norm, row.id, row.version)
        except Exception as e:  # noqa: BLE001 — enqueue must never fail the upload
            logger.warning("reconcile enqueue failed for change=%s: %s", change_id, e)

    logger.info("Artifact uploaded: change=%s doc_type=%s subtype=%s version=%s file=%s",
                change_id, doc_type_norm, subtype, row.version, filename)

    return {
        "id":                row.id,
        "doc_type":          doc_type_norm,
        "subtype":           subtype,
        "version":           row.version,
        "source":            row.source.value,
        "status":            row.status.value,
        "original_filename": row.original_filename,
        "uploaded_at":       row.uploaded_at.isoformat() if row.uploaded_at else None,
    }


@router.post("/{change_id}/source-document")
async def upload_source_document(
    change_id: str, db: DbDep, current_user: CurrentUser,
    file: UploadFile = File(...),
):
    """Attach a detailed SOURCE document (e.g. an existing BRD) at change creation.

    This is SEED material, not a generated-artifact substitute (that is the
    Generate-or-Upload endpoint above): every Phase A stage still runs, but the
    enhancer/research/canvas/BRD prompts receive this document's text so the
    pipeline starts from the PM's facts instead of assuming. One document per
    change; re-upload replaces it. Stored as extracted text on the change row
    (services.source_material renders it into prompts, wrapped + bounded)."""
    from app.services.text_extraction import extract_full_text, ALLOWED_UPLOAD_EXTENSIONS
    from app.services.source_material import SOURCE_DOC_MAX_CHARS
    from app.services.image_understanding import (
        describe_document_images, figures_block, _IMAGE_UPLOAD_EXTENSIONS,
    )
    from app.models.user import UserRole
    from app.core.config import settings

    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")

    allowed = ALLOWED_UPLOAD_EXTENSIONS | _IMAGE_UPLOAD_EXTENSIONS
    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext or '(none)'}. "
                   f"Allowed: {', '.join(sorted(allowed))}",
        )
    declared = getattr(file, "size", None)
    if declared is not None and declared > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        # Text first (docx/pdf/md/txt; scanned PDFs go through the OCR fallback inside
        # chunking). A standalone image has no text layer — vision carries it entirely.
        text_content = extract_full_text(tmp_path) if ext in ALLOWED_UPLOAD_EXTENSIONS else ""
        # Vision pass: describe embedded figures (diagrams/screenshots/tables) that text
        # extraction cannot see. Fail-open — a vision outage never blocks the upload.
        try:
            captions = await describe_document_images(tmp_path, ext)
        except Exception as e:  # noqa: BLE001
            logger.warning("vision pass failed for change=%s file=%s: %s", change_id, filename, e)
            captions = []
        text_content = (text_content + figures_block(captions)).strip()
    finally:
        tmp_path.unlink(missing_ok=True)
    if not text_content:
        raise HTTPException(status_code=400,
                            detail="Could not extract any text or describable figures from the uploaded file.")

    # Keep the original for fidelity/audit, same location scheme as artifact uploads.
    base = Path(settings.artifacts_dir) / "sessions" / change_id
    try:
        base.mkdir(parents=True, exist_ok=True)
        # Re-upload replaces the document; a cross-extension replace (.pdf → .docx)
        # would otherwise leave the old original stranded next to the new one.
        for stale in base.glob("source_document.*"):
            if stale.suffix.lower() != ext:
                stale.unlink(missing_ok=True)
        (base / f"source_document{ext}").write_bytes(raw)
    except Exception as e:  # noqa: BLE001 — best-effort; the extracted text is what the pipeline uses
        logger.warning("Failed to persist source-document original for change=%s: %s", change_id, e)

    change.source_doc_name = filename[:500]
    change.source_doc_text = text_content
    # Re-title from the DOCUMENT: it is the richer source, and on a document-only
    # change the initial_prompt is just a filename stub ("Implement the change
    # described in the attached document: X.pdf") — titling from that says nothing.
    # Value-neutral by construction (see generate_change_title). Fail-open: the
    # creation-time title stays.
    try:
        from app.agents.prompt_enhancer import generate_change_title
        _title = await generate_change_title(text_content, fallback=change.initial_prompt)
        if _title and _title != (change.title or ""):
            logger.info("source-document: title regenerated change=%s %r → %r",
                        change_id, change.title, _title)
            change.title = _title
    except Exception as e:  # noqa: BLE001 — never block the upload on titling
        logger.warning("source-document: title regeneration failed for change=%s: %s", change_id, e)
    db.commit()

    truncated = len(text_content) > SOURCE_DOC_MAX_CHARS
    logger.info("Source document attached: change=%s file=%s chars=%d figures=%d truncated_in_prompts=%s",
                change_id, filename, len(text_content), len(captions), truncated)
    return {
        "source_doc_name": change.source_doc_name,
        "chars": len(text_content),
        "figures_described": len(captions),
        # surfaced so the UI can warn when a very large doc won't be fully visible to the agents
        "truncated_in_prompts": truncated,
        "prompt_char_cap": SOURCE_DOC_MAX_CHARS,
    }


@router.post("/{change_id}/artifacts/{doc_type}/revert-to-generated")
def revert_to_generated(
    change_id: str, doc_type: str, db: DbDep, current_user: CurrentUser,
    subtype: str | None = None,
):
    """Revert an uploaded document back to the most recent GENERATED version.

    Copies the latest generated version's content into a new highest-version
    row (source=generated) so it becomes the active document everywhere
    downstream — undoing an "Upload instead" without re-running generation.
    """
    from app.models.user import UserRole
    from app.models.document_source import DocumentSource

    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")

    doc_type_norm = (doc_type or "").lower().replace("-", "_")
    model, _builder_label, draft_status, dt_enum = _resolve_uploadable(doc_type_norm, subtype)

    q = db.query(model).filter(
        model.change_request_id == change_id,
        model.source == DocumentSource.GENERATED,
    )
    if dt_enum is not None:
        q = q.filter(model.doc_type == dt_enum)
    latest_gen = q.order_by(model.version.desc()).first()
    if not latest_gen or not (latest_gen.content or "").strip():
        raise HTTPException(status_code=404, detail="No previously generated version to revert to.")

    next_ver = _next_artifact_version(model, change_id, db, doc_type_enum=dt_enum)
    row = model(
        change_request_id=change_id,
        content=latest_gen.content,
        version=next_ver,
        status=draft_status,
        source=DocumentSource.GENERATED,
    )
    if dt_enum is not None:
        row.doc_type = dt_enum
    db.add(row)
    # G1: the uploaded doc is gone — clear any OPEN reconciliation so its stale
    # conflicts don't keep the downstream gate stuck.
    from app.agents.upload_reconciler import supersede_open_reconciliations
    supersede_open_reconciliations(db, change_id, doc_type_norm)
    db.commit()
    db.refresh(row)

    logger.info("Artifact reverted to generated: change=%s doc_type=%s subtype=%s version=%s",
                change_id, doc_type_norm, subtype, row.version)
    return {
        "id": row.id, "doc_type": doc_type_norm, "subtype": subtype,
        "version": row.version, "source": row.source.value, "status": row.status.value,
    }


# ── Ship-time overrides (migration 0115) ────────────────────────────────────
# When the PM wants to substitute a hand-authored file for the generated
# artifact on the next kit ship, they upload it via PUT ship-override. The row's
# override_path being set is the sole signal to `build_kit_envelope` to swap the
# generated attachment (docx/pptx/mp4/xlsx) for the uploaded bytes.
_SHIP_OVERRIDE_MAX_BYTES = 25 * 1024 * 1024


def _resolve_ship_override_row(db, change_id: str, doc_type: str):
    """Return (row, canonical_doc_type_str) for the latest-version row that owns
    the override slot. Bare rows are created for doc_types that have never been
    generated so the PM can attach an override up-front."""
    from app.models.product_kit import (
        ProductKitDocument, ProductKitDocType, active_doc_types,
    )
    from app.models.tech_spec import TechSpec
    from app.models.xsd import XSD

    dt = (doc_type or "").lower().replace("-", "_")
    if dt in ("tsd", "tech_spec"):
        row = (db.query(TechSpec)
               .filter(TechSpec.change_request_id == change_id)
               .order_by(TechSpec.version.desc()).first())
        if row is None:
            row = TechSpec(change_request_id=change_id, version=1)
            db.add(row); db.flush()
        return row, "tsd"
    if dt == "xsd":
        row = (db.query(XSD)
               .filter(XSD.change_request_id == change_id)
               .order_by(XSD.version.desc()).first())
        if row is None:
            row = XSD(change_request_id=change_id, version=1)
            db.add(row); db.flush()
        return row, "xsd"
    if dt in active_doc_types():
        try:
            enum_val = ProductKitDocType(dt)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown doc_type: {dt}")
        row = (db.query(ProductKitDocument)
               .filter(ProductKitDocument.change_request_id == change_id,
                       ProductKitDocument.doc_type == enum_val)
               .order_by(ProductKitDocument.version.desc()).first())
        if row is None:
            row = ProductKitDocument(
                change_request_id=change_id, doc_type=enum_val, version=1,
            )
            db.add(row); db.flush()
        return row, dt
    raise HTTPException(
        status_code=400,
        detail=f"doc_type {dt!r} is not ship-eligible.",
    )


def _override_extension_for_mime(mime: str) -> str:
    """Deterministic extension from the uploaded MIME sniff. The persisted path
    is built from the validated doc_type + this ext + a sha prefix — never from
    file.filename (defeats path-traversal via a crafted upload name)."""
    m = (mime or "").lower()
    if "wordprocessingml" in m: return ".docx"
    if "presentationml"  in m: return ".pptx"
    if "spreadsheetml"   in m: return ".xlsx"
    if m == "application/pdf": return ".pdf"
    if m == "application/zip": return ".zip"
    if m == "video/mp4":       return ".mp4"
    if m in ("application/xml", "text/xml"): return ".xml"
    if m == "text/plain":      return ".txt"
    if m == "text/markdown":   return ".md"
    return ".bin"


@router.put("/{change_id}/artifacts/{doc_type}/ship-override")
async def upload_ship_override(
    change_id: str, doc_type: str, db: DbDep, current_user: CurrentUser,
    file: UploadFile = File(...),
):
    """Upload a file that will substitute the generated artifact for this
    doc_type on the next kit ship. Re-upload replaces the earlier override."""
    import hashlib
    from datetime import datetime as _dt, timezone as _tz
    from app.core.config import settings
    from app.models.user import UserRole

    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")

    raw = await file.read(_SHIP_OVERRIDE_MAX_BYTES + 1)
    if len(raw) > _SHIP_OVERRIDE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {_SHIP_OVERRIDE_MAX_BYTES // (1024 * 1024)} MB).",
        )
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")

    row, dt = _resolve_ship_override_row(db, change_id, doc_type)

    mime = (file.content_type or "application/octet-stream")
    ext = _override_extension_for_mime(mime)
    sha = hashlib.sha256(raw).hexdigest()
    base = Path(settings.artifacts_dir) / "sessions" / change_id
    base.mkdir(parents=True, exist_ok=True)
    saved = base / f"ship_override_{dt}_{sha[:12]}{ext}"
    saved.write_bytes(raw)

    prev_path = row.override_path
    row.override_path = str(saved)
    row.override_filename = Path(file.filename or f"{dt}{ext}").name[:250]
    row.override_sha256 = sha
    row.override_size_bytes = len(raw)
    row.override_mime_type = mime[:120]
    row.override_uploaded_at = _dt.now(_tz.utc)
    row.override_uploaded_by = current_user.id
    db.commit()

    if prev_path and prev_path != str(saved):
        try:
            Path(prev_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Stale ship-override unlink failed for change=%s: %s", change_id, exc)

    logger.info(
        "Ship override uploaded: change=%s doc_type=%s bytes=%d mime=%s by=%s",
        change_id, dt, len(raw), mime, current_user.id,
    )
    return {
        "doc_type":            dt,
        "override_filename":   row.override_filename,
        "override_size_bytes": row.override_size_bytes,
        "override_mime_type":  row.override_mime_type,
        "override_sha256":     row.override_sha256,
        "override_uploaded_at": row.override_uploaded_at.isoformat(),
    }


@router.delete("/{change_id}/artifacts/{doc_type}/ship-override")
def clear_ship_override(
    change_id: str, doc_type: str, db: DbDep, current_user: CurrentUser,
):
    """Clear the ship-override for a doc_type — the next ship reverts to the
    generated artifact for that item."""
    from app.models.user import UserRole
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")

    row, dt = _resolve_ship_override_row(db, change_id, doc_type)
    prev = row.override_path
    row.override_path = None
    row.override_filename = None
    row.override_sha256 = None
    row.override_size_bytes = None
    row.override_mime_type = None
    row.override_uploaded_at = None
    row.override_uploaded_by = None
    db.commit()
    if prev:
        try:
            Path(prev).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Stale ship-override unlink failed for change=%s: %s", change_id, exc)
    return {"ok": True, "doc_type": dt}


@router.get("/{change_id}/artifacts/ship-manifest")
def get_ship_manifest(change_id: str, db: DbDep, current_user: CurrentUser):
    """Per-item ship status for the Phase C "Communicate Change" modal.
    Returns one entry per ship-eligible doc_type with its generated + override
    presence — the modal renders the checkbox list and upload chips from this."""
    from app.models.user import UserRole
    from app.models.product_kit import active_doc_types
    from app.models.tech_spec import TechSpec
    from app.models.xsd import XSD
    from app.models.agent_job import AgentJob, AgentJobStatus
    from app.services.product_kit_query import latest_kit_docs

    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")

    def _iso(ts):
        return ts.isoformat() if ts else None

    def _override_dict(row):
        if not row or not row.override_path:
            return None
        return {
            "filename":    row.override_filename,
            "size_bytes":  row.override_size_bytes,
            "mime_type":   row.override_mime_type,
            "sha256":      row.override_sha256,
            "uploaded_at": _iso(row.override_uploaded_at),
        }

    pk_rows: dict[str, "ProductKitDocument"] = {}
    for r in latest_kit_docs(db, change_id):
        key = r.doc_type.value if hasattr(r.doc_type, "value") else r.doc_type
        pk_rows[key] = r

    # cert_test_cases xlsx also comes from the AgentJob path — mirror the same
    # lookup the envelope does so the manifest reports "generated" when only
    # the xlsx exists (row.content may be empty for a fresh engine run).
    cert_xlsx_exists = False
    try:
        cert_job = (
            db.query(AgentJob)
            .filter(
                AgentJob.change_request_id == change_id,
                AgentJob.module == "product_kit",
                AgentJob.subtype == "cert_test_cases",
                AgentJob.status == AgentJobStatus.SUCCEEDED,
            )
            .order_by(AgentJob.completed_at.desc().nullslast(), AgentJob.updated_at.desc())
            .first()
        )
        if cert_job is not None:
            rp = cert_job.result_payload or {}
            files = rp.get("files") or {}
            xlsx_path = files.get("xlsx") or rp.get("xlsx_path")
            cert_xlsx_exists = bool(xlsx_path and os.path.exists(xlsx_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ship-manifest cert xlsx lookup failed: %s", exc)

    items: list[dict] = []
    for dt in sorted(active_doc_types()):
        row = pk_rows.get(dt)
        generated = None
        if row is not None:
            has_content = bool(row.content and row.content.strip())
            has_docx = bool(row.docx_path and os.path.exists(row.docx_path))
            has_pptx = bool(getattr(row, "pptx_path", None) and os.path.exists(row.pptx_path))
            has_video = bool(getattr(row, "file_path", None) and os.path.exists(row.file_path)) \
                if dt in ("promo_video", "explainer_video") else False
            if has_content or has_docx or has_pptx or has_video:
                generated = {"version": row.version}
        if dt == "cert_test_cases" and generated is None and cert_xlsx_exists:
            generated = {"version": (row.version if row else 1)}
        items.append({"doc_type": dt, "generated": generated, "override": _override_dict(row)})

    tsd_row = (db.query(TechSpec)
               .filter(TechSpec.change_request_id == change_id)
               .order_by(TechSpec.version.desc()).first())
    tsd_gen = {"version": tsd_row.version} if (tsd_row and (tsd_row.content or "").strip()) else None
    items.append({"doc_type": "tsd", "generated": tsd_gen, "override": _override_dict(tsd_row)})

    xsd_row = (db.query(XSD)
               .filter(XSD.change_request_id == change_id)
               .order_by(XSD.version.desc()).first())
    xsd_gen = {"version": xsd_row.version} if (xsd_row and (xsd_row.content or "").strip()) else None
    items.append({"doc_type": "xsd", "generated": xsd_gen, "override": _override_dict(xsd_row)})

    return {"change_id": change_id, "items": items}


@router.get("/{change_id}/artifacts/staleness")
def artifact_staleness(change_id: str, db: DbDep, current_user: CurrentUser):
    """Non-blocking signal: which downstream artifacts predate their upstream.

    When a user re-uploads (or regenerates) a BRD after the TSD/Product Kit were
    already produced from the old version, those downstream docs are out of date.
    We surface a per-stage boolean so the UI can show a "source changed —
    regenerate recommended" badge. Nothing is blocked. Comparison uses
    `created_at` because each version is a fresh row.
    """
    from app.models.user import UserRole
    from app.models.brd import BRD
    from app.models.tech_spec import TechSpec
    from app.models.xsd import XSD
    from app.models.product_kit import ProductKitDocument, ProductKitDocType

    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")

    def _latest_ts(model, *, doc_type_enum=None):
        q = db.query(model).filter(model.change_request_id == change_id)
        if doc_type_enum is not None:
            q = q.filter(model.doc_type == doc_type_enum)
        row = q.order_by(model.version.desc()).first()
        return row.created_at if row else None

    def _stale(target_ts, upstream_tss) -> bool:
        ups = [t for t in upstream_tss if t]
        if not target_ts or not ups:
            return False
        return max(ups) > target_ts

    brd_ts = _latest_ts(BRD)
    tsd_ts = _latest_ts(TechSpec)
    xsd_ts = _latest_ts(XSD)

    product_kit = {
        dt.value: _stale(_latest_ts(ProductKitDocument, doc_type_enum=dt), [brd_ts, tsd_ts, xsd_ts])
        for dt in ProductKitDocType
    }

    # Dependency map flips with the stage order (accuracy S5). v2 (default): BRD→XSD→TSD,
    # so XSD←[BRD], TSD←[BRD,XSD]. Legacy v1: BRD→TSD→XSD, so TSD←[BRD], XSD←[BRD,TSD].
    if (getattr(change, "workflow_version", 2) or 2) >= 2:
        tech_spec_stale = _stale(tsd_ts, [brd_ts, xsd_ts])
        xsd_stale = _stale(xsd_ts, [brd_ts])
    else:
        tech_spec_stale = _stale(tsd_ts, [brd_ts])
        xsd_stale = _stale(xsd_ts, [brd_ts, tsd_ts])

    return {
        "tech_spec":   tech_spec_stale,
        "xsd":         xsd_stale,
        "product_kit": product_kit,
    }


# ── Uploaded-doc ↔ ratified-plan reconciliation ───────────────────────────────
class ReconciliationDecisionRequest(BaseModel):
    resolutions: dict


def _latest_pending_reconciliation(db, change_id: str, doc_kind: str = "brd"):
    from app.models.document_reconciliation import DocumentReconciliation
    return (db.query(DocumentReconciliation)
            .filter(DocumentReconciliation.change_request_id == change_id,
                    DocumentReconciliation.doc_kind == doc_kind,
                    DocumentReconciliation.status == "pending")
            .order_by(DocumentReconciliation.created_at.desc()).first())


def _latest_open_reconciliation(db, change_id: str, doc_kind: str = "brd"):
    """The latest reconciliation the user still needs to see — ``pending`` (conflicts
    to resolve) or ``applying`` (resolved, doc regenerating). Drives the UI so the
    document isn't shown as final while either is true."""
    from app.models.document_reconciliation import DocumentReconciliation
    return (db.query(DocumentReconciliation)
            .filter(DocumentReconciliation.change_request_id == change_id,
                    DocumentReconciliation.doc_kind == doc_kind,
                    DocumentReconciliation.status.in_(("checking", "pending", "applying")))
            .order_by(DocumentReconciliation.created_at.desc()).first())


def _grounding_summary(db, change_id: str, doc_kind: str) -> dict | None:
    """The code-check findings worth surfacing on a JUST-RESOLVED reconciliation, before
    approval folds them into the plan: only deltas with a risk, a question, or an
    overturns-ratified flag. None when there's nothing to flag (S3)."""
    from app.models.document_reconciliation import DocumentReconciliation
    # 'resolved' = BRD awaiting approval · 'applied' = TSD folded in-task (no approval
    # moment exists for TSD, so its findings surface post-fold — still answerable).
    r = (db.query(DocumentReconciliation)
         .filter(DocumentReconciliation.change_request_id == change_id,
                 DocumentReconciliation.doc_kind == doc_kind,
                 DocumentReconciliation.status.in_(("resolved", "applied")))
         .order_by(DocumentReconciliation.created_at.desc()).first())
    g = (r.grounding if r else None) or {}
    if g.get("status") != "ok":
        return None
    findings = [d for d in (g.get("deltas") or [])
                if d.get("risk", "none") != "none" or (d.get("question") or "").strip()
                or d.get("overturns_ratified")]
    if not findings:
        return None
    return {
        "id": r.id,
        "overturns": any(d.get("overturns_ratified") for d in findings),
        "acknowledged": bool(g.get("overturns_acked")),
        "deltas": [{"directive": d.get("directive"), "impact": d.get("impact"),
                    "risk": d.get("risk", "none"), "risk_note": d.get("risk_note"),
                    "overturns_ratified": bool(d.get("overturns_ratified")),
                    "question": d.get("question")} for d in findings],
    }


@router.get("/{change_id}/reconciliation")
def get_reconciliation(change_id: str, db: DbDep, current_user: CurrentUser, doc_kind: str = "brd"):
    """Latest pending reconciliation — the conflicts between an uploaded doc and the
    ratified plan (each carries options + a free-text choice). {exists: false} when
    there is nothing to resolve."""
    from app.models.user import UserRole
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")
    recon = _latest_open_reconciliation(db, change_id, doc_kind)
    if recon is None:
        # No open row — but a just-resolved reconciliation may carry code-check findings
        # (risk / question / overturns-ratified) worth surfacing before approval. exists
        # stays False so the doc isn't re-hidden or re-gated; the panel renders the
        # advisory card off `grounding_summary` (S3).
        gs = _grounding_summary(db, change_id, doc_kind)
        return {"exists": False, **({"grounding_summary": gs} if gs else {})}
    # Non-conflict progress states — the UI shows a loader and keeps polling until the
    # row advances. 'checking' = the detection axes are still running (right after
    # upload); 'applying' = resolved, the corrected doc is regenerating.
    if recon.status == "checking":
        return {"exists": True, "id": recon.id, "doc_kind": recon.doc_kind,
                "status": "checking", "checking": True, "conflicts": []}
    if recon.status == "applying":
        return {"exists": True, "id": recon.id, "doc_kind": recon.doc_kind,
                "status": "applying", "regenerating": True, "conflicts": []}
    # Feasibility verdicts (red = can't be built · warn = needs new wire build the plan
    # never scoped) are validated against the real repo checkout at reconcile time and
    # STORED on the conflicts. Live-compute (index-only, never clones on the request
    # path) only for rows created before that change.
    conflicts = recon.conflicts or []
    if not any(c.get("feasibility_checked") for c in conflicts):
        from app.agents.upload_reconciler import assess_feasibility
        feas = assess_feasibility(db, change_id, conflicts)
        conflicts = [{**c,
                      "red_options": (feas.get(c.get("id")) or {}).get("red", []),
                      "warn_options": (feas.get(c.get("id")) or {}).get("warn", []),
                      "feasibility_reason": (feas.get(c.get("id")) or {}).get("reason")}
                     for c in conflicts]
    return {
        "exists": True, "id": recon.id, "doc_kind": recon.doc_kind,
        "doc_id": recon.doc_id, "doc_version": recon.doc_version,
        "plan_version_before": recon.plan_version_before,
        "status": recon.status, "regenerating": False, "conflicts": conflicts,
    }


@router.post("/{change_id}/reconciliation/decide")
def decide_reconciliation(change_id: str, body: ReconciliationDecisionRequest,
                          db: DbDep, current_user: CurrentUser, doc_kind: str = "brd"):
    """Record the user's per-conflict resolutions and mark the reconciliation
    resolved (which unblocks downstream generation). Plan/BRD re-versioning from
    these decisions is applied in a later step (at/before BRD approval)."""
    from app.models.user import UserRole
    from app.agents.upload_reconciler import validate_resolutions
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")
    recon = _latest_pending_reconciliation(db, change_id, doc_kind)
    if recon is None:
        raise HTTPException(status_code=404, detail="No pending reconciliation to resolve")
    ok, err = validate_resolutions(recon.conflicts or [], body.resolutions or {})
    if not ok:
        raise HTTPException(status_code=422, detail=err)
    recon.resolutions = body.resolutions
    # Background work in the 'applying' phase covers BOTH directions:
    #   plan-wins / custom → correct (regenerate) the uploaded doc
    #   brd-wins  / custom → code-ground the deltas that will amend the plan
    # Hold 'applying' (doc not final; approval gated; UI shows progress) until the task
    # flips it to 'resolved'. Nothing to do at all → 'resolved' immediately.
    _resos = list((body.resolutions or {}).values())
    needs_correction = any(
        (r or {}).get("chosen_option_id") == "plan_wins" or ((r or {}).get("custom_answer") or "").strip()
        for r in _resos)
    has_deltas = any(
        (r or {}).get("chosen_option_id") == "brd_wins" or ((r or {}).get("custom_answer") or "").strip()
        for r in _resos)
    recon.status = "applying" if (needs_correction or has_deltas) else "resolved"
    db.commit()
    logger.info("Reconciliation %s: change=%s recon=%s conflicts=%d",
                recon.status, change_id, recon.id, len(recon.conflicts or []))

    # G3: land the plan-changing resolutions (brd_wins / custom) in the Decision
    # Ledger so downstream agents' binding DECISIONS block reflects them — not just
    # the JSON on the reconciliation row. plan_wins affirms the existing plan, so it
    # needs no new binding directive.
    try:
        from app.services.decision_ledger import append_entry
        by_id = {c.get("id"): c for c in (recon.conflicts or [])}
        for cid, r in (body.resolutions or {}).items():
            c = by_id.get(cid)
            if not c:
                continue
            chosen_id = (r or {}).get("chosen_option_id")
            custom = ((r or {}).get("custom_answer") or "").strip()
            text = (c.get("text") or "")[:200]
            if custom:
                directive, chosen = f"Reviewer ruling on “{text}”: {custom}", "custom"
            elif chosen_id == "brd_wins":
                directive, chosen = f"The uploaded {doc_kind.upper()} is authoritative here — {text}", "brd_wins"
            else:
                continue
            subject = (c.get("evidence") or {}).get("item") or text
            append_entry(db, change_id, question_key=f"reconcile::{doc_kind}::{subject}"[:128],
                         kind="upload_reconciliation", question=c.get("text"),
                         options=c.get("options"), chosen=chosen, directive=directive,
                         evidence=[c.get("evidence")], decided_by=current_user.id)
    except Exception as e:  # noqa: BLE001 — advisory; the resolution already stands
        logger.warning("reconciliation ledger append failed for %s: %s", change_id, e)

    # Regenerate the uploaded doc in the background for every plan-wins / custom
    # resolution (find/replace to the plan or the ruling; omissions added back) → new
    # doc version. The task flips 'applying' → 'resolved' when done. If the enqueue
    # itself fails, don't strand the row in 'applying' — fall back to 'resolved' so the
    # user isn't blocked (the doc just isn't auto-corrected).
    enqueued = False
    if needs_correction or has_deltas:
        try:
            from app.services.celery_tasks import apply_corrections_task
            apply_corrections_task.delay(change_id, recon.id)
            enqueued = True
        except Exception as e:  # noqa: BLE001 — enqueue must not fail the resolution
            logger.warning("apply-corrections enqueue failed for %s: %s", change_id, e)
            recon.status = "resolved"
            db.commit()

    # TSD has no BRD-approval deferral point → its deltas fold into the plan at the END
    # of the background task, AFTER delta grounding, so the new plan version merges the
    # code-backed grounding (folding here would fold ungrounded). This sync fold remains
    # ONLY as the fallback when the task could not be enqueued.
    if doc_kind == "tech_spec" and not enqueued:
        try:
            from app.agents.plan_versioning import record_reconciliation_version
            record_reconciliation_version(db, change_request_id=change_id,
                                          reconciliation=recon, decided_by=current_user.id)
            db.commit()
        except Exception as e:  # noqa: BLE001 — best-effort; the resolution already stands
            logger.warning("TSD reconciliation plan-version failed for %s: %s", change_id, e)

    return {"id": recon.id, "status": recon.status, "resolved_count": len(recon.conflicts or [])}


@router.post("/{change_id}/reconciliation/dismiss")
def dismiss_reconciliation(change_id: str, db: DbDep, current_user: CurrentUser, doc_kind: str = "brd"):
    """Explicitly withdraw the pending reconciliation without resolving it — an
    escape hatch that unblocks downstream (e.g. the conflicts are spurious and the
    user will handle the doc another way). Supersedes open rows for this doc kind."""
    from app.models.user import UserRole
    from app.agents.upload_reconciler import supersede_open_reconciliations
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")
    n = supersede_open_reconciliations(db, change_id, doc_kind)
    db.commit()
    logger.info("Reconciliation dismissed: change=%s doc_kind=%s superseded=%d", change_id, doc_kind, n)
    return {"dismissed": n}


class GroundingAnswerRequest(BaseModel):
    index: int
    question: str
    answer: str


@router.post("/{change_id}/reconciliation/acknowledge-overturns")
def acknowledge_overturns(change_id: str, db: DbDep, current_user: CurrentUser, doc_kind: str = "brd"):
    """Acknowledge that an accepted change overturns a ratified plan decision — clears the
    soft gate on approval (§8.1). Records the ack on the grounding JSON of the latest
    resolved/applied reconciliation."""
    from app.models.user import UserRole
    from app.models.document_reconciliation import DocumentReconciliation
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")
    r = (db.query(DocumentReconciliation)
         .filter(DocumentReconciliation.change_request_id == change_id,
                 DocumentReconciliation.doc_kind == doc_kind,
                 DocumentReconciliation.status.in_(("resolved", "applied")))
         .order_by(DocumentReconciliation.created_at.desc()).first())
    if r and r.grounding:
        r.grounding = {**r.grounding, "overturns_acked": True}   # reassign so SQLAlchemy flags dirty
        db.commit()
    return {"acknowledged": True}


@router.post("/{change_id}/reconciliation/grounding-answer")
def answer_grounding(change_id: str, body: GroundingAnswerRequest, db: DbDep,
                     current_user: CurrentUser, doc_kind: str = "brd"):
    """Record the user's answer to a delta-grounding question in the Decision Ledger, so
    the binding directive rides into the downstream agents (S3). Advisory — never gates."""
    from app.models.user import UserRole
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this change request")
    answer = (body.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=422, detail="Answer cannot be empty")
    try:
        from app.services.decision_ledger import append_entry
        # Key on the QUESTION, not the filtered-list index — the index shifts if the
        # findings are re-grounded, which would supersede the wrong answer (gap #4).
        subject = (body.question or "").strip() or f"delta-{body.index}"
        append_entry(db, change_id,
                     question_key=f"grounding::{doc_kind}::{subject}"[:128],
                     kind="delta_grounding", question=(body.question or "")[:400],
                     chosen="answered", directive=answer[:600], decided_by=current_user.id)
        db.commit()
    except Exception as e:  # noqa: BLE001 — advisory
        logger.warning("grounding-answer append failed for %s: %s", change_id, e)
        db.rollback()
    return {"ok": True}


@router.get("/{change_id}/artifacts/summary")
def artifact_summary(change_id: str, db: DbDep, current_user: CurrentUser):
    """List the documents that exist for this change and can serve as
    downstream context, with their provenance. Powers the "Show Artifacts"
    panel — a document is "in context" when it is present.
    """
    from app.models.user import UserRole
    from app.models.research import ResearchOutput
    from app.models.canvas import ProductCanvas
    from app.models.brd import BRD
    from app.models.tech_spec import TechSpec
    from app.models.xsd import XSD
    from app.models.product_kit import ProductKitDocument, ProductKitDocType

    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if not _is_creator_or_admin(current_user, change):
        raise HTTPException(status_code=403, detail="Not authorized for this change request")

    def _latest(model, *, doc_type_enum=None):
        q = db.query(model).filter(model.change_request_id == change_id)
        if doc_type_enum is not None:
            q = q.filter(model.doc_type == doc_type_enum)
        return q.order_by(model.version.desc()).first()

    def _entry(key, label, row):
        body = getattr(row, "content", None) or getattr(row, "combined_report", None) if row else None
        present = bool(body)
        src = getattr(row, "source", None) if row else None
        return {
            "key": key, "label": label, "present": present,
            "source": (src.value if hasattr(src, "value") else None) if present else None,
            "version": (row.version if row else 0),
        }

    items = [
        _entry("research",  "Research",      _latest(ResearchOutput)),
        _entry("canvas",    "Product Canvas", _latest(ProductCanvas)),
        _entry("brd",       "BRD",            _latest(BRD)),
        _entry("tech_spec", "Tech Spec",      _latest(TechSpec)),
        _entry("xsd",       "XSD",            _latest(XSD)),
    ]
    # Product Kit docs that participate as context — Product Document
    # (product_note) and Circular are the user-facing ones.
    pk_labels = {"product_note": "Product Document", "circular": "Circular"}
    for sub, label in pk_labels.items():
        items.append(_entry(f"product_kit:{sub}", label,
                            _latest(ProductKitDocument, doc_type_enum=ProductKitDocType(sub))))

    return {"artifacts": items}


def _ensure_docx(row, *, change_id: str, doc_type_label: str,
                 cr_title: str, subtype: str | None = None) -> str | None:
    """Return a usable DOCX path for the given row. Builds on demand if missing.

    Returns None if row has no content to build from.
    """
    if row is None:
        return None
    existing = row.docx_path
    if existing:
        p = Path(existing)
        if p.exists():
            return str(p)
    # Need to (re)build
    content = row.content
    if not content or not content.strip():
        return None
    try:
        from app.services.docx_assembler import build_docx_from_markdown, artifact_path
        out = artifact_path(change_id, doc_type_label, version=row.version, subtype=subtype)
        build_docx_from_markdown(
            content,
            title=cr_title or doc_type_label,
            subtitle=(subtype or doc_type_label) if subtype else (cr_title or ""),
            doc_type=doc_type_label,
            doc_subtype=subtype,
            version=str(row.version),
            output_path=out,
        )
        row.docx_path = str(out)
        return str(out)
    except Exception as e:
        logger.warning("On-demand DOCX build failed for change=%s %s: %s",
                       change_id, doc_type_label, e)
        return None


# ── DOCX download endpoint (self-healing) ────────────────────────────────────

@router.get("/{change_id}/artifacts/{doc_type}/download")
def download_artifact(
    change_id: str, doc_type: str, db: DbDep, current_user: CurrentUser,
    subtype: str | None = None,
):
    """Download the latest generated .docx. Builds on-demand if missing.

    Paths:
      /changes/{id}/artifacts/brd/download
      /changes/{id}/artifacts/tech_spec/download
      /changes/{id}/artifacts/xsd/download
      /changes/{id}/artifacts/product_kit/download?subtype=circular   (etc.)

    If `docx_path` is set and file exists → serve it.
    Else if content is stored → build DOCX now, save, update row, serve it.
    Else → 404.
    """
    from app.models.user import UserRole
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    # Review teams may download ONLY product-kit documents (product alone) —
    # never BRD/TSD/etc. Creator/admin keep full access.
    _kit_doc = (doc_type or "").lower().replace("-", "_") == "product_kit"
    if not _is_creator_or_admin(current_user, change) and not (_kit_doc and _can_read_all_changes(current_user)):
        # Approver path — non-creator PMs are allowed only when they have an
        # actual `approvals` row on the BRD of this change. Narrower than
        # role-based ("any PM can download any BRD") which would over-open.
        from app.models.approval import Approval
        from app.models.brd import BRD
        is_approver = (db.scalar(
            select(func.count(Approval.id)).where(
                Approval.approver_id == current_user.id,
                Approval.artifact_id.in_(
                    select(BRD.id).where(BRD.change_request_id == change_id)
                ),
            )
        ) or 0) > 0
        if not is_approver:
            raise HTTPException(status_code=403, detail="Access denied")
        logger.info(
            "Artifact download by approver: change=%s doc_type=%s subtype=%s "
            "user=%s (id=%s) role=%s",
            change_id, doc_type, subtype or "-",
            current_user.username, current_user.id,
            current_user.role.value if hasattr(current_user.role, "value") else current_user.role,
        )
    else:
        logger.info(
            "Artifact download: change=%s doc_type=%s subtype=%s "
            "user=%s (id=%s) role=%s",
            change_id, doc_type, subtype or "-",
            current_user.username, current_user.id,
            current_user.role.value if hasattr(current_user.role, "value") else current_user.role,
        )

    row, builder_doc_type, file_label = _resolve_artifact_row(change_id, doc_type, db, subtype)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No {doc_type} generated for this change request yet")

    cr_slug = (change.title or "change").lower().replace(" ", "_")[:40]

    # ── XSD short-circuit ────────────────────────────────────────────────
    # XSD content is markdown wrapping one-or-more ```xml schema blocks
    # (or a "NOT REQUIRED" assessment with no XML at all). Wrapping XML in
    # a .docx loses the value — operators expect to download .xsd files
    # they can drop into their schema validator. Branch out before the
    # DOCX assembler runs.
    if (doc_type or "").lower() == "xsd":
        content = (row.content or "").strip()
        if not content:
            raise HTTPException(status_code=404, detail="XSD has no content yet")
        blocks = _extract_xsd_blocks(content)

        if not blocks:
            # Assessment-only — no schemas were generated. Serve the
            # markdown verbatim so the operator at least gets the
            # rationale; flag the extension so they don't expect XML.
            fname = f"{cr_slug}_{file_label}_assessment.md"
            return Response(
                content=content,
                media_type=_MD_MIME,
                headers={"Content-Disposition": f'attachment; filename="{urlquote(fname)}"'},
            )

        if len(blocks) == 1:
            fname, body = blocks[0]
            # Prefix with the change slug for ops sanity when many partners
            # download files; preserve the agent's chosen schema filename.
            download_name = f"{cr_slug}_{fname}"
            return Response(
                content=body,
                media_type=_XSD_MIME,
                headers={"Content-Disposition": f'attachment; filename="{urlquote(download_name)}"'},
            )

        # Multiple schemas → bundle into a zip.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            seen: dict[str, int] = {}
            for fname, body in blocks:
                # Disambiguate if the agent re-used a filename across blocks.
                if fname in seen:
                    seen[fname] += 1
                    stem, _, ext = fname.rpartition(".")
                    fname = f"{stem}_{seen[fname]}.{ext}" if ext else f"{fname}_{seen[fname]}"
                else:
                    seen[fname] = 1
                zf.writestr(fname, body)
        buf.seek(0)
        zip_name = f"{cr_slug}_{file_label}.zip"
        return Response(
            content=buf.getvalue(),
            media_type=_ZIP_MIME,
            headers={"Content-Disposition": f'attachment; filename="{urlquote(zip_name)}"'},
        )

    # ── default path: DOCX assembly for BRD / TSD / Canvas / Product Kit ─
    cr_title = change.title or (change.initial_prompt[:80] if change.initial_prompt else "change")
    path_str = _ensure_docx(
        row, change_id=change_id, doc_type_label=builder_doc_type,
        cr_title=cr_title, subtype=subtype,
    )
    if not path_str:
        raise HTTPException(
            status_code=404,
            detail="DOCX could not be built — no markdown content is stored for this artifact",
        )
    # Persist docx_path update (if on-demand build happened)
    db.commit()
    path = Path(path_str)
    if not path.exists():
        raise HTTPException(status_code=500, detail="DOCX file disappeared between build and serve")

    cr_slug = (change.title or "change").lower().replace(" ", "_")[:40]
    download_name = f"{cr_slug}_{file_label}.docx"
    return FileResponse(path, media_type=_DOCX_MIME, filename=download_name)


# ── PPTX download endpoint (D8 — Product Deck companion) ─────────────────────

@router.get("/{change_id}/artifacts/{doc_type}/download/pptx")
def download_artifact_pptx(
    change_id: str, doc_type: str, db: DbDep, current_user: CurrentUser,
    subtype: str | None = None,
):
    """Download the Product Deck `.pptx` rendition.

    Today only the `product_kit` doc_type with `subtype=product_deck`
    produces a .pptx (via D6 writeback). Other doc_types / subtypes
    don't have a `pptx_path` and 404. The file is NOT built on demand
    — if the LLM didn't emit a valid JSON outline at gen time, the
    operator must regenerate the deck.
    """
    from app.models.user import UserRole
    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if not _can_read_all_changes(current_user) and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    row, _builder_doc_type, file_label = _resolve_artifact_row(
        change_id, doc_type, db, subtype,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"No {doc_type} generated for this change request yet")

    pptx_path = getattr(row, "pptx_path", None)
    if not pptx_path:
        raise HTTPException(
            status_code=404,
            detail=(
                "No .pptx available — only product_deck produces one, "
                "and only when the generator emitted a valid JSON outline. "
                "Regenerate to retry."
            ),
        )
    path = Path(pptx_path)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="PPTX file is registered but missing on disk (cleaned up?). Regenerate to rebuild.",
        )

    cr_slug = (change.title or "change").lower().replace(" ", "_")[:40]
    download_name = f"{cr_slug}_{file_label}.pptx"
    return FileResponse(path, media_type=_PPTX_MIME, filename=download_name)


# ── Validation endpoint (Sprint 5 / detail page) ─────────────────────────────

@router.get("/{change_id}/validation/{doc_type}")
def validate_artifact(
    change_id: str, doc_type: str, db: DbDep, current_user: CurrentUser,
    subtype: str | None = None,
):
    """Re-run the document validator over the stored markdown for a given artifact.

    Returns the same {error_count, warning_count, has_errors, issues} shape
    that the WS `done` event emits on generation — so the ChangeDetail page
    can render the familiar ValidationPanel over already-saved content.
    """
    from app.models.user import UserRole
    from app.agents.document_validator import validate as validate_doc, summarize as summarize_validation

    change = db.get(ChangeRequest, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and change.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    row, _builder_doc_type, _file_label = _resolve_artifact_row(change_id, doc_type, db, subtype)
    if row is None or not (row.content or "").strip():
        return {"error_count": 0, "warning_count": 0, "has_errors": False, "issues": []}

    doc_type_norm = (doc_type or "").lower()
    validator_key = {
        "brd": "brd",
        "tech_spec": "tech_spec", "tech-spec": "tech_spec",
        "xsd": "xsd",
        "canvas": "canvas",
        "product_kit": "product_kit",
    }.get(doc_type_norm, doc_type_norm)

    return summarize_validation(validate_doc(row.content, doc_type=validator_key))


# ─────────────────────────────────────────────────────────────────────────────
# Admin one-click cascade delete
# ─────────────────────────────────────────────────────────────────────────────
#
# Removes everything tied to a single change request: relational rows
# (FK cascade does most of the heavy lifting), document_chunks rows
# (keyed by metadata.change_id, not FK), Apache AGE :Chunk nodes for
# this change, in-flight agent_jobs (cancelled first so handlers
# short-circuit), Redis chunk buffers, and on-disk artefacts directories.
#
# Defaults applied (per the plan):
#   - Synchronous (DELETE blocks until done; typical change <30 sec)
#   - Hard-delete (rows physically gone; audit log line is the record)
#   - AGE :Chunk-only (code-symbol nodes belong to repos, not CRs)
#
# Safety:
#   - Admin-only (AdminUser dependency).
#   - Caller must pass `?confirm_title=<exact CR title>` to prevent
#     fat-finger; mismatched title → 400 BadRequest.
#   - Single SQL transaction wraps the relational deletes.
#   - AGE / Redis / disk cleanup are best-effort outside the transaction;
#     failures logged, not propagated.

def _safe_step_error(step: str, exc: BaseException) -> str:
    """Format one entry for the `summary["errors"]` list returned by
    `admin_delete_change`.

    SCR #6. That list is not diagnostic-only: the endpoint ends with
    `return {"deleted": True, "summary": summary, ...}`, so every string
    appended to it is serialised into the HTTP response body. Eleven handlers
    in this endpoint were interpolating `str(e)` directly, which for the
    SQL-emitting steps means the psycopg2 class name, the constraint, the
    referencing table and the full statement.

    This is the same class of leak as the two `raise HTTPException` sites
    further down — those were fixed with `client_safe_detail`, but the
    accumulator threaded through the same function was missed, because the
    `return` is ~400 lines away from the handlers and no per-handler analysis
    associates the two.

    `step` is an authored literal ("AGE delete", "agent_jobs count"), so it is
    safe and is what actually tells the operator which phase failed. Callers
    log the unredacted exception separately.
    """
    return f"{step}: {client_safe_detail(exc)}"


@router.delete("/{change_id}")
def admin_delete_change(
    change_id: str,
    db: DbDep,
    user: AdminUser,
    confirm_title: str | None = None,
):
    """Admin: hard-delete a change request and all derived data in one click.

    Required query param: `confirm_title` — must match the change's `title`
    exactly. The frontend's confirmation modal collects this; backend
    re-checks it as a server-side guard against direct API misuse.

    Returns a summary of what was deleted (row counts per category) so the
    UI can show a meaningful toast and the audit log captures the scope.
    """
    import shutil
    from datetime import datetime, timezone
    from app.core.config import settings as _settings

    cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")

    if (confirm_title or "").strip() != (cr.title or "").strip():
        raise HTTPException(
            status_code=400,
            detail="confirm_title does not match — type the exact CR title to confirm deletion",
        )

    started_at_ts = datetime.now(timezone.utc)
    cr_title = cr.title or ""
    cr_initial = cr.initial_prompt or ""
    summary: dict = {
        "change_id":         change_id,
        "title":             cr_title,
        "agent_jobs_cancelled": 0,
        "agent_jobs_deleted":   0,
        "document_chunks_deleted": 0,
        "age_chunks_detached":  0,
        "redis_chunk_buffers_cleared": 0,
        "artifact_dirs_removed": 0,
        "errors": [],
    }

    # ── Step 1: cancel any in-flight agent_jobs for this change ────────────
    # Doing this BEFORE the relational delete means active WS handlers /
    # Celery tasks see status='cancelled' on their next is_cancelled poll
    # and short-circuit gracefully, releasing FK references.
    try:
        from app.services import job_registry
        from app.models.agent_job import AgentJob, ACTIVE_STATUSES
        active_jobs = (
            db.query(AgentJob)
            .filter(AgentJob.change_request_id == change_id)
            .filter(AgentJob.status.in_(ACTIVE_STATUSES))
            .all()
        )
        for j in active_jobs:
            try:
                job_registry.cancel_job(db, j.id)
                summary["agent_jobs_cancelled"] += 1
            except Exception as e:
                summary["errors"].append(_safe_step_error(f"cancel_job {j.id}", e))
    except Exception as e:
        summary["errors"].append(_safe_step_error("job-cancel phase", e))

    # ── Step 2: clear Redis chunk buffers for this change's jobs ───────────
    # Best-effort. Iterates all jobs (active + terminal) for the change
    # and DELs their chunk lists / meta hashes. The TTL would expire them
    # anyway in 1 h, but explicit cleanup keeps Redis tidy.
    try:
        from app.services.job_registry import _get_redis, _chunks_key, _meta_key
        from app.models.agent_job import AgentJob
        redis_client = _get_redis()
        if redis_client is not None:
            all_jobs = (
                db.query(AgentJob.id)
                .filter(AgentJob.change_request_id == change_id)
                .all()
            )
            for (jid,) in all_jobs:
                try:
                    redis_client.delete(_chunks_key(jid))
                    redis_client.delete(_meta_key(jid))
                    summary["redis_chunk_buffers_cleared"] += 1
                except Exception:
                    pass
    except Exception as e:
        summary["errors"].append(_safe_step_error("redis-cleanup phase", e))

    # ── Step 3: remove on-disk artefacts ───────────────────────────────────
    # Two roots:
    #   <ARTIFACTS_DIR>/sessions/<change_id>/   — DOCX outputs from non-docgen
    #                                              flows (BRD, TSD, XSD, Product Kit)
    #   <ARTIFACTS_DIR>/docgen/<docgen_job_id>/ — per-job docgen pipeline outputs
    #                                              (markdown plan, sections JSON,
    #                                              diagram PNGs, .docx)
    artifacts_root = Path(_settings.artifacts_dir or "/app/artifacts")
    sessions_dir = artifacts_root / "sessions" / change_id
    if sessions_dir.exists():
        try:
            shutil.rmtree(sessions_dir)
            summary["artifact_dirs_removed"] += 1
        except Exception as e:
            # The absolute path is the operator's key diagnostic here, so it
            # stays — but in the LOG, not in the response body (SCR #6: the
            # summary dict is returned to the caller).
            logger.warning("artifact cleanup failed: path=%s error=%s",
                           sessions_dir, e)
            summary["errors"].append(_safe_step_error("artifact cleanup (sessions)", e))

    # Walk agent_jobs metadata for any docgen_job_id pointing at a per-job
    # artefact dir under <ARTIFACTS_DIR>/docgen/. The set is bounded
    # (~1 doc per generation × <20 generations per change is plenty).
    try:
        from app.models.agent_job import AgentJob
        docgen_dirs: set[Path] = set()
        for jid, payload in (
            db.query(AgentJob.id, AgentJob.result_payload)
            .filter(AgentJob.change_request_id == change_id)
            .all()
        ):
            if isinstance(payload, dict):
                dgid = payload.get("docgen_job_id")
                if dgid:
                    docgen_dirs.add(artifacts_root / "docgen" / dgid)
        for d in docgen_dirs:
            if d.exists():
                try:
                    shutil.rmtree(d)
                    summary["artifact_dirs_removed"] += 1
                except Exception as e:
                    # Path to the log, category label to the caller — see the
                    # sessions-dir handler above.
                    logger.warning("docgen artifact cleanup failed: path=%s error=%s",
                                   d, e)
                    summary["errors"].append(_safe_step_error("artifact cleanup (docgen)", e))
    except Exception as e:
        summary["errors"].append(_safe_step_error("docgen-artifact phase", e))

    # ── Step 4: detach Apache AGE :Chunk nodes for this change ─────────────
    # Code-symbol nodes from indexed repos are NOT touched — they belong
    # to repos, not CRs. Only nodes whose change_id property matches.
    #
    # CRITICAL: any exception inside an SQL-emitting except block must be
    # followed by `db.rollback()` — Postgres' aborted-transaction state
    # poisons every subsequent statement on the same connection until a
    # ROLLBACK is issued, regardless of whether the Python exception was
    # caught. Without the rollback, step 7's cascade load (lazy-load on
    # `conversations`) hits InFailedSqlTransaction.
    # NOTE on rollback strategy: every SQL-emitting step here uses
    # `db.begin_nested()` (a SAVEPOINT) so a failure undoes ONLY that
    # step. A bare `db.rollback()` on the outer TX would erase every
    # previously-successful delete in this request — confirmed by the
    # 2026-05-02 trace: phase_b_runs deleted 1 row, then a downstream
    # cert_triage error triggered db.rollback() which silently restored
    # the phase_b_runs row, breaking the final parent delete.
    sp = db.begin_nested()
    try:
        if getattr(_settings, "use_kg_ingestion", False):
            from app.kg import client as kg_client
            esc_cid = kg_client.escape_cypher_literal(change_id)
            cypher = (
                f"MATCH (n) WHERE n.change_id = {esc_cid} "
                f"DETACH DELETE n"
            )
            kg_client.run_cypher(db, cypher)
            # AGE doesn't return a clean delete-count via `DETACH DELETE`
            # without a RETURN — recording 0 here means "ran successfully";
            # a separate count-before query would double the work.
            summary["age_chunks_detached"] = -1   # sentinel for "ran but unmeasured"
        sp.commit()
    except Exception as e:
        summary["errors"].append(_safe_step_error("AGE delete", e))
        try:
            sp.rollback()
        except Exception:
            pass

    # ── Step 5: delete document_chunks keyed by metadata.change_id ─────────
    # These rows are NOT FK-cascaded because document_chunks doesn't have
    # a change_request_id column — they're keyed by metadata.change_id
    # JSONB property (set at ingestion time). Manual DELETE.
    #
    # NOTE: the SQL column is `metadata` (no underscore). The Python
    # attribute is `metadata_` because SQLAlchemy reserves `metadata` on
    # declarative bases — see `mapped_column("metadata", JSON, ...)` in
    # `app.models.document_chunk`. Raw SQL must use the actual DB
    # column name.
    sp = db.begin_nested()
    try:
        result = db.execute(
            text(
                "DELETE FROM document_chunks "
                "WHERE metadata->>'change_id' = :cid"
            ),
            {"cid": change_id},
        )
        summary["document_chunks_deleted"] = int(result.rowcount or 0)
        sp.commit()
    except Exception as e:
        summary["errors"].append(_safe_step_error("document_chunks delete", e))
        try:
            sp.rollback()
        except Exception:
            pass

    # ── Step 6: count agent_jobs for the audit summary ─────────────────────
    # The actual DELETE happens inside the manual chain below (and FK CASCADE
    # would also handle it). Just a read for the audit.
    sp = db.begin_nested()
    try:
        from app.models.agent_job import AgentJob
        deleted_jobs = (
            db.query(func.count(AgentJob.id))
            .filter(AgentJob.change_request_id == change_id)
            .scalar() or 0
        )
        summary["agent_jobs_deleted"] = int(deleted_jobs)
        sp.commit()
    except Exception as e:
        summary["errors"].append(_safe_step_error("agent_jobs count", e))
        try:
            sp.rollback()
        except Exception:
            pass

    # ── Step 6.5: manual delete of NON-cascading children ──────────────────
    #
    # Audit (2026-05-02): only THREE FKs to change_requests have
    # `ondelete='CASCADE'`:
    #   - agent_jobs                  (added in alembic 0025)
    #   - clarifications              (clarification model)
    #   - change_request_context
    # Every other FK is the SQLAlchemy default (NO ACTION). The first
    # cascade attempt blew up on phase_b_runs_change_request_id_fkey
    # because of this.
    #
    # We delete in the correct order — grand-children → children →
    # direct children — so each FK constraint is satisfied at the moment
    # the parent is deleted. ORM cascade through the relationship() is
    # not used: we use raw bulk-DELETE so 100k+ chunks/iterations don't
    # round-trip through the identity map.
    #
    # Order (each table assumes the previous ones have been emptied):
    #   Phase B sub-tree:
    #     uat_triage_results → uat_test_results → uat_test_runs →
    #     uat_test_cases → deployment_runs → build_runs → git_events →
    #     code_review_results → is_review_results → code_plans →
    #     code_iterations → phase_b_runs
    #   Phase C sub-tree:
    #     cert_triage → cert_test_results → cert_runs →
    #     negotiation_messages → negotiation_threads → partner_progress →
    #     a2a_messages → a2a_sessions → change_partner_assignments
    #   Direct CR children (each independent):
    #     approvals (via artifact_id IN (SELECT id FROM brds/tech_specs/xsds)) →
    #     brds, tech_specs, xsds, canvases, research_outputs,
    #     product_kit_documents, conversations, notifications, feedback
    _DELETE_SQL: list[tuple[str, str]] = [
        # Phase B grand-grand-grand-children → up.
        # Column names verified against models/phase_b.py 2026-05-02:
        #   uat_triage_results.test_result_id  → uat_test_results.id
        #   uat_test_results.test_run_id       → uat_test_runs.id
        #   (no `uat_` / `cert_` cross-prefix; uat_* tables belong to
        #    Phase B, cert_* to Phase C — they never reference each other.)
        # phase_b_triage_reports (0137) references phase_b_runs AND
        # build_runs/uat_test_runs — before all three.
        ("phase_b_triage_reports",
         "DELETE FROM phase_b_triage_reports WHERE phase_b_run_id IN "
         "(SELECT id FROM phase_b_runs WHERE change_request_id = :cid)"),
        ("uat_triage_results",
         "DELETE FROM uat_triage_results WHERE test_result_id IN "
         "(SELECT utr.id FROM uat_test_results utr "
         "  JOIN uat_test_runs utru ON utr.test_run_id = utru.id "
         "  JOIN phase_b_runs pbr ON utru.phase_b_run_id = pbr.id "
         " WHERE pbr.change_request_id = :cid)"),
        ("uat_test_results",
         "DELETE FROM uat_test_results WHERE test_run_id IN "
         "(SELECT id FROM uat_test_runs WHERE phase_b_run_id IN "
         " (SELECT id FROM phase_b_runs WHERE change_request_id = :cid))"),
        ("uat_test_runs",
         "DELETE FROM uat_test_runs WHERE phase_b_run_id IN "
         "(SELECT id FROM phase_b_runs WHERE change_request_id = :cid)"),
        ("uat_test_cases",
         "DELETE FROM uat_test_cases WHERE phase_b_run_id IN "
         "(SELECT id FROM phase_b_runs WHERE change_request_id = :cid)"),
        ("deployment_runs",
         "DELETE FROM deployment_runs WHERE phase_b_run_id IN "
         "(SELECT id FROM phase_b_runs WHERE change_request_id = :cid)"),
        ("build_runs",
         "DELETE FROM build_runs WHERE phase_b_run_id IN "
         "(SELECT id FROM phase_b_runs WHERE change_request_id = :cid)"),
        ("git_events",
         "DELETE FROM git_events WHERE phase_b_run_id IN "
         "(SELECT id FROM phase_b_runs WHERE change_request_id = :cid)"),
        ("code_review_results",
         "DELETE FROM code_review_results WHERE code_iteration_id IN "
         "(SELECT ci.id FROM code_iterations ci "
         "  JOIN phase_b_runs pbr ON ci.phase_b_run_id = pbr.id "
         " WHERE pbr.change_request_id = :cid)"),
        ("is_review_results",
         "DELETE FROM is_review_results WHERE code_iteration_id IN "
         "(SELECT ci.id FROM code_iterations ci "
         "  JOIN phase_b_runs pbr ON ci.phase_b_run_id = pbr.id "
         " WHERE pbr.change_request_id = :cid)"),
        ("code_plans",
         "DELETE FROM code_plans WHERE change_request_id = :cid"),
        ("code_iterations",
         "DELETE FROM code_iterations WHERE phase_b_run_id IN "
         "(SELECT id FROM phase_b_runs WHERE change_request_id = :cid)"),
        ("phase_b_runs",
         "DELETE FROM phase_b_runs WHERE change_request_id = :cid"),
        # Phase C sub-tree.
        # Column names verified against models/phase_c.py 2026-05-02:
        #   cert_triage.cert_test_result_id → cert_test_results.id
        #   cert_test_results.cert_run_id   → cert_runs.id
        #   a2a_messages has change_request_id directly (no session_id col)
        #   a2a_sessions does NOT have change_request_id (linked via partner_id)
        ("cert_triage",
         "DELETE FROM cert_triage WHERE cert_test_result_id IN "
         "(SELECT ctr.id FROM cert_test_results ctr "
         "  JOIN cert_runs cr2 ON ctr.cert_run_id = cr2.id "
         " WHERE cr2.change_request_id = :cid)"),
        ("cert_test_results",
         "DELETE FROM cert_test_results WHERE cert_run_id IN "
         "(SELECT id FROM cert_runs WHERE change_request_id = :cid)"),
        ("cert_runs",
         "DELETE FROM cert_runs WHERE change_request_id = :cid"),
        ("negotiation_messages",
         "DELETE FROM negotiation_messages WHERE thread_id IN "
         "(SELECT id FROM negotiation_threads WHERE change_request_id = :cid)"),
        ("negotiation_threads",
         "DELETE FROM negotiation_threads WHERE change_request_id = :cid"),
        ("partner_progress",
         "DELETE FROM partner_progress WHERE assignment_id IN "
         "(SELECT id FROM change_partner_assignments WHERE change_request_id = :cid)"),
        # a2a_messages has change_request_id directly (it's the parent FK,
        # not session_id). Delete by the direct CR link.
        ("a2a_messages",
         "DELETE FROM a2a_messages WHERE change_request_id = :cid"),
        # NOTE: a2a_sessions has NO change_request_id column — it's keyed
        # by partner_id only. Sessions persist per-partner-pair across
        # change requests, so they're intentionally NOT deleted here.
        # Any orphan a2a_messages have already been cleared above.
        ("change_partner_assignments",
         "DELETE FROM change_partner_assignments WHERE change_request_id = :cid"),
        # Approvals — link via artifact_id (no FK constraint), so we have
        # to enumerate every artifact type that belongs to this change.
        ("approvals (BRD)",
         "DELETE FROM approvals WHERE artifact_type = 'brd' "
         "  AND artifact_id IN (SELECT id FROM brds WHERE change_request_id = :cid)"),
        ("approvals (TechSpec)",
         "DELETE FROM approvals WHERE artifact_type = 'tech_spec' "
         "  AND artifact_id IN (SELECT id FROM tech_specs WHERE change_request_id = :cid)"),
        ("approvals (XSD)",
         "DELETE FROM approvals WHERE artifact_type = 'xsd' "
         "  AND artifact_id IN (SELECT id FROM xsds WHERE change_request_id = :cid)"),
        # Direct CR children (each table has FK → change_requests with NO ACTION)
        ("notifications",
         # notifications doesn't have a direct change_request_id FK in current schema —
         # it's keyed by user_id. Best-effort skip via metadata if needed; for now no-op.
         # (Left intentionally empty so the table doesn't appear in summary.deleted_by_table)
         ""),
        ("brds",
         "DELETE FROM brds WHERE change_request_id = :cid"),
        ("tech_specs",
         "DELETE FROM tech_specs WHERE change_request_id = :cid"),
        ("xsds",
         "DELETE FROM xsds WHERE change_request_id = :cid"),
        ("product_canvases",
         "DELETE FROM product_canvases WHERE change_request_id = :cid"),
        ("research_outputs",
         "DELETE FROM research_outputs WHERE change_request_id = :cid"),
        ("product_kit_documents",
         "DELETE FROM product_kit_documents WHERE change_request_id = :cid"),
        ("conversations",
         "DELETE FROM conversations WHERE change_request_id = :cid"),
        ("feedback",
         "DELETE FROM feedback WHERE change_request_id = :cid"),
        # agent_jobs has CASCADE (alembic 0025) but we delete explicitly for
        # the audit count — the cascade would eat any rows we missed anyway.
        ("agent_jobs",
         "DELETE FROM agent_jobs WHERE change_request_id = :cid"),
        # clarifications has CASCADE — explicit delete keeps the audit count.
        ("clarifications",
         "DELETE FROM clarifications WHERE change_request_id = :cid"),
        # change_request_contexts has CASCADE (note: PLURAL table name —
        # the model class is `ChangeRequestContext` but the table is
        # `change_request_contexts`).
        ("change_request_contexts",
         "DELETE FROM change_request_contexts WHERE change_request_id = :cid"),
    ]

    # CRITICAL: each per-table DELETE runs inside its own SAVEPOINT
    # (`db.begin_nested()`). On error, rollback the SAVEPOINT only —
    # NOT the whole transaction. A naive `db.rollback()` would erase
    # every previously-successful DELETE in the same TX (Postgres rolls
    # back the entire current transaction on `ROLLBACK`, not just the
    # failing statement). With SAVEPOINTs, only the failing step is
    # undone; successful prior deletes survive to the final commit.
    summary["deleted_by_table"] = {}
    for table_label, sql in _DELETE_SQL:
        if not sql:
            continue
        sp = db.begin_nested()
        try:
            r = db.execute(text(sql), {"cid": change_id})
            n = int(r.rowcount or 0)
            sp.commit()
            if n > 0:
                summary["deleted_by_table"][table_label] = n
            # Log every step (even 0-row) so we can see which deletes
            # actually executed when debugging FK-violation traces.
            logger.info(
                "admin_change_delete: change=%s deleted %d row(s) from %s",
                change_id, n, table_label,
            )
        except Exception as e:
            summary["errors"].append(_safe_step_error(f"{table_label} delete", e))
            logger.warning(
                "admin_change_delete: change=%s %s delete failed: %s",
                change_id, table_label, e,
            )
            try:
                sp.rollback()       # undo just THIS step; outer TX intact
            except Exception:
                pass
            # If a sub-tree delete fails (e.g., column rename, missing
            # table), the chain continues. Successful prior deletes are
            # preserved by the SAVEPOINT semantics. Step 7's parent
            # delete will still fail if the failing step left orphan
            # FK references — but that's now a real schema mismatch we
            # need to fix, not a self-inflicted rollback wound.

    # Commit the manual deletes BEFORE step 7. Without this, a later
    # rollback on step 7 would also undo all our progress.
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        summary["errors"].append(_safe_step_error("commit-after-manual-deletes", e))
        logger.exception("commit-after-manual-deletes failed: change=%s", change_id)
        # SCR #6: a SQLAlchemy commit failure renders table names, column names
        # and the full SQL statement into str(e). Operators get all of it from
        # logger.exception above; the caller gets a category label.
        raise HTTPException(
            status_code=500,
            detail=f"Manual delete chain commit failed: {client_safe_detail(e)}",
        )

    # Re-fetch the change_request because the commit detached the original
    # cr instance from the session's identity map.
    cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not cr:
        # Possible only if a concurrent request also deleted it. Treat as success.
        logger.info("admin_change_delete: change=%s already gone — treating as success", change_id)
    else:
        # ── Step 7: delete the change_requests row itself ──────────────
        # All non-cascading children have been removed above. The remaining
        # FKs (agent_jobs / clarifications / change_request_context) have
        # CASCADE so this is the final clean-up.
        try:
            db.delete(cr)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.exception(
                "admin_change_delete failed at parent-row delete: change=%s error=%s",
                change_id, e,
            )
            # SCR #6: see note on the sibling handler above — a FK-violation
            # message names the referencing table and constraint.
            raise HTTPException(
                status_code=500,
                detail=("Parent-row delete failed (child rows likely remaining): "
                        f"{client_safe_detail(e)}"),
            )

    duration_ms = int((datetime.now(timezone.utc) - started_at_ts).total_seconds() * 1000)
    summary["duration_ms"] = duration_ms

    # ── Audit log line — structured JSON for log shipping ──────────────────
    # This is the durable record of what was deleted. Without a dedicated
    # audit_log table (deferred to a follow-up if compliance requires it),
    # the structured log line + the summary returned to the UI are the
    # operational trail.
    logger.warning(
        "admin_change_delete: change_id=%s title=%r deleted_by=%s duration_ms=%d "
        "agent_jobs_cancelled=%d agent_jobs_deleted=%d document_chunks=%d "
        "age_chunks=%s redis_buffers=%d artifact_dirs=%d errors=%d",
        change_id, cr_title[:120], user.username, duration_ms,
        summary["agent_jobs_cancelled"], summary["agent_jobs_deleted"],
        summary["document_chunks_deleted"], summary["age_chunks_detached"],
        summary["redis_chunk_buffers_cleared"], summary["artifact_dirs_removed"],
        len(summary["errors"]),
    )

    return {
        "deleted":  True,
        "summary":  summary,
        "initial_prompt_preview": cr_initial[:120],
    }
