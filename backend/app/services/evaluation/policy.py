# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 2 policy configuration and gate-decision mapping."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.evaluation.checkpoints import CheckpointId, PolicyMode, VerdictValue
from app.services.evaluation.contracts import get_contract
from app.services.evaluation.hard_fail_catalog import get_hard_fail

logger = logging.getLogger(__name__)

POLICY_KEY_PREFIX = "eval_policy."
MAX_RETRIES_PER_CHECKPOINT = 1


@dataclass(slots=True)
class GateDecision:
    checkpoint_id: str
    policy_mode: PolicyMode
    verdict: str | None
    blocked: bool
    reason: str
    hard_fail_codes: list[str] = field(default_factory=list)
    warn_codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    hard_fail_details: list[dict] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    target_artifact_ids: list[str] = field(default_factory=list)
    verdict_id: str | None = None
    requires_ack: bool = False
    required_ack_verdict_id: str | None = None
    retry_available: bool = False
    retries_used: int = 0
    max_retries: int = MAX_RETRIES_PER_CHECKPOINT
    override_allowed: bool = False

    def to_debug_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "policy_mode": self.policy_mode.value,
            "verdict": self.verdict,
            "blocked": self.blocked,
            "reason": self.reason,
            "hard_fail_codes": self.hard_fail_codes,
            "warn_codes": self.warn_codes,
            "reasons": self.reasons,
            "hard_fail_details": self.hard_fail_details,
            "source_artifact_ids": self.source_artifact_ids,
            "target_artifact_ids": self.target_artifact_ids,
            "verdict_id": self.verdict_id,
            "requires_ack": self.requires_ack,
            "required_ack_verdict_id": self.required_ack_verdict_id,
            "retry_available": self.retry_available,
            "retries_used": self.retries_used,
            "max_retries": self.max_retries,
            "override_allowed": self.override_allowed,
        }


def policy_config_key(checkpoint_id: CheckpointId | str) -> str:
    cp = CheckpointId(checkpoint_id) if isinstance(checkpoint_id, str) else checkpoint_id
    return f"{POLICY_KEY_PREFIX}{cp.value}"


def get_policy_mode(
    db: Session,
    checkpoint_id: CheckpointId | str,
    *,
    fallback: PolicyMode | None = None,
) -> PolicyMode:
    contract = get_contract(checkpoint_id)
    default_mode = fallback or contract.policy_mode
    key = policy_config_key(contract.checkpoint_id)
    try:
        raw = db.execute(
            text("SELECT value FROM app_configs WHERE key = :key"),
            {"key": key},
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not read eval policy override for key=%s (fallback=%s): %s",
            key,
            default_mode.value,
            exc,
        )
        return default_mode

    if raw is None or str(raw).strip() == "":
        return default_mode

    try:
        return PolicyMode(str(raw).strip())
    except ValueError:
        logger.warning(
            "Invalid eval policy override '%s' for checkpoint=%s; using %s",
            raw,
            contract.checkpoint_id.value,
            default_mode.value,
        )
        return default_mode


def list_policy_overrides(db: Session) -> dict[str, PolicyMode]:
    try:
        rows = db.execute(
            text("SELECT key, value FROM app_configs WHERE key LIKE :prefix"),
            {"prefix": f"{POLICY_KEY_PREFIX}%"},
        ).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list eval policy overrides: %s", exc)
        return {}
    overrides: dict[str, PolicyMode] = {}
    for key, value in rows:
        if not key or value is None:
            continue
        cp_id = str(key).removeprefix(POLICY_KEY_PREFIX)
        try:
            cp = CheckpointId(cp_id)
            overrides[cp.value] = PolicyMode(str(value).strip())
        except ValueError:
            logger.warning("Ignoring malformed eval policy override key=%s value=%s", key, value)
    return overrides


