# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A task handlers for the Authority platform.

Slice 8 of the unified A2A SDK refactor — these are the handler
bodies that used to live in `app.api.a2a` and `app.api.a2a_cert_handlers`,
called by both the legacy `POST /api/a2a/tasks/send` router (now deleted)
and the SDK executor. Slice 8 deletes the legacy router; the SDK
executor (`app.a2a_common.authority_executor.AuthorityAgentExecutor`) is now the
sole caller.

Why a sibling module instead of inlining into the executor: the executor
is the dispatch layer; these are the per-task-type business logic. Keeps
the executor short (one branch per task_type, ~20 lines) and makes each
handler unit-testable without spinning up the SDK Task lifecycle.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import BackgroundTasks
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.base import generate_uuid, utcnow
from app.models.phase_c import (
    A2AMessage,
    AssignmentStatus,
    Blocker,
    BlockerSeverity,
    BlockerStatus,
    CertDirection,
    CertRun,
    CertRunStatus,
    CertTestResult,
    CertTestStatus,
    CertTriage,
    CertWaiver,
    ChangePartnerAssignment,
    CounterProposal,
    CounterProposalStatus,
    PartnerAgent,
    PartnerProgress,
    ProgressStep,
    TriageVerdict,
)
from app.services.assignment_status import derive_progress_status, set_status

logger = logging.getLogger(__name__)

# Labels that positively identify a NON-production deployment. Anything else —
# including an unset or unrecognised value — counts as production.
_NON_PRODUCTION_ENVS = frozenset({"development", "dev", "local", "test"})

# Upper bound on per-TC results one cert_test_response may carry.
_MAX_CERT_RESULTS_PER_MESSAGE = 5000


def _is_production() -> bool:
    """True unless the deployment explicitly declares itself non-production.

    Reads BOTH environment knobs deliberately. `settings.app_env` binds to
    ``APP_ENV``, which no compose file in this repository sets; what compose sets
    is ``ENVIRONMENT`` (docker-compose.yml:51, ``ENVIRONMENT: ${ENVIRONMENT:-uat}``),
    and `Settings` is ``extra="ignore"`` so that value is silently discarded.
    A security gate keyed on `app_env` alone would therefore sit in development
    posture on every deployment, which is exactly the failure this guard exists
    to avoid.

    Fail-closed by construction: the caller must opt OUT of production, not into
    it, so a typo or an unset variable errs toward refusing the dangerous path.
    """
    import os

    from app.core.config import settings

    declared = [
        raw.strip().lower()
        for raw in (os.environ.get("ENVIRONMENT"), getattr(settings, "app_env", None))
        if raw and raw.strip()
    ]
    if not declared:
        return True                                   # nothing declared → production
    # Every label that IS set must say non-production. `ENVIRONMENT=uat` with
    # `APP_ENV` unset therefore counts as production, which is the intent.
    return not all(label in _NON_PRODUCTION_ENVS for label in declared)


# Required ProgressStep order — used by both _process_milestone_update and
# _process_readiness_declaration. Same as the legacy module's STEP_ORDER.
STEP_ORDER = [
    ProgressStep.DESIGN_COMPLETED,
    ProgressStep.CODING_COMPLETED,
    ProgressStep.TESTING_COMPLETED,
]

# Protocol v1 `milestone_update` carries (milestone, state) instead of the
# legacy single `step`. A milestone only advances assignment progress when its
# state is "completed"; other states (in_progress / at_risk / delayed) are
# informational and recorded as a no-op ack.
_MILESTONE_TO_STEP = {
    "design":  ProgressStep.DESIGN_COMPLETED,
    "coding":  ProgressStep.CODING_COMPLETED,
    "testing": ProgressStep.TESTING_COMPLETED,
}


# ── Partner change-lifecycle handlers ────────────────────────────────────────


def process_milestone_update(
    partner_id: str,
    change_id: str,
    payload: dict,
    message: A2AMessage,
    db: Session,
) -> str:
    """Record partner's implementation milestone (protocol v1 `milestone_update`).

    Payload: `{milestone: design|coding|testing, state: in_progress|completed|...,
    version_implementing?, notes?, risks?[]}`. Only `state="completed"` advances
    a ProgressStep; other states are acknowledged without state change.

    Validates step ordering — can't complete a later milestone before earlier
    ones. Auto-derives `assignment.status`; back-fills an ACCEPTED transition
    when a partner skips the explicit ack and completes design/coding first."""
    milestone = str(payload.get("milestone") or "").strip()
    state = str(payload.get("state") or "completed").strip()

    if milestone not in _MILESTONE_TO_STEP:
        message.status = "failed"
        db.commit()
        return f"Invalid milestone: '{milestone}'. Valid: {list(_MILESTONE_TO_STEP)}"

    # Non-terminal states are informational — record the message, don't advance.
    if state != "completed":
        message.status = "completed"
        db.commit()
        return f"Milestone '{milestone}' state '{state}' noted (no progress advance)"

    step = _MILESTONE_TO_STEP[milestone]
    step_value = step.value

    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()

    if not assignment:
        message.status = "failed"
        db.commit()
        return "Partner not assigned to this change request"

    existing_steps = db.scalars(
        select(PartnerProgress)
        .where(PartnerProgress.assignment_id == assignment.id)
        .order_by(PartnerProgress.reported_at)
    ).all()
    reported = {p.step for p in existing_steps}

    step_idx = STEP_ORDER.index(step)
    for prev_step in STEP_ORDER[:step_idx]:
        if prev_step not in reported:
            message.status = "failed"
            db.commit()
            return f"Cannot report '{step_value}' — must report '{prev_step.value}' first"

    if step in reported:
        message.status = "completed"
        db.commit()
        return f"Step '{step_value}' already reported"

    progress = PartnerProgress(
        id=generate_uuid(),
        assignment_id=assignment.id,
        step=step,
        notes=payload.get("notes"),
    )
    db.add(progress)

    completed_steps = list(reported) + [step]
    new_status = derive_progress_status(completed_steps)
    if new_status is not None:
        current = assignment.status
        current_value = current.value if hasattr(current, "value") else current
        if current_value == "received":
            set_status(
                assignment, AssignmentStatus.ACCEPTED, db,
                actor_partner_id=partner_id,
                reason="Auto-accepted on first progress update (partner skipped explicit ack).",
            )
        set_status(
            assignment, new_status, db,
            actor_partner_id=partner_id,
            reason=f"Reported {step_value}",
        )

    message.status = "completed"
    db.commit()

    logger.info(
        "Status update recorded: change=%s partner=%s step=%s",
        change_id, partner_id, step_value,
    )
    return f"Step '{step_value}' recorded successfully"


# Cert-channel lifecycle status → AssignmentStatus. Protocol v1: deployed/tested
# are implementation progress owned by milestone_update — they no longer flip
# the assignment here (see _CERT_STATUS_NOOP). Only received + the
# ready_for_certification trigger remain meaningful on the cert wire.
_CERT_STATUS_TO_ASSIGNMENT = {
    "received":                AssignmentStatus.RECEIVED,
    "ready_for_certification": AssignmentStatus.READY_FOR_CERTIFICATION,
}

