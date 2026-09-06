# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The durable, resumable, leased state machine (THE BOOK §3).

This is the *engine* that drives an agentic run between phases — the phase
*bodies* (clone, context, xsd, code, verify, review, push) are filled in by
S3–S13 and dispatched by the S13 orchestrator. Here we own:

* the legal phase transitions (§3 diagram);
* run creation with duplicate-run prevention (the partial-unique active-run
  index from migration M-D);
* leases (one worker drives a run; an expired lease is reclaimable);
* idempotent phase advancement + attempt counting;
* cooperative cancellation;
* the recovery sweep that reclaims crashed runs.

Every transition emits an ``agentic_events`` row via :mod:`agentic_events`, so
the run is fully reconstructable.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.agentic_events import emit_event
from app.core.config import settings
from app.models.agentic import (
    AgenticRun,
    AgenticPhase,
    AgenticStatus,
    TERMINAL_STATUSES,
)
from app.models.base import utcnow

logger = logging.getLogger("app.agentic")

P = AgenticPhase
_TERMINAL_PHASES = {P.COMPLETED, P.FAILED, P.GAVE_UP, P.CANCELLED}

# Legal transitions per the §3 state diagram (+ the §11 rebase_reverify path,
# which the prose specifies even though the diagram omits the node).
VALID_TRANSITIONS: dict[AgenticPhase, set[AgenticPhase]] = {
    P.PENDING:                 {P.WORKSPACE_READY, P.FAILED, P.CANCELLED},
    P.WORKSPACE_READY:         {P.CONTEXT_READY, P.FAILED, P.CANCELLED},
    # Phase B (kind='code') skips XSD discovery (schema already approved) → straight to CODE_CHANGE.
    # kind='analysis' (S2) branches to ANALYZING. kind='gov_*' (governance review
    # stages) branches to REVIEW — the stage reviews an already-built change.
    P.CONTEXT_READY:           {P.XSD_DISCOVERY, P.CODE_CHANGE, P.AWAITING_TSD_APPROVAL, P.ANALYZING, P.REVIEW, P.FAILED, P.CANCELLED},
    # Change-Analysis (kind='analysis'): read-only loop that pauses at the clarifications
    # batch gate and the plan-ratification gate, then completes. No manifest/push.
    P.ANALYZING:               {P.AWAITING_CLARIFICATIONS, P.AWAITING_PLAN_APPROVAL, P.FAILED, P.CANCELLED},
    P.AWAITING_CLARIFICATIONS: {P.ANALYZING, P.FAILED, P.CANCELLED},
    P.AWAITING_PLAN_APPROVAL:  {P.ANALYZING, P.COMPLETED, P.FAILED, P.CANCELLED},
    # XSD_DISCOVERY's propose pass stops at AWAITING_APPROACH_DECISION; Phase A's apply pass
    # stops at AWAITING_XSD_APPROVAL; a "full" run (the standalone quick-start codegen console —
    # no BRD/TSD anywhere in its flow) goes straight to CODE_CHANGE, WITHOUT the ADR-0005 TSD
    # gate — that gate is scoped to the kind='code' Phase-B path (CONTEXT_READY below), which
    # genuinely has a TSD to check. A "full" run has no TSD a human could ever approve, so
    # routing it through the gate would permanently wedge it at AWAITING_TSD_APPROVAL once
    # enforcement is turned on.
    P.XSD_DISCOVERY:           {P.AWAITING_APPROACH_DECISION, P.AWAITING_XSD_APPROVAL, P.CODE_CHANGE,
                                P.FAILED, P.CANCELLED},
    P.AWAITING_APPROACH_DECISION: {P.XSD_DISCOVERY, P.FAILED, P.CANCELLED},
    # AWAITING_XSD_APPROVAL → XSD_DISCOVERY is the refine loop (human requests changes).
    P.AWAITING_XSD_APPROVAL:   {P.XSD_DISCOVERY, P.CODE_CHANGE, P.AWAITING_TSD_APPROVAL, P.COMPLETED, P.FAILED, P.CANCELLED},
    # ADR-0005 — the run parks here when agentic_tsd_approval_gate_enforce=True and the
    # bound TechSpec is not APPROVED. A human approves the TSD (existing TSD conversation
    # panel, once auto-approve-on-generate covers most cases) then calls the
    # /agentic/runs/{id}/decide-tsd-approval endpoint, which re-checks and advances.
    P.AWAITING_TSD_APPROVAL:   {P.CODE_CHANGE, P.FAILED, P.CANCELLED},
    P.CODE_CHANGE:             {P.VERIFICATION, P.AWAITING_CODE_DECISION, P.AWAITING_SCHEMA_AMENDMENT,
                                P.FAILED, P.GAVE_UP, P.CANCELLED},
    # A3: human answers the code agent's decision question -> back to CODE_CHANGE.
    P.AWAITING_CODE_DECISION:  {P.CODE_CHANGE, P.FAILED, P.CANCELLED},
    # Fix 2: the code phase staged a change to the approved schema. Approve → the edit is
    # applied verbatim and code resumes with the amended schema; reject → code resumes with a
    # binding directive to implement around it. Either way the run goes back to CODE_CHANGE,
    # so an unexecutable schema fix can no longer deadlock the review loop.
    P.AWAITING_SCHEMA_AMENDMENT: {P.CODE_CHANGE, P.FAILED, P.CANCELLED},
    # After 3 failed auto-verifications VERIFICATION parks at AWAITING_VERIFY_DECISION
    # (human gate) instead of giving up; the human then retries (→CODE_CHANGE) or skips (→REVIEW).
    # → AWAITING_HUMAN_APPROVAL is the governance unverified-fixes park: a gov_* stage
    # whose fix budget is spent (or whose verifier could not run) freezes its fix-delta
    # manifest and parks at the human gate instead of failing.
    P.VERIFICATION:            {P.CODE_CHANGE, P.REVIEW, P.AWAITING_VERIFY_DECISION, P.AWAITING_HUMAN_APPROVAL, P.GAVE_UP, P.FAILED, P.CANCELLED},
    P.AWAITING_VERIFY_DECISION:{P.CODE_CHANGE, P.REVIEW, P.FAILED, P.CANCELLED},
    # → COMPLETED is the governance clean short-circuit: a gov_* stage whose review
    # staged no fixes completes without a human gate (nothing changed → nothing to approve).
    P.REVIEW:                  {P.CODE_CHANGE, P.AWAITING_HUMAN_APPROVAL, P.COMPLETED, P.FAILED, P.CANCELLED},
    # → COMPLETED is the deferred-push approval: human approved the manifest but chose
    # to push later, so the run completes without the remote write.
    P.AWAITING_HUMAN_APPROVAL: {P.PUSHING, P.REBASE_REVERIFY, P.CANCELLED, P.COMPLETED},
    # → PUSHING lets the push task RECOVER a run parked in rebase_reverify: it undoes any
    # leftover push commit and re-attempts the push, instead of crashing on an illegal
    # transition and wedging the run there forever.
    P.REBASE_REVERIFY:         {P.PUSHING, P.AWAITING_HUMAN_APPROVAL, P.FAILED, P.CANCELLED},
    P.PUSHING:                 {P.COMPLETED, P.REBASE_REVERIFY, P.FAILED, P.CANCELLED},
    # Terminal phases have no outgoing edges — except COMPLETED → PUSHING, which
    # re-opens a deferred-push run for its single remote write (manifest already
    # approved; the workspace is GC-guarded until pushed).
    P.COMPLETED: {P.PUSHING}, P.FAILED: set(), P.GAVE_UP: set(), P.CANCELLED: set(),
}