def set_policy_mode(
    db: Session,
    checkpoint_id: CheckpointId | str,
    mode: PolicyMode,
    *,
    commit: bool = True,
) -> None:
    cp = CheckpointId(checkpoint_id) if isinstance(checkpoint_id, str) else checkpoint_id
    key = policy_config_key(cp)
    now = datetime.now(timezone.utc)
    exists = db.execute(
        text("SELECT 1 FROM app_configs WHERE key = :key"),
        {"key": key},
    ).scalar_one_or_none()

    if exists:
        db.execute(
            text(
                "UPDATE app_configs "
                "SET value = :value, category = :category, is_secret = false, updated_at = :updated_at "
                "WHERE key = :key"
            ),
            {
                "key": key,
                "value": mode.value,
                "category": "evaluation",
                "updated_at": now,
            },
        )
    else:
        db.execute(
            text(
                "INSERT INTO app_configs (key, value, category, is_secret, created_at, updated_at) "
                "VALUES (:key, :value, :category, false, :created_at, :updated_at)"
            ),
            {
                "key": key,
                "value": mode.value,
                "category": "evaluation",
                "created_at": now,
                "updated_at": now,
            },
        )
    if commit:
        db.commit()


def decide_gate(
    *,
    checkpoint_id: CheckpointId | str,
    policy_mode: PolicyMode,
    verdict,
    acknowledged_verdict_id: str | None = None,
    retry_allowed: bool = False,
    retries_used: int = 0,
    override_allowed: bool = False,
) -> GateDecision:
    cp = CheckpointId(checkpoint_id) if isinstance(checkpoint_id, str) else checkpoint_id
    cp_value = cp.value

    def _build_hard_fail_details(codes: list[str]) -> list[dict]:
        details: list[dict] = []
        for code in codes:
            try:
                entry = get_hard_fail(code)
                details.append({
                    "code": entry.code,
                    "title": entry.title,
                    "meaning": entry.meaning,
                    "remediation": entry.remediation,
                })
            except KeyError:
                details.append({
                    "code": code,
                    "title": "Unknown hard-fail code",
                    "meaning": "",
                    "remediation": "",
                })
        return details

    if policy_mode in (PolicyMode.DISABLED, PolicyMode.ADVISORY):
        return GateDecision(
            checkpoint_id=cp_value,
            policy_mode=policy_mode,
            verdict=getattr(verdict, "verdict", None),
            blocked=False,
            reason=f"Policy mode '{policy_mode.value}' does not block transitions.",
        )

    if verdict is None:
        return GateDecision(
            checkpoint_id=cp_value,
            policy_mode=policy_mode,
            verdict=None,
            blocked=True,
            reason="No evaluation verdict recorded for gated checkpoint.",
        )

    verdict_value = str(getattr(verdict, "verdict", "")).upper()
    verdict_id = getattr(verdict, "id", None)
    hard_fail_codes = list(getattr(verdict, "hard_fail_codes", []) or [])
    warn_codes = list(getattr(verdict, "warn_codes", []) or [])
    reasons = list(getattr(verdict, "reasons_json", None) or getattr(verdict, "reasons", []) or [])
    hard_fail_details = _build_hard_fail_details(hard_fail_codes)
    source_ids = list(getattr(verdict, "source_artifact_ids", []) or [])
    target_ids = list(getattr(verdict, "target_artifact_ids", []) or [])

    if verdict_value == VerdictValue.PASS.value:
        return GateDecision(
            checkpoint_id=cp_value,
            policy_mode=policy_mode,
            verdict=verdict_value,
            blocked=False,
            reason="Checkpoint passed.",
            verdict_id=verdict_id,
            hard_fail_codes=hard_fail_codes,
            warn_codes=warn_codes,
            reasons=reasons,
            hard_fail_details=hard_fail_details,
            source_artifact_ids=source_ids,
            target_artifact_ids=target_ids,
        )

    if verdict_value == VerdictValue.WARN.value:
        if policy_mode == PolicyMode.SOFT_GATE:
            if acknowledged_verdict_id and verdict_id and acknowledged_verdict_id == verdict_id:
                return GateDecision(
                    checkpoint_id=cp_value,
                    policy_mode=policy_mode,
                    verdict=verdict_value,
                    blocked=False,
                    reason=f"WARN verdict {verdict_id} acknowledged by caller.",
                    verdict_id=verdict_id,
                    hard_fail_codes=hard_fail_codes,
                    warn_codes=warn_codes,
                    reasons=reasons,
                    hard_fail_details=hard_fail_details,
                    source_artifact_ids=source_ids,
                    target_artifact_ids=target_ids,
                )
            return GateDecision(
                checkpoint_id=cp_value,
                policy_mode=policy_mode,
                verdict=verdict_value,
                blocked=True,
                reason=(
                    "WARN verdict requires acknowledgement in soft gate mode. "
                    "Retry with eval_acknowledged_verdict_id."
                ),
                verdict_id=verdict_id,
                requires_ack=True,
                required_ack_verdict_id=verdict_id,
                hard_fail_codes=hard_fail_codes,
                warn_codes=warn_codes,
                reasons=reasons,
                hard_fail_details=hard_fail_details,
                source_artifact_ids=source_ids,
                target_artifact_ids=target_ids,
            )
        if policy_mode == PolicyMode.HARD_GATE:
            return GateDecision(
                checkpoint_id=cp_value,
                policy_mode=policy_mode,
                verdict=verdict_value,
                blocked=True,
                reason=(
                    "WARN verdict blocks transition in hard gate mode. "
                    "Authorized role may use manual override with reason."
                ),
                verdict_id=verdict_id,
                hard_fail_codes=hard_fail_codes,
                warn_codes=warn_codes,
                reasons=reasons,
                hard_fail_details=hard_fail_details,
                source_artifact_ids=source_ids,
                target_artifact_ids=target_ids,
                override_allowed=override_allowed,
            )
        return GateDecision(
            checkpoint_id=cp_value,
            policy_mode=policy_mode,
            verdict=verdict_value,
            blocked=False,
            reason="WARN allowed in current policy mode.",
            verdict_id=verdict_id,
            hard_fail_codes=hard_fail_codes,
            warn_codes=warn_codes,
            reasons=reasons,
            hard_fail_details=hard_fail_details,
            source_artifact_ids=source_ids,
            target_artifact_ids=target_ids,
        )

    if verdict_value == VerdictValue.FAIL.value:
        retry_available = bool(retry_allowed and retries_used < MAX_RETRIES_PER_CHECKPOINT)
        if retry_available:
            reason = (
                "FAIL verdict blocks transition. One retry is still available; "
                "regenerate once, then re-evaluate."
            )
        elif override_allowed:
            reason = (
                "FAIL verdict blocks transition. Retry exhausted or disabled; "
                "authorized role may use manual override with reason."
            )
        else:
            reason = "FAIL verdict blocks transition."
        return GateDecision(
            checkpoint_id=cp_value,
            policy_mode=policy_mode,
            verdict=verdict_value,
            blocked=True,
            reason=reason,
            verdict_id=verdict_id,
            hard_fail_codes=hard_fail_codes,
            warn_codes=warn_codes,
            reasons=reasons,
            hard_fail_details=hard_fail_details,
            source_artifact_ids=source_ids,
            target_artifact_ids=target_ids,
            retry_available=retry_available,
            retries_used=retries_used,
            max_retries=MAX_RETRIES_PER_CHECKPOINT,
            override_allowed=override_allowed,
        )

    return GateDecision(
        checkpoint_id=cp_value,
        policy_mode=policy_mode,
        verdict=verdict_value,
        blocked=True,
        reason=f"Unknown verdict value '{verdict_value}'.",
        verdict_id=verdict_id,
        hard_fail_codes=hard_fail_codes,
        warn_codes=warn_codes,
        reasons=reasons,
        hard_fail_details=hard_fail_details,
        source_artifact_ids=source_ids,
        target_artifact_ids=target_ids,
    )