# Accepted but no-op: implementation progress is tracked via milestone_update.
# Kept here (rather than rejected) for back-compat with partners still emitting
# them during the migration window.
_CERT_STATUS_NOOP = {"deployed", "tested"}


def process_cert_status_update(
    partner_id: str,
    change_id: str,
    payload: dict,
    message: A2AMessage,
    db: Session,
) -> str:
    """Inbound cert lifecycle status update from a partner.

    Wire payload (post-unwrap):
      { status: received|deployed|tested|ready_for_certification,
        change_id,
        role?,            # only on ready_for_certification
        test_data? }      # only on ready_for_certification

    Behaviour:
      * Validate status value.
      * Set ChangePartnerAssignment.status to the matching enum value.
      * No prerequisite checks — the partner UI is responsible for
        enforcing the linear order (received → deployed → tested →
        ready). Backend trusts the wire because each transition fires
        independently from a button click.
      * Final 'ready_for_certification' DOES NOT itself trigger the
        cert orchestrator — the executor branch above does that based
        on the same payload, so the orchestrator sees the same shape
        as the legacy READINESS_DECLARATION path.
    """
    status_str = (payload or {}).get("status", "").strip().lower()
    if not status_str:
        message.status = "failed"
        db.commit()
        return "Missing required field 'status'"
    # deployed/tested are tracked via milestone_update — accept as a no-op.
    if status_str in _CERT_STATUS_NOOP:
        message.status = "completed"
        db.commit()
        logger.info(
            "Cert status '%s' acknowledged as no-op (tracked via milestone_update): change=%s",
            status_str, change_id,
        )
        return f"Cert status '{status_str}' acknowledged (no-op; tracked via milestone_update)"
    target = _CERT_STATUS_TO_ASSIGNMENT.get(status_str)
    if target is None:
        message.status = "failed"
        db.commit()
        return (
            f"Invalid cert status '{status_str}'. Valid: "
            f"{list(_CERT_STATUS_TO_ASSIGNMENT)}"
        )

    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()
    if not assignment:
        message.status = "failed"
        db.commit()
        return "Partner not assigned to this change request"

    # Readiness gate: 'ready_for_certification' is the cert-run trigger — the
    # executor fires orchestrate_cert_run when this handler sets
    # message.status=='completed' for this status. It MUST enforce the same
    # milestone prerequisite as the READINESS_DECLARATION path, otherwise a
    # partner can jump straight to a real cert run via the cert stepper without
    # ever reporting design/coding/testing. Failing here (status!='completed')
    # also blocks the orchestrator trigger in the executor.
    if status_str == "ready_for_certification":
        reported = {
            p.step for p in db.scalars(
                select(PartnerProgress).where(PartnerProgress.assignment_id == assignment.id)
            ).all()
        }
        for step in STEP_ORDER:
            if step not in reported:
                message.status = "failed"
                db.commit()
                return (
                    f"Cannot mark ready for certification — milestone "
                    f"'{step.value}' not yet reported"
                )

    set_status(
        assignment, target, db,
        actor_partner_id=partner_id,
        reason=f"Cert status update: {status_str}",
    )
    message.status = "completed"
    db.commit()

    logger.info(
        "Cert status updated: change=%s partner=%s status=%s",
        change_id, partner_id, status_str,
    )
    return f"Cert status set to '{status_str}'"


def process_readiness_declaration(
    partner_id: str,
    change_id: str,
    message: A2AMessage,
    db: Session,
) -> str:
    """Mark partner as ready for certification. Requires all 3 ProgressSteps."""
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()

    if not assignment:
        message.status = "failed"
        db.commit()
        return "Partner not assigned to this change request"

    existing_steps = db.scalars(
        select(PartnerProgress)
        .where(PartnerProgress.assignment_id == assignment.id)
    ).all()
    reported = {p.step for p in existing_steps}

    for step in STEP_ORDER:
        if step not in reported:
            message.status = "failed"
            db.commit()
            return f"Cannot declare readiness — step '{step.value}' not yet reported"

    set_status(
        assignment, AssignmentStatus.READY_FOR_CERTIFICATION, db,
        actor_partner_id=partner_id,
        reason="Partner declared readiness",
    )
    message.status = "completed"
    db.commit()

    logger.info("Readiness declared: change=%s partner=%s", change_id, partner_id)
    return "Partner declared ready for certification"


async def process_proposal_acknowledged(
    partner_id: str,
    change_id: str,
    message: A2AMessage,
    db: Session,
) -> str:
    """Partner auto-emitted PROPOSAL_ACKNOWLEDGED on receipt.

    Per the rollout-doc convention this is the non-repudiation receipt:
    partner echoes back kit_files_received[] with checksum_verified
    flags so we can prove the bytes arrived intact. NO assignment-status
    transition — auto-ack just confirms the kit landed; the explicit
    accept (CHANGE_ACKNOWLEDGEMENT) is what flips to ACCEPTED.

    The structured payload is persisted on assignment.acceptance_meta
    under `acknowledged` so the explicit-accept handler can layer its
    own block alongside without overwriting.
    """
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()

    if not assignment:
        message.status = "failed"
        db.commit()
        return "Partner not assigned to this change request"

    payload = (message.payload or {}).get("payload") or {}
    meta = dict(assignment.acceptance_meta or {})
    meta["acknowledged"] = {
        "received_at":         payload.get("received_at"),
        "in_response_to":      payload.get("in_response_to"),
        "version_received":    payload.get("version_received"),
        "kit_files_received":  payload.get("kit_files_received") or [],
        # additive checksum receipt (spec's string list can't carry it)
        "kit_files_verified":  payload.get("kit_files_verified") or [],
        "review_phase":        payload.get("review_phase"),
        "estimated_response_by": payload.get("estimated_response_by"),
    }
    assignment.acceptance_meta = meta

    message.status = "completed"

    # A round IS the current kit version's 24h window (round N == v(N)) — the
    # window opens when the partner receives/acknowledges that version's kit.
    _round_was_created = False
    _round_number = 1
    try:
        from app.services.negotiation_extended import create_round_state
        from app.models.change_request import ChangeRequest as _CR
        _ch = db.get(_CR, change_id)
        _round_number = (getattr(_ch, "negotiation_version", 1) or 1) if _ch else 1
        _state, _round_was_created = create_round_state(
            change_id, partner_id, round_number=_round_number, db=db,
        )
    except Exception:
        logger.exception("Failed to create round state for change=%s partner=%s — continuing", change_id, partner_id)

    db.commit()

    # If the ack just opened round N (as opposed to hitting the idempotent
    # branch), notify the partner over A2A. The opened_reason is "initial_ack"
    # here because this handler fires on the partner's proposal_acknowledged
    # — the initial round for the current kit version.
    if _round_was_created:
        try:
            from app.services.negotiation_extended import send_round_opened
            await send_round_opened(
                change_request_id=change_id,
                partner_id=partner_id,
                round_number=_round_number,
                opened_reason="initial_ack",
                db=db,
            )
        except Exception:
            logger.exception(
                "send_round_opened(initial_ack) failed for change=%s partner=%s round=%d",
                change_id, partner_id, _round_number,
            )

    logger.info(
        "Proposal acknowledged: change=%s partner=%s files=%d",
        change_id, partner_id, len(meta["acknowledged"]["kit_files_received"]),
    )
    return "Proposal acknowledgement recorded"