class TransitionError(Exception):
    """Raised when an illegal phase transition is attempted."""


def _can_transition(frm: str, to: AgenticPhase) -> bool:
    try:
        return to in VALID_TRANSITIONS[AgenticPhase(frm)]
    except (KeyError, ValueError):
        return False


# ── Run creation (duplicate-run prevention) ──────────────────────────────────────

def create_run(
    db: Session,
    change_request_id: str,
    selected_repo_ids: list[str],
    *,
    kind: str = "full",
    parent_run_id: str | None = None,
    workspace_run_id: str | None = None,
    created_by: str | None = None,
) -> tuple[AgenticRun, bool]:
    """Create a new run, or return the existing active one (§3).

    The partial-unique index ``uq_agentic_runs_active`` enforces at most one
    ``status='active'`` run per change. A race loses the INSERT with an
    ``IntegrityError``; we roll back and return the winner. Returns
    ``(run, created)`` — ``created=False`` means an active run already existed.

    ``kind`` splits the pipeline (THE BOOK v3.4): ``"full"`` (legacy), ``"xsd"``
    (Phase A), or ``"code"`` (Phase B, which sets ``parent_run_id`` +
    ``workspace_run_id`` to adopt Phase A's workspace).
    """
    existing = _active_run(db, change_request_id)
    if existing is not None:
        return existing, False

    run = AgenticRun(
        change_request_id=change_request_id,
        phase=P.PENDING.value,
        status=AgenticStatus.ACTIVE.value,
        selected_repo_ids=list(selected_repo_ids),
        attempts_json={},
        kind=kind,
        parent_run_id=parent_run_id,
        workspace_run_id=workspace_run_id,
        created_by=created_by,
    )
    db.add(run)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = _active_run(db, change_request_id)
        if winner is None:  # pragma: no cover — only if the row went terminal mid-race
            raise
        return winner, False

    emit_event(db, run.id, "run_created",
               {"change_request_id": change_request_id, "selected_repo_ids": list(selected_repo_ids),
                "kind": kind, "parent_run_id": parent_run_id})
    return run, True


