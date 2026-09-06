# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The suite join (ITA I-7, §3.8) — harness-agnostic, beside the dispatch seam.

With partner-initiated cases executing on the partner's side, a run can no
longer complete synchronously: it must wait for results that arrive over A2A,
or for the suite deadline. This module owns EVERY terminal action of such a
run — the COMPLETED flip, the assignment decision, the flow's closing trigger
and the signoff — driven entirely from PERSISTED state (`cert_runs` rows +
`cert_flow_states`), which is what makes a restart mid-wait a non-event: the
next bank report or deadline sweep picks up exactly where the process died.

Deliberately NOT inside `orchestrate_cert_run_precert_engine`: that function
is the precert harness's driver and SIM-8 deletes it; the join is suite
bookkeeping every harness needs, so it lives beside `certification_dispatch`
(the plan's §12.1 pins this, and `test_certification_dispatch.py` pins the
seam). Nothing here imports a harness or a pack.

WHAT COUNTS AS PENDING: a `CertTestResult` whose `actual_response` carries the
`not_reported` marker the orchestrator stamps at dispatch. A bank report
overwrites the marker (`process_cert_case_result_report`); a genuine
authority-side execution ERROR never has it. At deadline the marker STAYS —
the unreported case remains recorded explicitly, and it blocks certification
(a placeholder is not a pass).
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.config import settings
from app.models.base import utcnow
from app.models.phase_c import (
    AssignmentStatus,
    CertRun,
    CertRunStatus,
    CertTestStatus,
    ChangePartnerAssignment,
    PartnerAgent,
)
from app.services.assignment_status import set_status

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["pending_case_ids", "deadline_for", "finalize_run",
           "check_and_finalize", "sweep_expired"]


def pending_case_ids(run: CertRun) -> list[str]:
    """Case ids still awaiting the partner's report, from the rows alone."""
    return sorted(
        r.test_case_id or ""
        for r in run.results
        if isinstance(r.actual_response, dict) and r.actual_response.get("not_reported")
    )


def deadline_for(run: CertRun):
    """The suite deadline — derived, not stored: started_at + the configured
    window (migrations 0133–0135 stay reserved for the SIM/ITA columns).

    A naive `started_at` is read as UTC: the column is timezone-aware and
    every writer stamps UTC, but the SQLite harness strips tzinfo on the way
    back out and a naive/aware comparison raises."""
    started = run.started_at
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started + timedelta(seconds=float(settings.cert_suite_deadline_s))


async def finalize_run(db: "Session", run: CertRun, *, reason: str) -> dict:
    """The terminal bookkeeping for a joined (or expired) run. Idempotent —
    a COMPLETED run is left untouched, so a bank report racing the deadline
    sweep cannot finalize twice.

    Certification requires EVERY case passed (`passed == total`): unreported
    cases are ERROR-class and block it, exactly like a genuine failure would.
    The signoff (and the flow's closing trigger) fire only on a clean sweep.
    """
    if run.status == CertRunStatus.COMPLETED:
        return {"finalized": False, "reason": "already completed"}

    rows = run.results
    run.total = len(rows)
    run.passed = sum(1 for r in rows if r.status == CertTestStatus.PASS)
    run.failed = sum(1 for r in rows if r.status == CertTestStatus.FAIL)
    run.skipped = sum(1 for r in rows
                      if r.status in (CertTestStatus.SKIP, CertTestStatus.ERROR))
    unreported = pending_case_ids(run)
    run.status = CertRunStatus.COMPLETED
    run.completed_at = utcnow()
    all_passed = run.failed == 0 and run.total > 0 and run.passed == run.total
    db.commit()

    logger.info(
        "cert_join finalize run=%s change=%s reason=%r pass=%d fail=%d "
        "unreported=%d all_passed=%s",
        run.id, run.change_request_id, reason, run.passed, run.failed,
        len(unreported), all_passed,
    )

    if all_passed:
        assignment = db.scalars(
            select(ChangePartnerAssignment).where(
                ChangePartnerAssignment.change_request_id == run.change_request_id,
                ChangePartnerAssignment.partner_id == run.partner_id,
            )
        ).first()
        if assignment:
            set_status(
                assignment, AssignmentStatus.CERTIFIED, db,
                actor_partner_id=run.partner_id,
                reason=f"Suite joined all-PASS ({run.passed}/{run.total}) — {reason}",
            )
            db.commit()

        # Close the persisted flow: RUNNING → COMPLETED. Loaded fresh — the
        # dispatching process may be long gone (restart-safe by construction).
        from app.services.cert_agent import flow_store
        from app.services.cert_agent.flow import FlowState
        from app.services.cert_agent.state_machine import Phase, Trigger

        row = flow_store.load(db, run.cflow_id) if run.cflow_id else None
        if row is not None:
            flow = FlowState(run.cflow_id, phase=Phase(row.phase),
                             persist=flow_store.persister(
                                 db, change_request_id=run.change_request_id,
                                 partner_id=run.partner_id,
                                 current_round=row.current_round))
            flow.fire(Trigger.all_cases_passed)

        partner = db.get(PartnerAgent, run.partner_id)
        if partner is not None:
            try:
                # The WIRE enum, not the legacy DB enum in models.phase_c —
                # only the protocol's carries the Part B signoff member.
                from app.a2a_common import protocol as _proto
                from app.services.a2a_client import send_task_to_partner

                await send_task_to_partner(
                    partner=partner,
                    task_type=_proto.A2ATaskType.CERT_SIGNOFF_NOTIFICATION,
                    payload={
                        "summary": (f"All {run.total} case(s) passed across both "
                                    f"classes — certification signed off ({reason})."),
                        "all_passed": True,
                        "cert_run_id": run.id,
                        "signed_off_at": utcnow().isoformat(),
                    },
                    db=db,
                    change_request_id=run.change_request_id,
                    cflow_id=run.cflow_id,
                    cert_attempt=run.run_number,
                )
                db.commit()
            except Exception:  # noqa: BLE001 — the DB verdict must survive a wire failure
                logger.exception("cert_join: signoff send failed for run=%s", run.id)

    return {"finalized": True, "all_passed": all_passed,
            "unreported": unreported, "reason": reason}