def process_change_acknowledgement(
    partner_id: str,
    change_id: str,
    message: A2AMessage,
    db: Session,
) -> str:
    """Partner formally accepts the change → assignment.status = ACCEPTED.

    Reads the structured PROPOSAL_ACCEPTANCE payload (decision,
    accepted_by, internal_change_advisory_ref, estimated_phase_timeline)
    and persists it on assignment.acceptance_meta under `accepted`.
    Coexists with the `acknowledged` block written by
    process_proposal_acknowledged; both can be present.
    """
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()

    if not assignment:
        message.status = "failed"
        db.commit()
        return "Partner not assigned to this change request"

    payload = (message.payload or {}).get("payload") or {}
    meta = dict(assignment.acceptance_meta or {})
    meta["accepted"] = {
        "decision":                     payload.get("decision", "ACCEPT"),
        "accepted_at":                  payload.get("accepted_at"),
        "version_accepted":             payload.get("version_accepted"),
        "accepted_by":                  payload.get("accepted_by"),
        "internal_change_advisory_ref": payload.get("internal_change_advisory_ref"),
        "implementation_kickoff_date":  payload.get("implementation_kickoff_date"),
        "estimated_phase_timeline":     payload.get("estimated_phase_timeline"),
    }
    assignment.acceptance_meta = meta

    set_status(
        assignment, AssignmentStatus.ACCEPTED, db,
        actor_partner_id=partner_id,
        reason="Partner acknowledged change",
    )

    # Partner responded within their window → close the open round so
    # the silent-acceptance sweep won't later auto-accept on their behalf.
    try:
        from app.services.negotiation_extended import mark_round_responded
        mark_round_responded(change_id, partner_id, db)
    except Exception:
        logger.exception("Failed to mark round responded (acceptance) change=%s partner=%s", change_id, partner_id)

    message.status = "completed"
    db.commit()

    logger.info("Change acknowledged: change=%s partner=%s", change_id, partner_id)
    return "Change acknowledgement recorded"


MAX_NEGOTIATION_ROUNDS = 50