def _active_run(db: Session, change_request_id: str) -> AgenticRun | None:
    return (
        db.query(AgenticRun)
        .filter(
            AgenticRun.change_request_id == change_request_id,
            AgenticRun.status == AgenticStatus.ACTIVE.value,
        )
        .one_or_none()
    )


# ── Leases (§3/§6) ────────────────────────────────────────────────────────────

def acquire_lease(db: Session, run_id: str, owner: str, ttl_seconds: int | None = None) -> bool:
    """Atomically take/renew the lease. True if this worker now holds it.

    The single guarded UPDATE is atomic in Postgres, so two workers racing for
    the same expired-lease run cannot both win.
    """
    ttl = ttl_seconds or settings.agentic_lease_ttl_seconds
    now = utcnow()
    expires = now + timedelta(seconds=ttl)
    result = db.execute(
        update(AgenticRun)
        .where(
            AgenticRun.id == run_id,
            AgenticRun.status == AgenticStatus.ACTIVE.value,
            (
                (AgenticRun.lease_owner.is_(None))
                | (AgenticRun.lease_expires_at < now)
                | (AgenticRun.lease_owner == owner)
            ),
        )
        .values(lease_owner=owner, lease_expires_at=expires, updated_at=now)
    )
    return result.rowcount == 1


def renew_lease(db: Session, run_id: str, owner: str, ttl_seconds: int | None = None) -> bool:
    """Heartbeat: extend the lease only if this worker still owns it."""
    ttl = ttl_seconds or settings.agentic_lease_ttl_seconds
    now = utcnow()
    result = db.execute(
        update(AgenticRun)
        .where(AgenticRun.id == run_id, AgenticRun.lease_owner == owner)
        .values(lease_expires_at=now + timedelta(seconds=ttl), updated_at=now)
    )
    return result.rowcount == 1


def release_lease(db: Session, run_id: str, owner: str | None = None) -> None:
    """Release the lease. When ``owner`` is given, release ONLY if this worker still
    holds it — a superseded (zombie) driver parking a run it no longer owns must not
    stomp the lease of the driver that legitimately reclaimed it (§3)."""
    q = update(AgenticRun).where(AgenticRun.id == run_id)
    if owner is not None:
        q = q.where(AgenticRun.lease_owner == owner)
    db.execute(q.values(lease_owner=None, lease_expires_at=None, updated_at=utcnow()))


# ── Transitions ────────────────────────────────────────────────────────────────

