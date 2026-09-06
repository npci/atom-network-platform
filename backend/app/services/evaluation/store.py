# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Verdict store — persists and retrieves eval_verdicts rows.

All DB operations go through here. The runner calls save_verdict().
The API reads via get_latest() and get_history().

Isolation: if persistence fails, we log a warning and continue.
Advisory mode must NEVER block the product workflow.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional, TYPE_CHECKING

from sqlalchemy.orm import Session

from app.services.evaluation.checkpoints import CheckpointId, PolicyMode
from app.services.evaluation.schemas import EvalVerdict as EvalVerdictSchema

if TYPE_CHECKING:
    from app.models.eval_verdict import EvalVerdict

logger = logging.getLogger(__name__)


def save_verdict(db: Session, change_request_id: str, verdict: EvalVerdictSchema) -> "EvalVerdict | None":
    """Persist a verdict. Returns the ORM row or None if persistence fails.

    Never raises — advisory mode must not block workflow.
    """
    try:
        EvalVerdict = _eval_verdict_model()
        row = EvalVerdict(
            id=str(uuid.uuid4()),
            change_request_id=change_request_id,
            checkpoint_id=verdict.checkpoint_id.value,
            from_stage=_contract_from_stage(verdict.checkpoint_id),
            to_stage=_contract_to_stage(verdict.checkpoint_id),
            verdict=verdict.verdict.value,
            passed=verdict.passed,
            policy_mode=verdict.policy_mode.value,
            confidence=verdict.confidence,
            scores_json=verdict.scores,
            hard_fail_codes=verdict.hard_fail_codes,
            warn_codes=verdict.warn_codes,
            reasons_json=verdict.reasons,
            source_artifact_ids=verdict.source_artifact_ids,
            target_artifact_ids=verdict.target_artifact_ids,
            rubric_version=verdict.rubric_version,
            deterministic_version=verdict.deterministic_version,
            critic_model=verdict.critic_model,
            judge_model=verdict.judge_model,
            latency_ms=verdict.latency_ms,
            retry_recommended=verdict.retry_recommended,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info(
            "eval_verdict saved checkpoint=%s change=%s verdict=%s id=%s",
            verdict.checkpoint_id.value, change_request_id, verdict.verdict.value, row.id,
        )
        return row
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "eval_verdict save failed (advisory — workflow continues) "
            "checkpoint=%s change=%s error=%s",
            verdict.checkpoint_id, change_request_id, exc,
        )
        db.rollback()
        return None


def save_override_verdict(
    db: Session,
    *,
    previous_verdict,
    override_actor: str,
    override_reason: str,
    policy_mode: PolicyMode,
) -> "EvalVerdict | None":
    """Insert an override verdict row linked to a previous blocked verdict."""
    try:
        EvalVerdict = _eval_verdict_model()
        row = EvalVerdict(
            id=str(uuid.uuid4()),
            change_request_id=previous_verdict.change_request_id,
            checkpoint_id=previous_verdict.checkpoint_id,
            from_stage=previous_verdict.from_stage,
            to_stage=previous_verdict.to_stage,
            verdict="PASS",
            passed=True,
            policy_mode=policy_mode.value,
            confidence=1.0,
            scores_json=previous_verdict.scores_json or {},
            hard_fail_codes=[],
            warn_codes=["MANUAL_OVERRIDE"],
            reasons_json=[
                f"Manual override accepted: {override_reason}",
                *list(previous_verdict.reasons_json or []),
            ],
            source_artifact_ids=previous_verdict.source_artifact_ids or [],
            target_artifact_ids=previous_verdict.target_artifact_ids or [],
            rubric_version=previous_verdict.rubric_version,
            deterministic_version=previous_verdict.deterministic_version,
            critic_model=previous_verdict.critic_model,
            judge_model=previous_verdict.judge_model,
            latency_ms=0,
            retry_recommended=False,
            is_override=True,
            override_actor=override_actor,
            override_reason=override_reason,
            previous_verdict_id=previous_verdict.id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info(
            "eval_verdict override saved checkpoint=%s change=%s previous=%s override=%s",
            previous_verdict.checkpoint_id,
            previous_verdict.change_request_id,
            previous_verdict.id,
            row.id,
        )
        return row
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "eval_verdict override save failed checkpoint=%s change=%s error=%s",
            getattr(previous_verdict, "checkpoint_id", "?"),
            getattr(previous_verdict, "change_request_id", "?"),
            exc,
        )
        db.rollback()
        return None


def get_latest(
    db: Session,
    change_request_id: str,
    checkpoint_id: CheckpointId | str,
) -> Optional["EvalVerdict"]:
    """Return the most recent verdict for a checkpoint on a change request."""
    EvalVerdict = _eval_verdict_model()
    cp = checkpoint_id.value if isinstance(checkpoint_id, CheckpointId) else checkpoint_id
    return (
        db.query(EvalVerdict)
        .filter(
            EvalVerdict.change_request_id == change_request_id,
            EvalVerdict.checkpoint_id == cp,
        )
        .order_by(EvalVerdict.created_at.desc())
        .first()
    )


def get_history(
    db: Session,
    change_request_id: str,
    checkpoint_id: CheckpointId | str | None = None,
    limit: int = 50,
) -> list["EvalVerdict"]:
    """Return verdict history for a change request, newest first."""
    EvalVerdict = _eval_verdict_model()
    q = db.query(EvalVerdict).filter(
        EvalVerdict.change_request_id == change_request_id,
    )
    if checkpoint_id is not None:
        cp = checkpoint_id.value if isinstance(checkpoint_id, CheckpointId) else checkpoint_id
        q = q.filter(EvalVerdict.checkpoint_id == cp)
    return q.order_by(EvalVerdict.created_at.desc()).limit(limit).all()


def count_runs(
    db: Session,
    change_request_id: str,
    checkpoint_id: CheckpointId | str,
    *,
    include_overrides: bool = False,
) -> int:
    EvalVerdict = _eval_verdict_model()
    cp = checkpoint_id.value if isinstance(checkpoint_id, CheckpointId) else checkpoint_id
    q = db.query(EvalVerdict).filter(
        EvalVerdict.change_request_id == change_request_id,
        EvalVerdict.checkpoint_id == cp,
    )
    if not include_overrides:
        q = q.filter(EvalVerdict.is_override.is_(False))
    return int(q.count())


def get_by_id(db: Session, verdict_id: str) -> Optional["EvalVerdict"]:
    EvalVerdict = _eval_verdict_model()
    return db.query(EvalVerdict).filter(EvalVerdict.id == verdict_id).first()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _contract_from_stage(cp: CheckpointId) -> str:
    from app.services.evaluation.contracts import get_contract
    return get_contract(cp).from_stage


def _contract_to_stage(cp: CheckpointId) -> str:
    from app.services.evaluation.contracts import get_contract
    return get_contract(cp).to_stage


def _eval_verdict_model():
    from app.models.eval_verdict import EvalVerdict
    return EvalVerdict
