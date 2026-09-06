# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Evaluation runner — orchestrates deterministic checks, critic, judge, store.

Phase 1: advisory only. The runner never raises or blocks. If anything
fails, it logs a warning and returns an advisory WARN verdict so the
product workflow continues unaffected.

Usage (from a Phase A endpoint, after artifact generation):

    from app.services.evaluation.runner import run_advisory
    from app.services.evaluation.checkpoints import CheckpointId

    # Fire and forget — does not block the endpoint response
    asyncio.create_task(
        run_advisory(
            db=db,
            change_request_id=change_id,
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": {"type": "brd", "content": brd_text}},
            target_artifacts={"tech_spec_document": {"type": "tech_spec", "content": tsd_text}},
        )
    )

For wiring from a sync (non-async) FastAPI endpoint use
`fire_advisory_eval(...)` from this module — it picks the right dispatch
strategy automatically (asyncio task in async contexts, daemon thread in
sync contexts).

Phase 2 will add: critic model call, retry loop, gate enforcement.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional, TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.services.evaluation.checkpoints import CheckpointId, VerdictValue, PolicyMode
from app.services.evaluation.contracts import get_contract
from app.services.evaluation.deterministic import run_checks, DETERMINISTIC_VERSION
from app.services.evaluation.judge import judge_advisory
from app.services.evaluation.policy import get_policy_mode
from app.services.evaluation.schemas import EvalVerdict as EvalVerdictSchema

if TYPE_CHECKING:
    from app.models.eval_verdict import EvalVerdict

logger = logging.getLogger(__name__)


async def run_advisory(
    db: Session,
    change_request_id: str,
    checkpoint_id: CheckpointId,
    source_artifacts: dict[str, dict],
    target_artifacts: dict[str, dict],
    source_artifact_ids: Optional[list[str]] = None,
    target_artifact_ids: Optional[list[str]] = None,
) -> Optional[EvalVerdict]:
    """Run advisory evaluation for one checkpoint.

    Always returns (never raises). In advisory mode the verdict is stored
    but the caller's workflow is never blocked regardless of outcome.

    Returns the persisted EvalVerdict ORM row, or None if something failed
    at the persistence level.
    """
    start_ms = int(time.time() * 1000)

    try:
        contract = get_contract(checkpoint_id)
    except KeyError:
        logger.warning("run_advisory: no contract for checkpoint %s — skipping", checkpoint_id)
        return None

    effective_policy_mode = get_policy_mode(
        db,
        checkpoint_id,
        fallback=contract.policy_mode,
    )

    if effective_policy_mode == PolicyMode.DISABLED:
        logger.debug("run_advisory: checkpoint %s is disabled — skipping", checkpoint_id)
        return None

    all_artifacts = {**source_artifacts, **target_artifacts}

    # ── 1. Check required artifacts are present ───────────────────────────
    missing_artifacts: list[str] = []
    for key in contract.required_source_artifacts:
        if key not in source_artifacts or not source_artifacts[key]:
            missing_artifacts.append(f"source:{key}")
    for key in contract.required_target_artifacts:
        if key not in target_artifacts or not target_artifacts[key]:
            missing_artifacts.append(f"target:{key}")

    if missing_artifacts:
        verdict = _build_verdict(
            checkpoint_id=checkpoint_id,
            contract_rubric_version=contract.rubric_version,
            verdict_value=VerdictValue.FAIL,
            passed=False,
            confidence=1.0,
            hard_fail_codes=["MISSING_REQUIRED_ARTIFACT"],
            reasons=[f"Required artifact(s) missing: {', '.join(missing_artifacts)}"],
            source_artifact_ids=source_artifact_ids or [],
            target_artifact_ids=target_artifact_ids or [],
            latency_ms=int(time.time() * 1000) - start_ms,
            warn_codes=[],
            judge_model=None,
            policy_mode=effective_policy_mode,
            retry_recommended=contract.retry_allowed,
        )
        return _persist_verdict(db, change_request_id, verdict)

    # ── 2. Run deterministic checks ───────────────────────────────────────
    det_findings = run_checks(contract.deterministic_checks, all_artifacts)

    # ── 3. Critic LLM (semantic findings) — Phase A Excellence Slice 1.
    # Never raises. When disabled or unable to run, contributes no findings.
    from app.services.evaluation.critic import critique
    critic_result = await critique(
        db=db,
        checkpoint_id=checkpoint_id,
        source_artifacts=source_artifacts,
        target_artifacts=target_artifacts,
    )

    # ── 4. Judge policy (deterministic + critic) ──────────────────────────
    decision = judge_advisory(
        deterministic_findings=det_findings,
        contract_hard_fail_codes=contract.hard_fail_codes,
        critic_findings=critic_result.findings if critic_result.enabled else None,
    )

    # Grounding provenance — informational only (never changes the verdict).
    # Surfaced as one human-readable reason line so Eval Logs shows what the
    # judge consulted. (scores stays dict[str,float] for rubric scores only.)
    grounding_sources = getattr(critic_result, "grounding_sources", []) or []
    reasons = list(decision.reasons)
    if grounding_sources:
        files = ", ".join(s.get("source_file", "?") for s in grounding_sources)
        reasons.append(f"[grounding] evaluated against {len(grounding_sources)} knowledge source(s): {files}")

    verdict = _build_verdict(
        checkpoint_id=checkpoint_id,
        contract_rubric_version=contract.rubric_version,
        verdict_value=decision.verdict,
        passed=decision.passed,
        confidence=decision.confidence,
        hard_fail_codes=decision.hard_fail_codes,
        reasons=reasons,
        source_artifact_ids=source_artifact_ids or [],
        target_artifact_ids=target_artifact_ids or [],
        latency_ms=int(time.time() * 1000) - start_ms,
        warn_codes=decision.warn_codes,
        judge_model=decision.judge_model,
        critic_model=critic_result.judge_model,
        policy_mode=effective_policy_mode,
        retry_recommended=(
            decision.verdict == VerdictValue.FAIL and contract.retry_allowed
        ),
    )
    return _persist_verdict(db, change_request_id, verdict)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_verdict(
    *,
    checkpoint_id: CheckpointId,
    contract_rubric_version: str,
    verdict_value: VerdictValue,
    passed: bool,
    confidence: float,
    hard_fail_codes: list[str],
    reasons: list[str],
    source_artifact_ids: list[str],
    target_artifact_ids: list[str],
    latency_ms: int,
    warn_codes: list[str],
    judge_model: str | None,
    policy_mode: PolicyMode,
    retry_recommended: bool,
    critic_model: str | None = None,
) -> EvalVerdictSchema:
    return EvalVerdictSchema(
        checkpoint_id=checkpoint_id,
        verdict=verdict_value,
        passed=passed,
        policy_mode=policy_mode,
        confidence=confidence,
        scores={},
        hard_fail_codes=hard_fail_codes,
        warn_codes=warn_codes,
        reasons=reasons if reasons else ["No specific reasons recorded."],
        source_artifact_ids=source_artifact_ids,
        target_artifact_ids=target_artifact_ids,
        rubric_version=contract_rubric_version,
        deterministic_version=DETERMINISTIC_VERSION,
        critic_model=critic_model,
        judge_model=judge_model,
        latency_ms=latency_ms,
        retry_recommended=retry_recommended,
    )