def advance(db: Session, run: AgenticRun, to_phase: AgenticPhase) -> AgenticRun:
    """Move a run to a non-terminal phase. Idempotent if already there.

    Bumps ``attempts_json[to_phase]`` so the iteration / verify caps (§3) can be
    enforced by the caller. Terminal transitions go through ``mark_terminal``.
    """
    if to_phase in _TERMINAL_PHASES:
        raise TransitionError(f"use mark_terminal() for terminal phase {to_phase.value}")
    if run.phase == to_phase.value:
        return run  # idempotent re-entry — phase already current
    if not _can_transition(run.phase, to_phase):
        raise TransitionError(f"illegal transition {run.phase} -> {to_phase.value}")

    attempts = dict(run.attempts_json or {})
    attempts[to_phase.value] = attempts.get(to_phase.value, 0) + 1

    frm = run.phase
    run.phase = to_phase.value
    run.attempts_json = attempts  # reassign so SQLAlchemy detects the JSON change
    run.updated_at = utcnow()
    emit_event(db, run.id, "phase_changed",
               {"from": frm, "to": to_phase.value, "attempt": attempts[to_phase.value]})
    return run


def mark_terminal(
    db: Session,
    run: AgenticRun,
    status: AgenticStatus,
    error: str | None = None,
) -> AgenticRun:
    """Drive a run to a terminal state, release its lease, free it for GC (§3/§14)."""
    if status not in TERMINAL_STATUSES:
        raise TransitionError(f"{status.value} is not a terminal status")
    terminal_phase = AgenticPhase(status.value)
    if run.phase != terminal_phase.value and not _can_transition(run.phase, terminal_phase):
        raise TransitionError(f"illegal terminal transition {run.phase} -> {terminal_phase.value}")

    run.phase = terminal_phase.value
    run.status = status.value
    if error:
        run.error = error
    run.lease_owner = None
    run.lease_expires_at = None
    run.updated_at = utcnow()
    emit_event(db, run.id, "run_terminal", {"status": status.value, "error": error})
    return run


def request_cancel(db: Session, run: AgenticRun) -> AgenticRun:
    """Set the cooperative cancel flag — honoured at the next phase boundary (§3)."""
    run.cancel_requested = True
    run.updated_at = utcnow()
    emit_event(db, run.id, "cancel_requested", {})
    return run


def check_cancel(run: AgenticRun) -> bool:
    return bool(run.cancel_requested)


def honour_cancel(db: Session, run: AgenticRun) -> AgenticRun:
    """Terminate a run whose ``cancel_requested`` flag is being honoured.

    A re-opened deferred-push run (``phase='completed'``, ``status='active'``) has no
    legal edge to CANCELLED — close it back to COMPLETED instead: it already completed
    once, and only the re-opened push is being abandoned. Every other phase goes to
    CANCELLED. Consumes the flag so a future deferred-push re-open isn't instantly
    killed by the stale request."""
    status = (AgenticStatus.COMPLETED if run.phase == AgenticPhase.COMPLETED.value
              else AgenticStatus.CANCELLED)
    run.cancel_requested = False
    return mark_terminal(db, run, status)


# ── Recovery sweep (the `agentic.recover` beat body, §3/§14) ──────────────────────

def recover_runs(db: Session, ttl_seconds: int | None = None) -> list[str]:
    """Reclaim runs whose lease expired (crashed worker). Returns recovered ids.

    Releasing the lease makes the run re-claimable; the S13 orchestrator (or the
    next worker) resumes it *from its persisted phase* — phases are idempotent
    and reset the clone to base SHA before re-running, so no work is duplicated.
    """
    now = utcnow()
    stale = (
        db.query(AgenticRun)
        .filter(
            AgenticRun.status == AgenticStatus.ACTIVE.value,
            AgenticRun.lease_owner.isnot(None),
            AgenticRun.lease_expires_at < now,
        )
        .all()
    )
    recovered: list[str] = []
    for run in stale:
        prev_owner = run.lease_owner
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = now
        emit_event(db, run.id, "lease_expired_recovered",
                   {"phase": run.phase, "prev_owner": prev_owner})
        recovered.append(run.id)
    return recovered
