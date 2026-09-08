# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Helpers for transitioning ChangePartnerAssignment.status.

Every code path that changes assignment.status should go through
`set_status()` here so the audit trail in `assignment_status_history` stays
consistent. System transitions pass `actor_user_id=None` and
`actor_partner_id=...` (or both None for purely-derived transitions).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.base import generate_uuid
from app.models.phase_c import (
    AssignmentStatus,
    AssignmentStatusHistory,
    ChangePartnerAssignment,
)

logger = logging.getLogger(__name__)

# Canonical forward order of the (active) linear lifecycle. Used to reject
# backward transitions — a replayed/late inbound message must never drag a
# partner who's already further along back to an earlier stage. Legacy/dormant
# values (communicated/acknowledged/in_progress/ready) are intentionally NOT
# ranked, so transitions involving them are left unguarded.
_FORWARD_ORDER = [
    AssignmentStatus.ASSIGNED,
    AssignmentStatus.RECEIVED,
    AssignmentStatus.ACCEPTED,
    AssignmentStatus.APPLIED,
    AssignmentStatus.TESTED,
    AssignmentStatus.READY_FOR_CERTIFICATION,
    AssignmentStatus.CERTIFYING,
    AssignmentStatus.CERTIFIED,
    AssignmentStatus.READY_FOR_PRODUCTION,
    AssignmentStatus.IN_PRODUCTION,
]
_RANK = {s.value: i for i, s in enumerate(_FORWARD_ORDER)}


def set_status(
    assignment: ChangePartnerAssignment,
    new_status: AssignmentStatus,
    db: Session,
    *,
    actor_user_id: str | None = None,
    actor_partner_id: str | None = None,
    reason: str | None = None,
) -> bool:
    """Update assignment.status and append a history row.

    Returns True if the status actually changed (history row written),
    False if the new value matched the current value (no history written —
    avoids noise on re-runs and idempotent operations).
    """
    current = assignment.status
    current_value = current.value if hasattr(current, "value") else current
    new_value = new_status.value if hasattr(new_status, "value") else new_status

    if current_value == new_value:
        return False

    # Forward-only guard: the lifecycle only advances. A replayed/late inbound
    # message (e.g. a duplicate CHANGE_ACKNOWLEDGEMENT that calls
    # set_status(ACCEPTED)) must not move a partner who's already further along
    # (applied/tested/…) back to an earlier stage. WITHDRAWN is an explicit
    # cancel and may fire from anywhere; statuses outside the ranked progression
    # are left unguarded.
    if new_value != AssignmentStatus.WITHDRAWN.value:
        cur_rank = _RANK.get(current_value)
        new_rank = _RANK.get(new_value)
        if cur_rank is not None and new_rank is not None and new_rank < cur_rank:
            # WARNING, not INFO: when the dropped transition was driven by a
            # real partner message this silently discards the partner's half of
            # the negotiation while the request still returns success. That is
            # how F-30 stayed invisible — the only trace was one INFO line.
            logger.warning(
                "Assignment %s: DISCARDED transition %s → %s (forward-only "
                "guard). If a partner message drove this, its status change "
                "has been lost — actor_user=%s actor_partner=%s reason=%r",
                assignment.id, current_value, new_value,
                actor_user_id, actor_partner_id, reason,
            )
            return False

    history = AssignmentStatusHistory(
        id=generate_uuid(),
        assignment_id=assignment.id,
        from_status=current_value,
        to_status=new_value,
        actor_user_id=actor_user_id,
        actor_partner_id=actor_partner_id,
        reason=reason,
    )
    db.add(history)
    assignment.status = new_status

    logger.info(
        "Assignment %s: %s → %s (user=%s partner=%s reason=%s)",
        assignment.id, current_value, new_value,
        actor_user_id, actor_partner_id, (reason or "")[:60],
    )
    return True


# The partner-owned negotiation states an operator-forced advance walks
# through on the way to CERTIFYING. Every one of them normally requires a
# partner message; none of them may ever be attributed to the partner when an
# operator forced it (F-30).
FORCED_ADVANCE_PATH = (
    AssignmentStatus.ACCEPTED,
    AssignmentStatus.APPLIED,
    AssignmentStatus.TESTED,
    AssignmentStatus.READY_FOR_CERTIFICATION,
    AssignmentStatus.CERTIFYING,
)

FORCED_REASON_PREFIX = "FORCED"


def force_advance_to_certifying(
    assignment: ChangePartnerAssignment,
    db: Session,
    *,
    operator_user_id: str,
    operator_username: str = "operator",
) -> list[str]:
    """Walk an assignment to CERTIFYING on an operator's authority.

    The single place this walk is allowed to happen, so the attribution rule
    lives with it rather than at each call site. Every row is stamped with the
    OPERATOR and never `actor_partner_id` — the partner sent nothing, and an
    audit trail that says otherwise is what F-30 was.

    Returns the states actually written (empty if already at/after CERTIFYING).

    Caller beware: CERTIFYING outranks every negotiation state, so after this
    runs the forward-only guard in `set_status` will DISCARD the partner's own
    acknowledgement and milestones. Only force a partner that is not expected
    to negotiate.
    """
    reason = (f"{FORCED_REASON_PREFIX} by operator {operator_username} via cert "
              f"dispatch advance=true — no partner input")
    written: list[str] = []
    for target in FORCED_ADVANCE_PATH:
        if assignment.status != target and set_status(
            assignment, target, db,
            actor_user_id=operator_user_id, reason=reason,
        ):
            written.append(target.value)
    if written:
        logger.warning(
            "Assignment %s: operator %s FORCED %s — no partner message caused "
            "these; the partner's own transitions will now be discarded by the "
            "forward-only guard.",
            assignment.id, operator_username, ", ".join(written),
        )
    return written


def derive_progress_status(progress_steps: list) -> AssignmentStatus | None:
    """Given a list of completed ProgressStep values, return the right status.

    Returns None if no transition should fire (status should stay where it is).
    """
    from app.models.phase_c import ProgressStep

    has_design  = ProgressStep.DESIGN_COMPLETED  in progress_steps
    has_coding  = ProgressStep.CODING_COMPLETED  in progress_steps
    has_testing = ProgressStep.TESTING_COMPLETED in progress_steps

    if has_design and has_coding and has_testing:
        return AssignmentStatus.TESTED
    if has_design and has_coding:
        return AssignmentStatus.APPLIED
    # design only — partial progress, no main-state transition yet.
    return None
