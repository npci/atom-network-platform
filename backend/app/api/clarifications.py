# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Clarifications API — pre-generation gap questions for the PM.

Endpoints:
  POST /changes/{id}/clarify                  — trigger clarify run (bumps version)
  GET  /changes/{id}/clarifications           — latest version
  POST /changes/{id}/clarifications/answer    — PM submits answers
  POST /changes/{id}/clarifications/skip      — PM skips (only if no blocking gaps)
  POST /changes/{id}/clarifications/rerun     — new version, keeps prior answers

The flow is triggered at the end of Canvas stage. Once the row's status is
`answered` or `skipped`, the change request can advance to BRD.
"""
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.core.deps import DbDep, CurrentUser
from app.models.change_request import ChangeRequest
from app.models.clarification import Clarification
from app.models.canvas import ProductCanvas
from app.models.user import UserRole
from app.agents.ambiguity_detector import detect as detect_gaps
from app.agents.assumption_handler import apply as apply_assumptions
from app.agents.question_generator import gen_questions
from app.services.context_cache import get_or_build
from app.services.evaluation.checkpoints import CheckpointId
from app.services.evaluation.runner import fire_advisory_eval

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/changes", tags=["clarifications"])


# ──────────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────────

class AnswerPayload(BaseModel):
    answers: dict[str, str]  # {question_id: answer_text}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _check_access(change_id: str, db, current_user):
    cr = db.get(ChangeRequest, change_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    if current_user.role != UserRole.ADMIN and cr.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return cr


def _latest_clarification(change_id: str, db) -> Clarification | None:
    return (
        db.query(Clarification)
        .filter(Clarification.change_request_id == change_id)
        .order_by(Clarification.version.desc())
        .first()
    )


def _fire_canvas_to_clarification_eval(change_id: str, row: Clarification, db) -> None:
    """Phase 7: advisory eval when a clarification reaches a terminal status.

    Fire-and-forget. Pulls the latest canvas as the source artifact and packages
    the questions + answers + per-question status into the target artifact
    expected by `check_no_unanswered_canvas_questions`.
    """
    try:
        canvas = (
            db.query(ProductCanvas)
            .filter(ProductCanvas.change_request_id == change_id)
            .order_by(ProductCanvas.version.desc())
            .first()
        )
        questions = list(row.questions or [])
        answers = dict(row.answers or {})
        terminal_status = (row.status or "").lower()
        enriched_questions: list[dict] = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id", ""))
            answer = answers.get(qid, "").strip() if qid else ""
            if terminal_status in ("answered", "skipped"):
                q_status = "answered" if answer else "skipped"
            else:
                q_status = "answered" if answer else "pending"
            enriched_questions.append({**q, "status": q_status, "answer": answer})

        fire_advisory_eval(
            change_request_id=change_id,
            checkpoint_id=CheckpointId.CANVAS_TO_CLARIFICATION,
            source_artifacts={
                "product_canvas": {
                    "type": "product_canvas",
                    "content": (canvas.content if canvas else ""),
                },
            },
            target_artifacts={
                "clarification_thread": {
                    "type": "clarification_thread",
                    "questions": enriched_questions,
                    "answers":   answers,
                    "status":    row.status,
                },
            },
            source_artifact_ids=[canvas.id] if canvas and canvas.id else [],
            target_artifact_ids=[row.id] if row.id else [],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not fire canvas_to_clarification eval for change=%s: %s",
            change_id, exc,
        )


def _serialize(row: Clarification | None) -> dict:
    if row is None:
        return {"exists": False}
    return {
        "exists":            True,
        "id":                row.id,
        "version":           row.version,
        "status":            row.status,
        "questions":         row.questions or [],
        "answers":           row.answers or {},
        "blocking_gap_keys": row.blocking_gap_keys or [],
        "assumed_gaps":      row.assumed_gaps or [],
        "created_at":        row.created_at.isoformat() if row.created_at else None,
        "updated_at":        row.updated_at.isoformat() if row.updated_at else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/{change_id}/clarify")
async def trigger_clarify(change_id: str, db: DbDep, current_user: CurrentUser):
    """Run the clarify pipeline: classify → retrieve → extract proposals →
    detect gaps → apply assumptions → generate questions. Writes a new
    Clarification row (version bumped).

    Returns the created row. If zero blocking gaps were found, status is
    preset to 'skipped' (UI shows "No clarifications needed").
    """
    cr = _check_access(change_id, db, current_user)

    # 1. Ensure we have a fresh context cache (classify + proposals)
    ctx = await get_or_build(change_id, db, refresh=False)
    if ctx is None:
        raise HTTPException(status_code=400, detail="Could not build context for this change request")

    proposals = ctx.proposals or {}
    required_fields = []
    try:
        from app.agents.taxonomy import get_taxonomy
        bucket = get_taxonomy().get(ctx.taxonomy_primary or "", {})
        required_fields = bucket.get("required_fields", [])
    except Exception:
        pass

    # 2. Detect gaps
    feature_desc = cr.enhanced_prompt or cr.initial_prompt or ""
    gaps = await detect_gaps(
        feature_description=feature_desc,
        proposals=proposals,
        required_fields=required_fields,
        taxonomy_primary=ctx.taxonomy_primary,
    )

    # 3. Apply assumption rules → split blocking vs assumable
    blocking_keys, assumed = apply_assumptions(gaps)

    # 4. Generate PM questions for blocking gaps
    gap_descriptions = {g["key"]: g.get("description", "") for g in gaps}
    if blocking_keys:
        questions = await gen_questions(
            feature_description=feature_desc,
            blocking_gap_keys=blocking_keys,
            taxonomy_primary=ctx.taxonomy_primary,
            gap_descriptions=gap_descriptions,
        )
    else:
        questions = []

    # 5. Persist a new version
    latest = _latest_clarification(change_id, db)
    next_version = (latest.version + 1) if latest else 1

    initial_status = "skipped" if not blocking_keys else "pending"

    row = Clarification(
        change_request_id=change_id,
        version=next_version,
        blocking_gap_keys=blocking_keys,
        assumed_gaps=assumed,
        questions=questions,
        answers={},
        status=initial_status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "Clarify: change=%s version=%d blocking=%d assumed=%d status=%s",
        change_id, next_version, len(blocking_keys), len(assumed), initial_status,
    )
    return _serialize(row)


@router.get("/{change_id}/clarifications")
def get_clarifications(change_id: str, db: DbDep, current_user: CurrentUser):
    """Return the latest clarification session (or {exists: false})."""
    _check_access(change_id, db, current_user)
    row = _latest_clarification(change_id, db)
    return _serialize(row)


@router.post("/{change_id}/clarifications/answer")
def submit_answers(
    change_id: str, payload: AnswerPayload, db: DbDep, current_user: CurrentUser,
):
    """Save PM answers. If every required question has a non-empty answer,
    flips status to 'answered'. Otherwise stays 'pending'."""
    _check_access(change_id, db, current_user)
    row = _latest_clarification(change_id, db)
    if row is None:
        raise HTTPException(status_code=404, detail="No clarification to answer")

    # Merge incoming answers over existing (preserve prior answers if PM submits partially)
    existing = dict(row.answers or {})
    for qid, text in (payload.answers or {}).items():
        if text is not None:
            existing[str(qid)] = str(text).strip()
    row.answers = existing

    # Determine completion
    required_ids = [q["id"] for q in (row.questions or []) if q.get("required", True)]
    missing = [qid for qid in required_ids if not existing.get(qid, "").strip()]
    if not missing:
        row.status = "answered"
        logger.info("Clarify answered: change=%s version=%d", change_id, row.version)
    else:
        row.status = "pending"
        logger.info(
            "Clarify partial: change=%s version=%d missing=%d/%d",
            change_id, row.version, len(missing), len(required_ids),
        )

    db.commit()
    db.refresh(row)

    # Phase 7: advisory eval fires only when the row reaches a terminal status.
    if row.status == "answered":
        _fire_canvas_to_clarification_eval(change_id, row, db)

    return _serialize(row)


@router.post("/{change_id}/clarifications/skip")
def skip_clarifications(change_id: str, db: DbDep, current_user: CurrentUser):
    """Skip clarification — only valid when there are no blocking gaps."""
    _check_access(change_id, db, current_user)
    row = _latest_clarification(change_id, db)
    if row is None:
        raise HTTPException(status_code=404, detail="No clarification to skip")
    if row.blocking_gap_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot skip — {len(row.blocking_gap_keys)} blocking gap(s) must be answered first",
        )
    row.status = "skipped"
    db.commit()
    db.refresh(row)
    logger.info("Clarify skipped: change=%s version=%d", change_id, row.version)

    # Phase 7: advisory eval also fires on skip (skip is a terminal status).
    _fire_canvas_to_clarification_eval(change_id, row, db)

    return _serialize(row)


@router.post("/{change_id}/clarifications/rerun")
async def rerun_clarifications(change_id: str, db: DbDep, current_user: CurrentUser):
    """Regenerate clarifications — new version, preserves prior answers.

    Useful after Research or Canvas changes. Prior answers are carried over
    for any question whose gap_key still exists in the new version, so the
    PM doesn't lose context.
    """
    cr = _check_access(change_id, db, current_user)
    prior = _latest_clarification(change_id, db)
    prior_answers_by_key: dict[str, str] = {}
    if prior and prior.answers and prior.questions:
        id_to_key = {q["id"]: q.get("gap_key", "") for q in prior.questions}
        for qid, ans in prior.answers.items():
            key = id_to_key.get(qid)
            if key:
                prior_answers_by_key[key] = ans

    # Force-refresh context so fresh proposals/gaps are produced
    ctx = await get_or_build(change_id, db, refresh=True)
    if ctx is None:
        raise HTTPException(status_code=400, detail="Context refresh failed")

    from app.agents.taxonomy import get_taxonomy
    bucket = get_taxonomy().get(ctx.taxonomy_primary or "", {})
    required_fields = bucket.get("required_fields", [])

    feature_desc = cr.enhanced_prompt or cr.initial_prompt or ""
    gaps = await detect_gaps(
        feature_description=feature_desc,
        proposals=ctx.proposals or {},
        required_fields=required_fields,
        taxonomy_primary=ctx.taxonomy_primary,
    )
    blocking_keys, assumed = apply_assumptions(gaps)
    gap_descriptions = {g["key"]: g.get("description", "") for g in gaps}
    questions = await gen_questions(
        feature_description=feature_desc,
        blocking_gap_keys=blocking_keys,
        taxonomy_primary=ctx.taxonomy_primary,
        gap_descriptions=gap_descriptions,
    ) if blocking_keys else []

    # Carry prior answers over by gap_key match
    carried_answers: dict[str, str] = {}
    for q in questions:
        prior_ans = prior_answers_by_key.get(q.get("gap_key", ""))
        if prior_ans:
            carried_answers[q["id"]] = prior_ans

    next_version = (prior.version + 1) if prior else 1

    # If nothing is blocking, preset to skipped
    if not blocking_keys:
        status_value = "skipped"
    elif carried_answers and all(q["id"] in carried_answers for q in questions if q.get("required", True)):
        status_value = "answered"
    else:
        status_value = "pending"

    row = Clarification(
        change_request_id=change_id,
        version=next_version,
        blocking_gap_keys=blocking_keys,
        assumed_gaps=assumed,
        questions=questions,
        answers=carried_answers,
        status=status_value,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "Clarify rerun: change=%s version=%d carried=%d status=%s",
        change_id, next_version, len(carried_answers), status_value,
    )
    return _serialize(row)
