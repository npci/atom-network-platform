# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The precert engine's run lifecycle — the legal paths, and proof illegal moves are refused.

Every legal step lives in the TRANSITIONS table; anything absent raises IllegalTransition,
which the outer layer reports to the counterparty as `invalid_state_transition`.
"""
from __future__ import annotations

from app.services.precert_engine.state_machine import (
    IllegalTransition,
    Phase as P,
    Trigger as T,
    is_terminal,
    next_phase,
)


def _walk(start: P, steps: list[tuple[T, P]]) -> P:
    current = start
    for trigger, want in steps:
        current = next_phase(current, trigger)
        assert current == want, f"{trigger.value} -> {current.value}, wanted {want.value}"
    return current


def test_happy_path_to_certified():
    end = _walk(P.NOT_STARTED, [
        (T.readiness_declared, P.CONFIG_REQUESTED),
        (T.config_submitted, P.CONFIG_RECEIVED),
        (T.setup_completed, P.SETUP),
        (T.run_started, P.RUNNING),
        (T.all_cases_passed, P.COMPLETED),
        (T.signed_off, P.CERTIFIED),
    ])
    assert is_terminal(end)


def test_failure_then_fix_then_rerun():
    _walk(P.RUNNING, [
        (T.case_failed, P.TRIAGE_PENDING),
        (T.triaged_real_defect, P.FIX_PENDING),
        (T.fix_received, P.RUNNING),
        (T.all_cases_passed, P.COMPLETED),
    ])


def test_waiver_granted_and_rejected():
    _walk(P.TRIAGE_PENDING, [
        (T.triaged_waiver_eligible, P.WAIVER_PENDING),
        (T.waiver_granted, P.RUNNING),
    ])
    blocked = _walk(P.TRIAGE_PENDING, [
        (T.triaged_waiver_eligible, P.WAIVER_PENDING),
        (T.waiver_rejected, P.BLOCKED),
    ])
    assert not is_terminal(blocked)
    # A later grant can still unblock a rejected waiver.
    assert next_phase(P.BLOCKED, T.waiver_granted) == P.RUNNING


def test_abort_from_any_nonterminal():
    for phase in (P.CONFIG_REQUESTED, P.RUNNING, P.TRIAGE_PENDING, P.WAIVER_PENDING, P.BLOCKED):
        assert next_phase(phase, T.aborted) == P.ABORTED


def test_illegal_moves_are_refused():
    for phase, trigger in [
        (P.NOT_STARTED, T.run_started),   # cannot run before setup
        (P.RUNNING, T.signed_off),        # cannot sign off before completion
        (P.CERTIFIED, T.aborted),         # cannot abort a finished run
    ]:
        try:
            next_phase(phase, trigger)
        except IllegalTransition:
            continue
        raise AssertionError(f"expected {phase.value}+{trigger.value} to be refused")


def test_terminal_states():
    assert is_terminal(P.CERTIFIED) and is_terminal(P.ABORTED)
    assert not is_terminal(P.RUNNING)