def process_counter_proposal(
    partner_id: str,
    change_id: str,
    payload: dict,
    message: A2AMessage,
    db: Session,
) -> str:
    """Partner sent a structured COUNTER_PROPOSAL — create a tracked
    CounterProposal row so the PM has explicit accept/reject controls.

    Doesn't touch assignment.status; instead the API layer's state
    transition guards check for any open counter-proposals before
    allowing progression past ACCEPTED.
    """
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()
    if not assignment:
        message.status = "failed"
        db.commit()
        return "Partner not assigned to this change request"

    # The negotiation round = the current published kit version's 24h window
    # (round N == v(N)) — NOT a per-message counter. Every counter raised within
    # a version's window shares the same round number.
    from app.models.change_request import ChangeRequest as _CR
    _change_row = db.get(_CR, change_id)
    current_round = (getattr(_change_row, "negotiation_version", 1) or 1) if _change_row else 1

    # Anti-abuse safety cap on total counters per assignment (a guard, not a
    # "round" — rounds are version-based above).
    prior_count = db.scalar(
        select(func.count(CounterProposal.id)).where(
            CounterProposal.assignment_id == assignment.id,
        )
    ) or 0
    if prior_count >= MAX_NEGOTIATION_ROUNDS:
        message.status = "failed"
        message.error_code = "max_messages_exceeded"
        db.commit()
        logger.warning(
            "Counter rejected (max messages): change=%s partner=%s",
            change_id, partner_id,
        )
        return f"Maximum {MAX_NEGOTIATION_ROUNDS} negotiation messages reached"

    # Parse partner-supplied fields. The actual COUNTER_PROPOSAL body
    # lives at payload['payload'] post-Slice-8 wrapping; tolerate both.
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    counter_proposal_id = inner.get("counter_proposal_id") or generate_uuid()
    justification = inner.get("justification") or inner.get("message") or ""
    valid_until_str = inner.get("valid_until")
    # Structured request type submitted by partner via new negotiation form
    request_category = inner.get("request_category") or None

    valid_until = None
    if valid_until_str:
        try:
            from datetime import datetime
            valid_until = datetime.fromisoformat(valid_until_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    # Symmetric counter-back: if there's an open authority-originated counter
    # waiting on the partner, this partner-originated counter implicitly
    # closes it (the partner is responding to the Authority's terms with their own
    # new terms, just like the /counter endpoint does in the other direction).
    open_authority_cp = db.scalars(
        select(CounterProposal).where(
            CounterProposal.assignment_id == assignment.id,
            CounterProposal.status == CounterProposalStatus.OPEN,
            CounterProposal.originator == "npci",
        )
    ).first()
    if open_authority_cp:
        open_authority_cp.status = CounterProposalStatus.COUNTERED_BACK
        open_authority_cp.resolved_at = utcnow()
        open_authority_cp.resolution_text = "Closed by partner's new counter"
        logger.info(
            "Auto-closed the Authority counter %s (partner countered back)",
            open_authority_cp.counter_proposal_id,
        )

    cp = CounterProposal(
        id=generate_uuid(),
        change_request_id=change_id,
        partner_id=partner_id,
        assignment_id=assignment.id,
        counter_proposal_id=counter_proposal_id,
        status=CounterProposalStatus.OPEN,
        originator="partner",
        negotiation_round=current_round,
        justification=justification,
        valid_until=valid_until,
        payload=inner,
        request_category=request_category,
    )
    db.add(cp)
    db.flush()  # so cp.id is available for the dual-write FK
    # Dual-write: mirror the structured event onto the unified
    # negotiation timeline so the renderer eventually consumes a
    # single source. Wrapped so a failure can't lose the CP itself.
    try:
        from app.services.negotiation_service import record_linked_message
        if open_authority_cp is not None:
            record_linked_message(
                db,
                change_request_id=change_id,
                partner_id=partner_id,
                role="partner",
                content="Closed by partner's new counter",
                event_kind="resolution",
                counter_proposal_id=open_authority_cp.id,
            )
        record_linked_message(
            db,
            change_request_id=change_id,
            partner_id=partner_id,
            role="partner",
            content=justification or "(no justification)",
            event_kind="proposal",
            counter_proposal_id=cp.id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Dual-write of negotiation_message failed for partner counter; CP still persisted")

    # Partner responded within their window → close the open round so the
    # silent-acceptance sweep won't later auto-accept on their behalf.
    try:
        from app.services.negotiation_extended import mark_round_responded
        mark_round_responded(change_id, partner_id, db)
    except Exception:
        logger.exception("Failed to mark round responded (counter) change=%s partner=%s", change_id, partner_id)

    message.status = "completed"
    db.commit()

    logger.info(
        "Counter-proposal received: change=%s partner=%s round=%d cp_id=%s",
        change_id, partner_id, cp.negotiation_round, counter_proposal_id,
    )
    return f"Counter-proposal recorded (round {cp.negotiation_round})"


def ensure_cp_for_query(
    partner_id: str,
    change_id: str,
    message: A2AMessage,
    db: Session,
) -> CounterProposal | None:
    """Create a CounterProposal record for a plain partner QUERY.

    Unified negotiation flow: every inbound partner message — not just
    structured counters — runs through auto-reject + clustering. Those operate
    on CounterProposal rows, so we mint one from the query text (no
    request_category, so the classifier falls back to the LLM mandatory-violation
    check). Returns the CP, or None when the partner isn't assigned.

    Distinct from process_counter_proposal: this does NOT dual-write a
    negotiation message (record_partner_query already recorded the partner's
    message in the thread) — it only creates the row the pipeline needs.
    """
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()
    if not assignment:
        return None

    inner = (message.payload or {}).get("payload")
    if not isinstance(inner, dict):
        inner = message.payload or {}
    text = inner.get("message") or inner.get("justification") or ""

    # Round = the current kit version's 24h window (round N == v(N)).
    from app.models.change_request import ChangeRequest as _CR
    _ch = db.get(_CR, change_id)
    current_round = (getattr(_ch, "negotiation_version", 1) or 1) if _ch else 1

    cp = CounterProposal(
        id=generate_uuid(),
        change_request_id=change_id,
        partner_id=partner_id,
        assignment_id=assignment.id,
        counter_proposal_id=generate_uuid(),
        status=CounterProposalStatus.OPEN,
        originator="partner",
        negotiation_round=current_round,
        justification=text,
        payload=inner,
        request_category=None,
    )
    db.add(cp)
    db.commit()
    logger.info("Query negotiation-item created: change=%s partner=%s cp=%s", change_id, partner_id, cp.id)
    return cp


def process_counter_decision(
    partner_id: str,
    change_id: str,
    payload: dict,
    message: A2AMessage,
    db: Session,
) -> str:
    """Partner accepted (or rejected) an authority-originated counter.

    Mirror of the authority-side `accept_counter_proposal` endpoint: locates
    the open `originator='npci'` CounterProposal by its wire id and
    flips its status. Does NOT touch assignment.status — counter
    acceptance is a per-counter act, distinct from rollout acceptance
    (which still arrives as a separate CHANGE_ACKNOWLEDGEMENT).
    """
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    cp_wire_id = inner.get("in_response_to")
    decision = (inner.get("decision") or "").upper()
    resolution_text = inner.get("resolution_text") or inner.get("response") or ""

    if not cp_wire_id:
        message.status = "failed"
        db.commit()
        return "Missing in_response_to (counter_proposal_id)"
    if decision not in {"ACCEPT", "REJECT"}:
        message.status = "failed"
        db.commit()
        return f"Invalid decision '{decision}' (expected ACCEPT or REJECT)"

    cp = db.scalars(
        select(CounterProposal).where(
            CounterProposal.change_request_id == change_id,
            CounterProposal.partner_id == partner_id,
            CounterProposal.counter_proposal_id == cp_wire_id,
            CounterProposal.originator == "npci",
        )
    ).first()
    if not cp:
        message.status = "failed"
        db.commit()
        return f"the Authority counter '{cp_wire_id}' not found"
    if cp.status != CounterProposalStatus.OPEN:
        message.status = "failed"
        db.commit()
        return f"Counter already {cp.status.value}"

    cp.status = (
        CounterProposalStatus.ACCEPTED if decision == "ACCEPT"
        else CounterProposalStatus.REJECTED
    )
    cp.resolved_at = utcnow()
    cp.resolution_text = resolution_text or f"Counter {decision.lower()}ed by partner"

    try:
        from app.services.negotiation_service import record_linked_message
        record_linked_message(
            db,
            change_request_id=change_id,
            partner_id=partner_id,
            role="partner",
            content=cp.resolution_text,
            event_kind="resolution",
            counter_proposal_id=cp.id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Dual-write NM failed for partner counter-decision; CP still resolved")

    # Partner acted on the Authority's counter → close the open round so the
    # silent-acceptance sweep won't later auto-accept on their behalf.
    try:
        from app.services.negotiation_extended import mark_round_responded
        mark_round_responded(change_id, partner_id, db)
    except Exception:
        logger.exception("Failed to mark round responded (counter-decision) change=%s partner=%s", change_id, partner_id)

    message.status = "completed"
    db.commit()

    logger.info(
        "Counter decision received: change=%s partner=%s cp=%s decision=%s",
        change_id, partner_id, cp_wire_id, decision,
    )
    return f"Counter {decision.lower()}ed"


def process_blocker(
    partner_id: str,
    change_id: str,
    payload: dict,
    message: A2AMessage,
    db: Session,
) -> str:
    """Partner reported a structured BLOCKER. Creates a Blocker row
    and flips assignment.status to BLOCKED (or sets the blocked_at
    flag — both already exist on the assignment). PM resolves via the
    /resolve endpoint.
    """
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()
    if not assignment:
        message.status = "failed"
        db.commit()
        return "Partner not assigned to this change request"

    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    blockers = inner.get("blockers") or []
    if not blockers:
        # Single-blocker shape (some senders flatten)
        blockers = [inner]

    created = 0
    for b in blockers:
        sev_str = (b.get("severity") or "high").lower()
        try:
            severity = BlockerSeverity(sev_str)
        except ValueError:
            severity = BlockerSeverity.HIGH

        row = Blocker(
            id=generate_uuid(),
            change_request_id=change_id,
            partner_id=partner_id,
            assignment_id=assignment.id,
            blocker_id=b.get("blocker_id") or f"BLK-{generate_uuid()[:8]}",
            severity=severity,
            status=BlockerStatus.OPEN,
            description=b.get("description") or "(no description)",
            impact=b.get("impact"),
            investigation_done=b.get("investigation_done"),
            options_considered=b.get("options_considered"),
            requested_action_from_npci=b.get("requested_action_from_npci"),
            payload=b,
        )
        db.add(row)
        db.flush()
        try:
            from app.services.negotiation_service import record_linked_message
            record_linked_message(
                db,
                change_request_id=change_id,
                partner_id=partner_id,
                role="partner",
                content=row.description,
                event_kind="blocker",
                blocker_id=row.id,
                # Blockers get their OWN thread. The "general" default filed operational
                # escalations into the same inbox as spec negotiation, so a bank reporting
                # a production blocker appeared interleaved with its counter-proposals and
                # queries — and nothing routed on `event_kind` to tell them apart. Uses the
                # same mechanism the cert inbox already relies on (kind='cert').
                thread_kind="blocker",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Dual-write of negotiation_message failed for blocker; Blocker still persisted")
        created += 1

    # Mark assignment as blocked (orthogonal flag; status preserves
    # the prior phase so we know what to return to on resolve).
    assignment.blocked_at = utcnow()
    assignment.blocked_reason = f"{created} open blocker(s) reported by partner"

    message.status = "completed"
    db.commit()

    logger.info(
        "Blocker(s) received: change=%s partner=%s count=%d",
        change_id, partner_id, created,
    )
    return f"{created} blocker(s) recorded"


def process_emergency_issue(
    partner_id: str,
    change_id: str,
    payload: dict,
    message: A2AMessage,
    db: Session,
) -> str:
    """Partner raised a post-freeze EMERGENCY_ISSUE (break-glass channel).

    Creates an EmergencyIssue row. Unlike a Blocker, this is accepted even
    when the change is frozen — it's the only inbound task that is. The PM
    triages and resolves from the Negotiation Hub.
    """
    from app.models.emergency_issue import (
        EMERGENCY_SEVERITIES,
        EI_STATUS_OPEN,
        EmergencyIssue,
    )

    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    severity = str(inner.get("severity") or "critical").lower()
    if severity not in EMERGENCY_SEVERITIES:
        severity = "critical"

    row = EmergencyIssue(
        id=generate_uuid(),
        change_request_id=change_id,
        partner_id=partner_id,
        issue_id=inner.get("issue_id") or f"EMG-{generate_uuid()[:8]}",
        severity=severity,
        status=EI_STATUS_OPEN,
        title=(inner.get("title") or "Emergency issue")[:300],
        description=inner.get("description") or "(no description)",
    )
    db.add(row)
    message.status = "completed"
    db.commit()

    logger.info(
        "EmergencyIssue received: change=%s partner=%s severity=%s id=%s",
        change_id, partner_id, severity, row.issue_id,
    )
    return f"Emergency issue {row.issue_id} recorded"


# ── Cert-related handlers (verbatim from a2a_cert_handlers.py) ───────────────


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


async def run_triage(cert_run_id: str, db_factory) -> None:
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

        # Protocol v1 (§7.6): collect verdicts to push to the partner as
        # cert_verdict_notification messages after they're persisted.
        to_notify: list[dict] = []

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
            to_notify.append({
                "case_id":   test_result.test_case_id,
                "verdict":   verdict_str,
                "reasoning": reasoning,
            })

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
        logger.info(
            "Auto-triage completed: cert_run_id=%s failures=%d",
            cert_run_id, len(failed),
        )

        # Protocol v1 (§7.6): notify the partner of each triage verdict via a
        # first-class cert_verdict_notification (previously triage stayed
        # internal — the Authority never told the partner the verdict over A2A).
        if to_notify:
            run = db.get(CertRun, cert_run_id)
            partner = db.get(PartnerAgent, run.partner_id) if run else None
            if run and partner:
                from app.a2a_common import protocol as _proto
                from app.services.a2a_client import send_task_to_partner
                for n in to_notify:
                    try:
                        await send_task_to_partner(
                            partner=partner,
                            task_type=_proto.A2ATaskType.CERT_VERDICT_NOTIFICATION,
                            payload={
                                "case_id":   n["case_id"],
                                "attempt":   run.run_number,
                                "verdict":   n["verdict"],
                                "reasoning": n["reasoning"],
                            },
                            db=db,
                            change_request_id=run.change_request_id,
                            cflow_id=run.cflow_id,
                            cert_attempt=run.run_number,
                        )
                    except Exception:  # noqa: BLE001 — one bad send shouldn't abort the rest
                        logger.exception("cert_verdict_notification send failed case=%s", n["case_id"])
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

    # CertRun.id is varchar(36); a synthetic wire cert_run_id like
    # "mock-<uuid>" (41 chars) doesn't fit and won't match any real run —
    # skip the direct lookup in that case and let the mock path derive a
    # 36-char deterministic id below.
    cert_run = db.get(CertRun, cert_run_id) if len(cert_run_id) <= 36 else None

    # OWNERSHIP. The run is looked up by an id the SENDER supplied, so it must be
    # checked against the authenticated sender before anything is written. Without
    # this, partner A can post results against partner B's run and drive B's
    # assignment to CERTIFIED — and the delete-then-insert below would erase B's
    # genuine results on the way. `process_cert_case_result_report` performs the
    # equivalent check via `_select_reportable_row`; this handler predates it.
    if cert_run is not None and cert_run.partner_id != message.partner_id:
        message.status = "failed"
        db.commit()
        logger.warning(
            "SECURITY_EVENT event=cert_result_not_partner_owned severity=high "
            "sender=%s owner=%s cert_run=%s message=%s — cross-partner cert "
            "result refused",
            message.partner_id, cert_run.partner_id, cert_run.id, message.id)
        return "partner_mismatch: that cert run belongs to another partner"

    if not cert_run:
        # Demo path — partner posted a synthetic run with `mock: true` and
        # The Authority's orchestrator never created a matching CertRun.
        #
        # PRODUCTION-GATED. This branch lets the party being certified invent the
        # run it is then judged by, so it is a self-certification primitive: with
        # it, a partner needs no dispatched run at all. It was previously reachable
        # on any deployment because the only condition was the caller's own boolean.
        if (
            payload.get("mock") is True
            and message.change_request_id
            and message.partner_id
            and not _is_production()
        ):
            # Derive a stable 36-char id from the wire cert_run_id so re-sends
            # (partner retries after a transient the Authority failure) resolve to the
            # same CertRun row — no duplicates, no orphaned per-TC results.
            import uuid as _uuid
            synth_id = (
                cert_run_id if len(cert_run_id) <= 36
                else str(_uuid.uuid5(_uuid.NAMESPACE_OID, cert_run_id))
            )
            cert_run = db.get(CertRun, synth_id)
            if cert_run is None:
                existing_max = db.query(func.coalesce(func.max(CertRun.run_number), 0)).filter(
                    CertRun.change_request_id == message.change_request_id,
                    CertRun.partner_id == message.partner_id,
                ).scalar() or 0
                cert_run = CertRun(
                    id=synth_id,
                    change_request_id=message.change_request_id,
                    partner_id=message.partner_id,
                    run_number=int(existing_max) + 1,
                    status=CertRunStatus.RUNNING,
                    started_at=utcnow(),
                )
                db.add(cert_run)
                db.flush()
                logger.info(
                    "cert_test_response synthetic CertRun created (mock=true): "
                    "wire_cert_run_id=%s stored_id=%s change=%s partner=%s run_number=%s",
                    cert_run_id, cert_run.id, cert_run.change_request_id,
                    cert_run.partner_id, cert_run.run_number,
                )
        else:
            message.status = "failed"
            db.commit()
            logger.warning(
                "cert_test_response references unknown CertRun: cert_run_id=%s message=%s",
                cert_run_id, message.id,
            )
            return f"CertRun '{cert_run_id}' not found"

    incoming = payload.get("results") or []
    if not isinstance(incoming, list):
        message.status = "failed"
        db.commit()
        return "cert_test_response 'results' must be a list"
    # Bound the work a single message can demand. Each entry becomes a row, and
    # the payload is partner-supplied, so an unbounded list is both a memory and
    # a storage amplification lever.
    if len(incoming) > _MAX_CERT_RESULTS_PER_MESSAGE:
        message.status = "failed"
        db.commit()
        logger.warning(
            "cert_test_response over-large: %d results (cap %d) partner=%s message=%s",
            len(incoming), _MAX_CERT_RESULTS_PER_MESSAGE, message.partner_id, message.id)
        return (f"cert_test_response carries {len(incoming)} results, "
                f"exceeding the {_MAX_CERT_RESULTS_PER_MESSAGE} cap")
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

    # Update partner status. all-pass → CERTIFIED; any fail → stay at
    # CERTIFYING until a re-run passes.
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
            set_status(
                assignment, AssignmentStatus.CERTIFYING, db,
                actor_partner_id=message.partner_id,
                reason=f"Cert run #{cert_run.run_number} completed with {failed} failure(s)",
            )

    message.status = "completed"
    db.commit()

    if failed > 0:
        from app.core.database import SessionLocal
        background_tasks.add_task(run_triage, cert_run.id, SessionLocal)

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
    """the Authority receives a defect notice from a partner. v1: just record."""
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

    triage: Optional[CertTriage] = None
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


def _inner(payload: dict) -> dict:
    """Unwrap a possibly-nested payload ({payload: {...}} or {...})."""
    if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
        return payload["payload"]
    return payload or {}


def process_cert_waiver_request(
    partner_id: str, change_id: str, payload: dict, message: A2AMessage, db: Session,
) -> str:
    """Partner requests a waiver for a cert case (protocol v1 §7.8). Records a
    CertWaiver row in status='requested' for the Risk+Product gate to decide."""
    inner = _inner(payload)
    case_id = str(inner.get("case_id") or "").strip()
    if not case_id:
        message.status = "failed"
        db.commit()
        return "cert_waiver_request missing case_id"
    if len(case_id) > 100:  # column is String(100); partner-controlled input
        message.status = "failed"
        message.error_code = "payload_validation_error"
        db.commit()
        return "cert_waiver_request case_id exceeds 100 characters"
    # Inbound is untrusted: only a partner assigned to this change may file a
    # waiver against it (same anchor every other inbound handler uses).
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()
    if not assignment:
        message.status = "failed"
        message.error_code = "partner_mismatch"
        db.commit()
        return "Partner not assigned to this change request"
    category = inner.get("category")
    waiver = CertWaiver(
        id=generate_uuid(),
        change_request_id=change_id,
        partner_id=partner_id,
        cflow_id=f"CFLOW-{change_id[:8]}-{partner_id[:8]}",
        case_id=case_id,
        category=str(category)[:40] if category else None,  # column is String(40)
        reason=inner.get("reason"),
        status="requested",
    )
    db.add(waiver)
    message.status = "completed"
    db.commit()
    logger.info("CERT_WAIVER_REQUEST recorded: change=%s case=%s", change_id, case_id)
    return f"Waiver request recorded for case {case_id}"


def process_cert_run_abort(
    partner_id: str, change_id: str, payload: dict, message: A2AMessage, db: Session,
) -> str:
    """Either side aborts the cert run (protocol v1 §7.14, terminal). Marks the
    latest cert run for this change+partner ABORTED.

    A COMPLETED run is a signed-off non-repudiation record — it cannot be
    aborted retroactively (invalid_state_transition). Only RUNNING runs abort."""
    run = db.scalars(
        select(CertRun)
        .where(CertRun.change_request_id == change_id, CertRun.partner_id == partner_id)
        .order_by(CertRun.started_at.desc())
    ).first()
    if run and run.status == CertRunStatus.COMPLETED:
        message.status = "failed"
        message.error_code = "invalid_state_transition"
        db.commit()
        return "Cert run is COMPLETED (signed off) — abort refused"
    if run and run.status != CertRunStatus.ABORTED:
        run.status = CertRunStatus.ABORTED
        run.completed_at = utcnow()
    message.status = "completed"
    db.commit()
    reason = str(_inner(payload).get("reason") or "")
    logger.info("CERT_RUN_ABORT: change=%s partner=%s reason=%s", change_id, partner_id, reason[:80])
    return f"Cert run aborted: {reason[:80]}" if reason else "Cert run aborted"


def process_cert_fix_notification(
    partner_id: str, change_id: str, payload: dict, message: A2AMessage, db: Session,
    bg: Optional[BackgroundTasks] = None,
) -> str:
    """Partner reports fixed defects and requests a re-run (protocol v1 §7.10;
    supersedes defect_resolution). Marks matching triage rows resolved.

    C-6: with `cert_auto_loop_enabled` (default OFF — the decided posture is
    "auto-fix, human approves the round close") the pure `cert_loop.decide`
    reads the round history and either dispatches round N+1 — through
    `certification_dispatch.run_certification`, the harness-agnostic seam,
    exactly like the readiness path — or halts, recording why on
    `cert_flow_states.halted_reason` (the column C-0 shipped for this). With
    the flag off, or no `bg` to schedule on, the pre-loop behaviour and
    wording are unchanged: the re-run stays operator-triggered.
    """
    inner = _inner(payload)
    fixed = inner.get("fixed_case_ids") or []
    resolved = 0
    if fixed:
        rows = db.scalars(
            select(CertTriage)
            .join(CertTestResult, CertTriage.cert_test_result_id == CertTestResult.id)
            .join(CertRun, CertTestResult.cert_run_id == CertRun.id)
            .where(
                CertRun.change_request_id == change_id,
                CertRun.partner_id == partner_id,
                CertTestResult.test_case_id.in_(fixed),
            )
        ).all()
        for t in rows:
            t.final_verdict = "resolved"
            resolved += 1
    message.status = "completed"
    db.commit()
    logger.info(
        "CERT_FIX_NOTIFICATION: change=%s cases=%s triage_resolved=%d",
        change_id, fixed, resolved,
    )
    base_msg = f"Fix recorded for {len(fixed)} case(s); {resolved} triage resolved (re-run on operator action)"

    from app.core.config import settings

    if not settings.cert_auto_loop_enabled or bg is None:
        return base_msg

    from app.services import cert_loop

    runs = db.scalars(
        select(CertRun)
        .where(CertRun.change_request_id == change_id,
               CertRun.partner_id == partner_id)
        .order_by(CertRun.run_number)
    ).all()
    rounds = []
    for run in runs:
        # A case may have executed several variants — any FAIL fails it. SKIP
        # and ERROR are excluded inside `failed_cases`: not partner-fixable.
        statuses: dict[str, list[str]] = {}
        for r in run.results:
            value = r.status.value if hasattr(r.status, "value") else str(r.status)
            statuses.setdefault(r.test_case_id or "", []).append(value)
        rounds.append(cert_loop.RoundOutcome(run.run_number,
                                             cert_loop.failed_cases(statuses)))
    decision = cert_loop.decide(rounds, max_rounds=settings.cert_max_rounds)
    cflow_id = f"CFLOW-{change_id[:8]}-{partner_id[:8]}"

    if decision.action is cert_loop.LoopAction.DISPATCH:
        from app.services import cert_modes
        from app.services.cert_agent import flow_store
        from app.services.cert_agent.flow import FlowState
        from app.services.cert_agent.state_machine import Phase as _Phase
        from app.services.cert_agent.state_machine import Trigger as _Trigger
        from app.services.certification_dispatch import run_certification

        # I-6b/§3.6.4: the loop must not AUTO-dispatch a round that has real
        # side effects. The modes of the round we would be repeating are the
        # ones at stake — a deployed application does not reset between
        # rounds the way a simulator does.
        _last = runs[-1] if runs else None
        _modes = cert_modes.RunModes(
            npci=getattr(_last, "npci_mode", None) or cert_modes.SIMULATOR,
            partner=getattr(_last, "partner_mode", None) or cert_modes.SIMULATOR)
        _ok, _why = cert_modes.auto_dispatch_allowed(
            _modes, permitted=settings.cert_application_mode_auto_dispatch)
        if not _ok:
            logger.warning("CERT_AUTO_LOOP refused: change=%s partner=%s — %s",
                           change_id, partner_id, _why)
            return f"{base_msg} — auto-loop: {_why}"

        row = flow_store.load(db, cflow_id)
        if row is not None:
            # FIX_PENDING → RUNNING on the persisted flow; anywhere else the
            # warn-don't-raise wrapper holds the phase and logs.
            flow = FlowState(cflow_id, phase=_Phase(row.phase),
                             persist=flow_store.persister(
                                 db, change_request_id=change_id,
                                 partner_id=partner_id,
                                 current_round=row.current_round))
            flow.fire(_Trigger.fix_received)
        latest = runs[-1] if runs else None
        # Through the seam — never orchestrate_cert_run* directly; the pack
        # picks the harness, and the audit trail records WHO dispatched.
        bg.add_task(
            run_certification, change_id, partner_id, "", {}, {},
            dispatch_meta={
                "dispatched_by": "auto",
                "previous_run_id": latest.id if latest else None,
                "fix_notification_message_id": message.id,
            },
        )
        logger.info("CERT_AUTO_LOOP dispatch: change=%s partner=%s — %s",
                    change_id, partner_id, decision.reason)
        return f"{base_msg} — auto-loop: {decision.reason}"

    if decision.halted:
        from app.services.cert_agent import flow_store

        row = flow_store.load(db, cflow_id)
        if row is not None:
            row.halted_reason = decision.reason[:200]
            db.commit()
        logger.warning("CERT_AUTO_LOOP halt: change=%s partner=%s — %s",
                       change_id, partner_id, decision.reason)
        return f"{base_msg} — auto-loop halted: {decision.reason}"

    # SIGNOFF: the all-passed run already emitted cert_signoff_notification —
    # report and stop, never send a second one.
    logger.info("CERT_AUTO_LOOP signoff: change=%s partner=%s — %s",
                change_id, partner_id, decision.reason)
    return f"{base_msg} — auto-loop: {decision.reason}"


def _partner_reportable(row: CertTestResult) -> bool:
    """May a bank report WRITE this row?

    Only two kinds of row are the partner's to speak for:

    * a placeholder still awaiting it — `not_reported`, stamped at dispatch by
      `cert_pack_run`/`cert_orchestrator` for a partner-initiated case, or for
      an application-mode case this side merely TRIGGERED;
    * a row a previous bank report already wrote — so a re-delivery, a
      correction or a second variant of the same case keeps working.

    Every other row on the run is one the AUTHORITY executed and adjudicated
    itself: it holds this side's own verdict and, on failure, the
    `assertion_failures` that justify it. A partner claim must never overwrite
    that — it would erase the evidence and, because `cert_join` counts a row
    as pending only while `not_reported` is set, hand the run a clean sweep to
    finalize on (`CERTIFIED`) off the back of cases that failed here.

    Deliberately keyed on the marker, NOT on `direction`: the application-mode
    placeholder at `cert_pack_run._round` is `AUTHORITY_TO_PARTNER` and is still
    legitimately the partner's to report, so a direction test would break it.
    """
    response = row.actual_response
    if not isinstance(response, dict):
        return False
    return bool(response.get("not_reported")) or response.get("reporter") == "bank"


def _select_reportable_row(
    rows: list[CertTestResult], variant_id: str,
) -> Optional[CertTestResult]:
    """Which of a case's rows this report speaks for, or None.

    A case carries ONE ROW PER VARIANT, so `(run, case_id)` does not identify a
    row on its own. `details.variant_id` (set by `case_details_payload`) picks
    the exact one when the partner sends it; otherwise the oldest still-awaited
    placeholder is filled first, so a multi-variant case converges one report
    at a time instead of the same arbitrary row being rewritten.
    """
    reportable = [r for r in rows if _partner_reportable(r)]
    if not reportable:
        return None
    if variant_id:
        for r in reportable:
            response = r.actual_response or {}
            if str(response.get("variant_id") or "") == variant_id:
                return r
            details = response.get("details")
            if isinstance(details, dict) \
                    and str(details.get("variant_id") or "") == variant_id:
                return r
    # Unfilled placeholders before rows a previous report already wrote.
    pending = [r for r in reportable
               if (r.actual_response or {}).get("not_reported")]
    return (pending or reportable)[0]


def _authority_cross_check(row: CertTestResult, details: dict) -> tuple[bool, str]:
    """Does the partner's PASS claim survive the AUTHORITY's own expectation?

    `row.expected_response` was computed from the published workbook at dispatch
    (`{"result": "PASS", "code": "E009"}`) and copied onto the row so a later
    registry edit cannot rewrite it. The report carries what the partner's
    application actually answered. Comparing the two here is the difference
    between certifying BEHAVIOUR and certifying the partner's opinion of its
    behaviour: a rig with no expectation configured on its side returns
    `passed` for any well-formed response, and without this the authority
    recorded that verbatim — a case whose expected code was E004 passed while
    the application answered SUCCESS (change f989926b, LL_4, run 4).

    Evidence arrives in either of two shapes, and BOTH are read: the partner
    platform's `observed` block, and the flat `actual_code` that this stack's
    own `case_details_payload` emits. Reading only the former left the check a
    no-op against every report built by `cert_agent.execution` — the expectation
    was fetched, and then nothing was ever compared against it.

    Absent evidence is NOT a failure. With no expectation, or with a report that
    carries no answer at all, there is nothing to contradict, and manufacturing
    a FAIL would be the mirror of the bug this fixes. That is safe only because
    a report can no longer reach a case the authority adjudicated itself
    (`_partner_reportable`): what stays trust-on-report is the partner's own
    class, which is self-reported by design.
    """
    expected = ((row.expected_response or {}).get("code") or "").strip()
    if not expected:
        return True, ""
    observed = (details or {}).get("observed") or {}
    if not observed and (details or {}).get("actual_code") is not None:
        observed = {"result": (details or {}).get("actual_code")}
    if not observed:
        return True, ""
    # A failure carries its code in err_code; a success carries an empty
    # err_code and says SUCCESS in result. One field either way.
    actual = (str(observed.get("err_code") or "").strip()
              or str(observed.get("result") or "").strip())
    if not actual or actual.upper() == expected.upper():
        return True, ""
    return False, (f"authority expected response code {expected!r}; the partner's "
                   f"application answered {actual!r}")


def process_cert_case_result_report(
    partner_id: str, change_id: str, payload: dict, message: A2AMessage, db: Session,
    bg: Optional[BackgroundTasks] = None,
) -> str:
    """A partner-ORIGINATED cert_case_result (ITA I-6, §3.7).

    Until I-6 an inbound cert_case_result was audited and acked as an echo of
    a case this side ran. With partner-initiated cases executing on the
    partner's own side, a `reporter: "bank"` report IS the result: it is
    upserted onto the latest run for the (change, partner), replacing the
    not-reported placeholder the orchestrator recorded at dispatch. It remains
    EVIDENCE, not proof — field assertions still run against the
    tunnel-captured exchange where one exists.

    Anything that is not a bank-originated report (no reporter, an echo of an
    authority-run case) keeps the pre-I-6 acknowledgement byte for byte.
    """
    inner = _inner(payload)
    reporter = str(inner.get("reporter") or "").strip().lower()
    case_id = str(inner.get("case_id") or inner.get("test_case_id")
                  or (payload or {}).get("test_case_id") or "").strip()
    if reporter != "bank" or not case_id:
        message.status = "completed"
        db.commit()
        return "cert_case_result received"

    run = db.scalars(
        select(CertRun)
        .where(CertRun.change_request_id == change_id,
               CertRun.partner_id == partner_id)
        .order_by(CertRun.run_number.desc())
    ).first()
    if run is None:
        message.status = "completed"
        db.commit()
        logger.warning("CERT_CASE_RESULT(bank): change=%s case=%s — no run to attach",
                       change_id, case_id)
        return f"Bank-reported result for {case_id} received (no run to attach)"

    wire = str(inner.get("status") or "").strip().lower()
    internal = {
        "passed": CertTestStatus.PASS,
        "failed": CertTestStatus.FAIL,
        "error": CertTestStatus.ERROR,
    }.get(wire, CertTestStatus.SKIP)

    details = dict(inner.get("details") or {})

    # Which row — if any — this report is entitled to write. A case holds one
    # row per variant, and only the rows still awaiting the partner (or ones a
    # previous bank report wrote) are its to speak for.
    rows = list(db.scalars(
        select(CertTestResult)
        .where(CertTestResult.cert_run_id == run.id,
               CertTestResult.test_case_id == case_id)
    ).all())
    row = _select_reportable_row(rows, str(details.get("variant_id") or "").strip())

    if row is None and rows:
        # The case exists on this run but every row for it is one the AUTHORITY
        # executed and adjudicated. Acknowledge — a partner that keeps retrying
        # a rejected report helps nobody — and change NOTHING.
        message.status = "completed"
        db.commit()
        logger.warning(
            "SECURITY_EVENT event=cert_result_not_partner_owned severity=high "
            "partner=%s change=%s case=%s run=%s claimed=%s — the case is the "
            "authority's own; report refused, verdict left intact",
            partner_id, change_id, case_id, run.run_number, wire or "unknown")
        return (f"Bank-reported result for {case_id} refused: the case was "
                f"executed by the authority and is not the partner's to report")

    # A PASS the partner claims is checked against what THIS side expected
    # before it is recorded. Only a claimed pass is re-examined: a reported
    # FAIL/ERROR is the partner conceding, and the authority has no reason to
    # argue a case up.
    override_reason = ""
    if internal == CertTestStatus.PASS and row is not None:
        ok, override_reason = _authority_cross_check(row, details)
        if not ok:
            internal = CertTestStatus.FAIL

    if row is None:
        row = CertTestResult(
            id=generate_uuid(), cert_run_id=run.id, test_case_id=case_id,
            direction=CertDirection.PARTNER_TO_AUTHORITY, status=internal,
        )
        db.add(row)
    else:
        row.status = internal
    # The report's own details ride along for the triage/assertion view. The
    # variant the placeholder named is carried FORWARD: it is what binds this
    # row to its variant once `not_reported` is gone, so a re-report or a
    # correction can still be matched to the right one.
    prior = row.actual_response if isinstance(row.actual_response, dict) else {}
    variant_id = (str(details.get("variant_id") or "").strip()
                  or str(prior.get("variant_id") or "").strip())
    row.actual_response = {"reporter": "bank", "status": wire,
                           "details": details}
    if variant_id:
        row.actual_response["variant_id"] = variant_id
    if override_reason:
        # Keep BOTH readings: what the partner claimed and why this side
        # disagreed. A silent downgrade would be as opaque as the silent
        # acceptance it replaces.
        row.actual_response["authority_override"] = {
            "reported": wire, "recorded": internal.value, "reason": override_reason,
        }
        logger.warning("CERT_CASE_RESULT(bank): change=%s case=%s reported 'passed' "
                       "but %s — recorded as FAIL", change_id, case_id, override_reason)

    # Counters are recomputed from the rows, not nudged — an upsert that both
    # replaces a placeholder and changes a status would need two adjustments,
    # and a recount cannot drift.
    db.flush()
    rows = run.results
    run.total = len(rows)
    run.passed = sum(1 for r in rows if r.status == CertTestStatus.PASS)
    run.failed = sum(1 for r in rows if r.status == CertTestStatus.FAIL)
    run.skipped = sum(1 for r in rows
                      if r.status in (CertTestStatus.SKIP, CertTestStatus.ERROR))
    message.status = "completed"
    db.commit()
    logger.info("CERT_CASE_RESULT(bank): change=%s case=%s -> %s (run %s)",
                change_id, case_id, internal.value, run.run_number)

    # ITA-7: the join decides whether this was the LAST awaited case — off the
    # request path, on its own session. A missed hook (no bg, a crash between
    # commit and schedule) is caught by the deadline sweep, so this is an
    # optimisation for latency, not the correctness story.
    if bg is not None:
        from app.services.cert_join import check_and_finalize

        bg.add_task(check_and_finalize, run.id)
    return f"Bank-reported result recorded for {case_id}: {wire or 'unknown'} (run {run.run_number})"