def _persist_verdict(
    db: Session,
    change_request_id: str,
    verdict: EvalVerdictSchema,
) -> "EvalVerdict | None":
    from app.services.evaluation.store import save_verdict
    return save_verdict(db, change_request_id, verdict)


# ── Fire-and-forget helper ──────────────────────────────────────────────────
#
# Phase 7 — many wiring points are inside sync FastAPI endpoints (e.g.
# clarifications submit/skip) where `asyncio.create_task` is unavailable.
# This helper picks the right dispatch automatically and always uses an
# isolated DB session so it cannot interfere with the caller's transaction.

async def _run_with_isolated_session(
    *,
    change_request_id: str,
    checkpoint_id: CheckpointId,
    source_artifacts: dict[str, dict],
    target_artifacts: dict[str, dict],
    source_artifact_ids: list[str] | None,
    target_artifact_ids: list[str] | None,
) -> None:
    eval_db: Session = SessionLocal()
    try:
        await run_advisory(
            db=eval_db,
            change_request_id=change_request_id,
            checkpoint_id=checkpoint_id,
            source_artifacts=source_artifacts,
            target_artifacts=target_artifacts,
            source_artifact_ids=source_artifact_ids or [],
            target_artifact_ids=target_artifact_ids or [],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Advisory eval task failed checkpoint=%s change=%s error=%s",
            checkpoint_id.value, change_request_id, exc,
        )
    finally:
        eval_db.close()


def fire_advisory_eval(
    *,
    change_request_id: str,
    checkpoint_id: CheckpointId,
    source_artifacts: dict[str, dict],
    target_artifacts: dict[str, dict],
    source_artifact_ids: list[str] | None = None,
    target_artifact_ids: list[str] | None = None,
) -> None:
    """Fire-and-forget an advisory evaluation.

    Safe to call from both async (WS / async handler) and sync (FastAPI sync
    endpoint) contexts. Never raises; failures are logged.
    """
    kwargs = dict(
        change_request_id=change_request_id,
        checkpoint_id=checkpoint_id,
        source_artifacts=source_artifacts,
        target_artifacts=target_artifacts,
        source_artifact_ids=source_artifact_ids,
        target_artifact_ids=target_artifact_ids,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        loop.create_task(_run_with_isolated_session(**kwargs))
        return

    def _thread_target() -> None:
        try:
            asyncio.run(_run_with_isolated_session(**kwargs))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Sync advisory eval failed checkpoint=%s change=%s error=%s",
                checkpoint_id.value, change_request_id, exc,
            )

    threading.Thread(
        target=_thread_target,
        name=f"eval-advisory-{checkpoint_id.value}",
        daemon=True,
    ).start()
