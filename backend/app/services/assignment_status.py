# Copyright 2026 National Payments Corporation of India
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
            logger.info(
                "Assignment %s: ignored backward transition %s → %s (forward-only)",
                assignment.id, current_value, new_value,
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
