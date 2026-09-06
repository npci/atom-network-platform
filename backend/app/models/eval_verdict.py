# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ORM model for the `eval_verdicts` table.

Schema lives in alembic 0052_eval_verdicts.py.

Every evaluation run produces one row. Rows are never updated after
creation — overrides and retries produce new rows that reference the
original via `previous_verdict_id`. This makes the audit trail
append-only and reproducible.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean, DateTime, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON as _SA_JSON

# JSONB on Postgres, plain JSON on the SQLite test harness (house pattern —
# see kit_publication.py; raw JSONB breaks create_all under SQLite).
_JSON = JSONB().with_variant(_SA_JSON(), "sqlite")
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EvalVerdictValue(str, enum.Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class EvalPolicyMode(str, enum.Enum):
    DISABLED  = "disabled"
    ADVISORY  = "advisory"
    SOFT_GATE = "soft_gate"
    HARD_GATE = "hard_gate"


class EvalVerdict(Base):
    """One evaluation run result for a specific checkpoint on a change request.

    Linked to a change_request. Never updated after insert — retries/overrides
    add new rows and link via previous_verdict_id.

    verdict and policy_mode are stored as VARCHAR (not native Postgres enum)
    for simpler migrations and portability.
    """
    __tablename__ = "eval_verdicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # ── What was evaluated ───────────────────────────────────────────────────
    change_request_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("change_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    from_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    to_stage: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── Decision (stored as VARCHAR for simpler migrations) ──────────────────
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    policy_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)

    # ── Scoring detail (stored as JSON for flexibility) ──────────────────────
    scores_json: Mapped[dict[str, Any]] = mapped_column(
        _JSON, nullable=False, default=dict, server_default="{}",
    )
    hard_fail_codes: Mapped[list[str]] = mapped_column(
        _JSON, nullable=False, default=list, server_default="[]",
    )
    warn_codes: Mapped[list[str]] = mapped_column(
        _JSON, nullable=False, default=list, server_default="[]",
    )
    reasons_json: Mapped[list[str]] = mapped_column(
        _JSON, nullable=False, default=list, server_default="[]",
    )

    # ── Artifact traceability ────────────────────────────────────────────────
    source_artifact_ids: Mapped[list[str]] = mapped_column(
        _JSON, nullable=False, default=list, server_default="[]",
    )
    target_artifact_ids: Mapped[list[str]] = mapped_column(
        _JSON, nullable=False, default=list, server_default="[]",
    )

    # ── Execution metadata (reproducibility) ────────────────────────────────
    rubric_version: Mapped[str] = mapped_column(String(64), nullable=False)
    deterministic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    critic_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    judge_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_recommended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── Override / audit ─────────────────────────────────────────────────────
    is_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    override_actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_verdict_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("eval_verdicts.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "change_request_id": self.change_request_id,
            "checkpoint_id": self.checkpoint_id,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "verdict": self.verdict,
            "passed": self.passed,
            "policy_mode": self.policy_mode,
            "confidence": self.confidence,
            "scores": self.scores_json or {},
            "hard_fail_codes": self.hard_fail_codes or [],
            "warn_codes": self.warn_codes or [],
            "reasons": self.reasons_json or [],
            "source_artifact_ids": self.source_artifact_ids or [],
            "target_artifact_ids": self.target_artifact_ids or [],
            "rubric_version": self.rubric_version,
            "deterministic_version": self.deterministic_version,
            "critic_model": self.critic_model,
            "judge_model": self.judge_model,
            "latency_ms": self.latency_ms,
            "retry_recommended": self.retry_recommended,
            "is_override": self.is_override,
            "override_actor": self.override_actor,
            "override_reason": self.override_reason,
            "previous_verdict_id": self.previous_verdict_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
