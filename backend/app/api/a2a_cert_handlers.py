# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound dispatch handlers for cert-related A2A task types.

These run inside POST /api/a2a/tasks/send (a2a.py:receive_task) when the
authenticated partner sends one of the cert-related task_types. Imported by
a2a.py and called as plain functions; the message audit row is already
created by receive_task before delegating here.
"""
import logging

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.phase_c import (
    A2AMessage, AssignmentStatus, ChangePartnerAssignment,
    CertDirection, CertRun, CertRunStatus, CertTestResult, CertTestStatus,
    CertTriage, TriageVerdict,
)
from app.models.base import generate_uuid, utcnow
from app.services.assignment_status import set_status

logger = logging.getLogger(__name__)


def _normalize_status(value: str) -> CertTestStatus:
    s = (value or "").lower()
    if s == "pass":
        return CertTestStatus.PASS
    if s == "fail":
        return CertTestStatus.FAIL
    if s == "skip":
        return CertTestStatus.SKIP
    return CertTestStatus.ERROR


def _normalize_direction(value: str) -> CertDirection:
    s = (value or "").lower()
    if s == "partner_to_npci":
        return CertDirection.PARTNER_TO_AUTHORITY
    return CertDirection.AUTHORITY_TO_PARTNER


async def _run_triage(cert_run_id: str, db_factory):
    """Background task: run AI triage on the failed results of a cert run."""
    from app.agents.cert_triage import triage_failed_tests

    db = db_factory()
    try:
        failed = db.scalars(
            select(CertTestResult).where(
                CertTestResult.cert_run_id == cert_run_id,
                CertTestResult.status == CertTestStatus.FAIL,
            )
        ).all()
        if not failed:
            return

        failed_data = [
            {
                "id": r.id,
                "test_case_id": r.test_case_id,
                "direction": r.direction.value if hasattr(r.direction, "value") else r.direction,
                "expected_response": r.expected_response,
                "actual_response": r.actual_response,
            }
            for r in failed
        ]

        verdicts = await triage_failed_tests(failed_data)

        for v in verdicts:
            result_id = v.get("test_result_id")
            verdict_str = v.get("verdict", "env_issue")
            reasoning = v.get("reasoning", "")

            test_result = next(
                (r for r in failed if r.id == result_id or r.test_case_id == result_id),
                None,
            )
            if not test_result:
                continue

            existing = db.scalars(
                select(CertTriage).where(CertTriage.cert_test_result_id == test_result.id)
            ).first()

            try:
                verdict_enum = TriageVerdict(verdict_str)
            except ValueError:
                verdict_enum = TriageVerdict.ENV_ISSUE

            if existing:
                existing.ai_verdict = verdict_enum
                existing.ai_reasoning = reasoning
            else:
                db.add(CertTriage(
                    id=generate_uuid(),
                    cert_test_result_id=test_result.id,
                    ai_verdict=verdict_enum,
                    ai_reasoning=reasoning,
                ))
        db.commit()
        logger.info("Auto-triage completed: cert_run_id=%s failures=%d", cert_run_id, len(failed))
    finally:
        db.close()


def process_cert_test_response(
    payload: dict,
    message: A2AMessage,
    db: Session,
    background_tasks: BackgroundTasks,
) -> str:
    """Handle inbound CERT_TEST_RESPONSE from the cert_engine partner.

    Looks up the originating CertRun by cert_run_id, upserts per-TC
    CertTestResult rows, recomputes summary, marks the run COMPLETED, and
    schedules AI triage if any failures.
    """
    cert_run_id = payload.get("cert_run_id")
    if not cert_run_id:
        message.status = "failed"
        db.commit()
        logger.warning("cert_test_response missing cert_run_id: message=%s", message.id)
        return "cert_test_response missing cert_run_id"

    cert_run = db.get(CertRun, cert_run_id)
    if not cert_run:
        message.status = "failed"
        db.commit()
        logger.warning(
            "cert_test_response references unknown CertRun: cert_run_id=%s message=%s",
            cert_run_id, message.id,
        )
        return f"CertRun '{cert_run_id}' not found"

    incoming = payload.get("results") or []
    passed = 0
    failed = 0
    skipped = 0

    # Replace any existing results for this run (idempotent on retransmit).
    existing_results = db.scalars(
        select(CertTestResult).where(CertTestResult.cert_run_id == cert_run.id)
    ).all()
    for r in existing_results:
        db.delete(r)
    db.flush()

    for tr in incoming:
        cert_status = _normalize_status(tr.get("status"))
        direction = _normalize_direction(tr.get("direction"))
        if cert_status == CertTestStatus.PASS:
            passed += 1
        elif cert_status == CertTestStatus.FAIL:
            failed += 1
        elif cert_status == CertTestStatus.SKIP:
            skipped += 1

        db.add(CertTestResult(
            id=generate_uuid(),
            cert_run_id=cert_run.id,
            test_case_id=tr.get("test_case_id"),
            direction=direction,
            status=cert_status,
            expected_response=tr.get("expected_response"),
            actual_response=tr.get("actual_response"),
            latency_ms=tr.get("latency_ms"),
        ))

    cert_run.total = len(incoming)
    cert_run.passed = passed
    cert_run.failed = failed
    cert_run.skipped = skipped
    cert_run.status = CertRunStatus.COMPLETED
    cert_run.completed_at = utcnow()

    # Update partner status. Two outcomes:
    #   all-pass → CERTIFIED
    #   any failures → stay at CERTIFYING until a re-run passes (or PO
    #                  manually intervenes). We do NOT promote to CERTIFIED
    #                  on failure even if triage marks them all env_issue.
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == cert_run.change_request_id,
            ChangePartnerAssignment.partner_id == cert_run.partner_id,
        )
    ).first()
    if assignment:
        if passed > 0 and failed == 0:
            set_status(
                assignment, AssignmentStatus.CERTIFIED, db,
                actor_partner_id=message.partner_id,
                reason=f"Cert run #{cert_run.run_number}: all {passed} TCs passed",
            )
        else:
            # Make sure we're at CERTIFYING (in case the run started before
            # this code was deployed, or the start path failed to set it).
            set_status(
                assignment, AssignmentStatus.CERTIFYING, db,
                actor_partner_id=message.partner_id,
                reason=f"Cert run #{cert_run.run_number} completed with {failed} failure(s)",
            )

    message.status = "completed"
    db.commit()

    if failed > 0:
        from app.core.database import SessionLocal
        background_tasks.add_task(_run_triage, cert_run.id, SessionLocal)

    logger.info(
        "CERT_TEST_RESPONSE processed: cert_run_id=%s total=%d passed=%d failed=%d skipped=%d",
        cert_run.id, len(incoming), passed, failed, skipped,
    )
    return f"Cert run {cert_run.id} updated: {passed}/{len(incoming)} passed"


def process_cert_acknowledgement(payload: dict, message: A2AMessage, db: Session) -> str:
    """Partner acknowledges CERTIFIED status. v1: just record."""
    message.status = "completed"
    db.commit()
    logger.info("CERT_ACKNOWLEDGEMENT recorded: message=%s", message.id)
    return "Certification acknowledgement recorded"


def process_defect_notice(payload: dict, message: A2AMessage, db: Session) -> str:
    """the Authority receives a defect notice from a partner (or, in the legacy direction
    where partner acknowledges a authority-issued defect notice). v1: just record."""
    message.status = "completed"
    db.commit()
    logger.info("DEFECT_NOTICE recorded: message=%s", message.id)
    return "Defect notice recorded"


def process_defect_resolution(payload: dict, message: A2AMessage, db: Session) -> str:
    """Partner reports they've resolved a defect; mark related triage rows.

    Looks up by triage_id (preferred) or test_result_id in payload.
    """
    triage_id = payload.get("triage_id")
    test_result_id = payload.get("test_result_id")

    triage = None
    if triage_id:
        triage = db.get(CertTriage, triage_id)
    elif test_result_id:
        triage = db.scalars(
            select(CertTriage).where(CertTriage.cert_test_result_id == test_result_id)
        ).first()

    if triage:
        triage.final_verdict = "resolved"
        message.status = "completed"
        db.commit()
        logger.info("DEFECT_RESOLUTION applied: triage=%s", triage.id)
        return f"Triage {triage.id} marked resolved"

    message.status = "completed"
    db.commit()
    logger.info("DEFECT_RESOLUTION recorded (no triage matched): message=%s", message.id)
    return "Defect resolution recorded"
