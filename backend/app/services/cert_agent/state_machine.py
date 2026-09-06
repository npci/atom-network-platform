# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The lifecycle of one certification run — the stages it moves through, and the only moves
allowed between them.

A run walks from 'not started' to either 'certified' or 'aborted'. Every legal step is listed
in TRANSITIONS; anything not listed is illegal and raises IllegalTransition (which the outer
layer reports to the other side as the 'invalid_state_transition' error). Pure: no I/O.

Shape (spec §9.3):
    NOT_STARTED → CONFIG_REQUESTED → CONFIG_RECEIVED → SETUP → RUNNING
    RUNNING, a case fails      → TRIAGE_PENDING
      real defect              → FIX_PENDING → (fix) → RUNNING
      not a defect             → RUNNING
      waiver-eligible          → WAIVER_PENDING → (granted) RUNNING / (rejected) BLOCKED
      disputed                 → DISPUTE_PENDING → (resolved) → TRIAGE_PENDING
    RUNNING, all pass/waived   → COMPLETED → (signed off) → CERTIFIED
    any non-terminal, aborted  → ABORTED
"""
from __future__ import annotations

from enum import Enum


class Phase(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    CONFIG_REQUESTED = "CONFIG_REQUESTED"
    CONFIG_RECEIVED = "CONFIG_RECEIVED"
    SETUP = "SETUP"
    RUNNING = "RUNNING"
    TRIAGE_PENDING = "TRIAGE_PENDING"
    FIX_PENDING = "FIX_PENDING"
    WAIVER_PENDING = "WAIVER_PENDING"
    DISPUTE_PENDING = "DISPUTE_PENDING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CERTIFIED = "CERTIFIED"
    ABORTED = "ABORTED"


class Trigger(str, Enum):
    readiness_declared = "readiness_declared"
    config_submitted = "config_submitted"
    setup_completed = "setup_completed"
    run_started = "run_started"
    case_failed = "case_failed"
    triaged_real_defect = "triaged_real_defect"
    triaged_not_defect = "triaged_not_defect"
    triaged_waiver_eligible = "triaged_waiver_eligible"
    fix_received = "fix_received"
    waiver_granted = "waiver_granted"
    waiver_rejected = "waiver_rejected"
    dispute_raised = "dispute_raised"
    dispute_resolved = "dispute_resolved"
    all_cases_passed = "all_cases_passed"
    signed_off = "signed_off"
    aborted = "aborted"


TERMINAL: frozenset[Phase] = frozenset({Phase.CERTIFIED, Phase.ABORTED})


class IllegalTransition(Exception):
    """A trigger that isn't allowed in the current phase.

    The application layer maps this to the protocol error code 'invalid_state_transition'.
    """

    def __init__(self, phase: Phase, trigger: Trigger):
        self.phase = phase
        self.trigger = trigger
        super().__init__(f"cannot apply '{trigger.value}' while in '{phase.value}'")


_BASE: dict[tuple[Phase, Trigger], Phase] = {
    (Phase.NOT_STARTED, Trigger.readiness_declared): Phase.CONFIG_REQUESTED,
    (Phase.CONFIG_REQUESTED, Trigger.config_submitted): Phase.CONFIG_RECEIVED,
    (Phase.CONFIG_RECEIVED, Trigger.setup_completed): Phase.SETUP,
    (Phase.SETUP, Trigger.run_started): Phase.RUNNING,
    (Phase.RUNNING, Trigger.case_failed): Phase.TRIAGE_PENDING,
    (Phase.RUNNING, Trigger.all_cases_passed): Phase.COMPLETED,
    (Phase.TRIAGE_PENDING, Trigger.triaged_real_defect): Phase.FIX_PENDING,
    (Phase.TRIAGE_PENDING, Trigger.triaged_not_defect): Phase.RUNNING,
    (Phase.TRIAGE_PENDING, Trigger.triaged_waiver_eligible): Phase.WAIVER_PENDING,
    (Phase.TRIAGE_PENDING, Trigger.dispute_raised): Phase.DISPUTE_PENDING,
    (Phase.FIX_PENDING, Trigger.fix_received): Phase.RUNNING,
    (Phase.WAIVER_PENDING, Trigger.waiver_granted): Phase.RUNNING,
    (Phase.WAIVER_PENDING, Trigger.waiver_rejected): Phase.BLOCKED,
    (Phase.DISPUTE_PENDING, Trigger.dispute_resolved): Phase.TRIAGE_PENDING,
    (Phase.BLOCKED, Trigger.fix_received): Phase.RUNNING,
    (Phase.BLOCKED, Trigger.waiver_granted): Phase.RUNNING,
    (Phase.COMPLETED, Trigger.signed_off): Phase.CERTIFIED,
}

# A run can be aborted from any non-terminal phase — generated so the table stays the one
# source of truth.
TRANSITIONS: dict[tuple[Phase, Trigger], Phase] = dict(_BASE)
for _p in Phase:
    if _p not in TERMINAL:
        TRANSITIONS[(_p, Trigger.aborted)] = Phase.ABORTED


def next_phase(current: Phase, trigger: Trigger) -> Phase:
    """The phase a run reaches after `trigger`, or raise if that move is not allowed."""
    try:
        return TRANSITIONS[(current, trigger)]
    except KeyError:
        raise IllegalTransition(current, trigger) from None


def is_terminal(phase: Phase) -> bool:
    return phase in TERMINAL