async def check_and_finalize(run_id: str) -> dict:
    """Bank-report hook: finalize iff nothing is pending. Opens its own
    session — it runs as a background task after the inbound handler returns."""
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        run = db.get(CertRun, run_id)
        if run is None or run.status == CertRunStatus.COMPLETED:
            return {"finalized": False, "reason": "no run or already completed"}
        pending = pending_case_ids(run)
        if pending:
            logger.info("cert_join: run=%s still awaiting %d case(s)",
                        run_id, len(pending))
            return {"finalized": False, "reason": f"{len(pending)} pending"}
        return await finalize_run(db, run, reason="all cases reported")
    finally:
        db.close()


async def sweep_expired(db: "Session" | None = None) -> dict:
    """Deadline sweep: finalize every RUNNING run past its suite deadline.

    Everything is re-derived from the database, so this is also the restart
    story: a process that died mid-wait needs no recovery step — the rows say
    what is pending, `started_at` says when patience runs out.
    """
    from app.core.database import SessionLocal

    own = db is None
    db = db or SessionLocal()
    try:
        now = utcnow()
        candidates = db.scalars(
            select(CertRun).where(CertRun.status == CertRunStatus.RUNNING)
        ).all()
        finalized = 0
        for run in candidates:
            if not _join_managed(run):
                # A RUNNING run with no join fingerprints is some OTHER
                # harness mid-flight (the cert-agent path also parks runs at
                # RUNNING while it works) — force-finalizing it would end a
                # live run. Not ours; leave it.
                continue
            if not pending_case_ids(run):
                # Fully reported but still RUNNING: the report hook was
                # missed (restart between the upsert and the bg task, or no
                # bg at the call site) — join it now, deadline or not.
                await finalize_run(db, run, reason="joined by sweep")
                finalized += 1
                continue
            if run.started_at is None or deadline_for(run) > now:
                continue
            await finalize_run(db, run, reason="suite deadline reached")
            finalized += 1
        return {"checked": len(candidates), "finalized": finalized}
    finally:
        if own:
            db.close()


def _join_managed(run: CertRun) -> bool:
    """Whether this run is the JOIN's to finalize: it carries a not-reported
    marker (still awaiting) or a bank-reported row (was awaiting). A run with
    neither belongs to a synchronous harness and is out of bounds."""
    for r in run.results:
        response = r.actual_response
        if isinstance(response, dict) and (
                response.get("not_reported") or response.get("reporter") == "bank"):
            return True
    return False
