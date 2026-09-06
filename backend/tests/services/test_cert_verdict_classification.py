# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-5, authority half: verdict classification and the trigger ordering.

The state-machine ordering bug the first pass caught pre-ship is pinned here:
`triaged_waiver_eligible` used to fire BEFORE the verdict was chosen, so the
phase was already WAIVER_PENDING and the real-defect move was rejected as
illegal. The triage trigger now follows the classification branch, and both
walks are exercised against the REAL state machine (strict `next_phase`, not
FlowState's warn-and-hold path).
"""
from __future__ import annotations

import inspect
import re

from app.services.cert_agent.state_machine import Phase, Trigger, next_phase


def _engine_src() -> str:
    from app.services import cert_orchestrator

    return inspect.getsource(cert_orchestrator.orchestrate_cert_run_precert_engine)


# ── both walks, against the strict state machine ─────────────────────────────

def test_real_defect_walk_is_legal_end_to_end():
    """case_failed → triaged_real_defect → FIX_PENDING → fix_received → RUNNING."""
    phase = Phase.RUNNING
    phase = next_phase(phase, Trigger.case_failed)
    assert phase is Phase.TRIAGE_PENDING
    phase = next_phase(phase, Trigger.triaged_real_defect)
    assert phase is Phase.FIX_PENDING
    phase = next_phase(phase, Trigger.fix_received)
    assert phase is Phase.RUNNING


def test_waiver_walk_is_unchanged():
    phase = Phase.RUNNING
    phase = next_phase(phase, Trigger.case_failed)
    phase = next_phase(phase, Trigger.triaged_waiver_eligible)
    assert phase is Phase.WAIVER_PENDING
    phase = next_phase(phase, Trigger.waiver_granted)
    assert phase is Phase.RUNNING


def test_the_ordering_bug_is_a_real_illegal_move():
    """Why the ordering matters: from WAIVER_PENDING the real-defect trigger
    IS illegal — firing the waiver trigger first forecloses the branch."""
    import pytest

    from app.services.precert_engine.state_machine import IllegalTransition

    with pytest.raises(IllegalTransition):
        next_phase(Phase.WAIVER_PENDING, Trigger.triaged_real_defect)


# ── source pins on the orchestrator's verdict branch ─────────────────────────

def test_classification_branches_on_assertion_failures():
    src = _engine_src()
    assert '"classification": "real_defect"' in src
    assert '"classification": "waiver_eligible"' in src


def test_triage_trigger_fires_after_the_verdict_branch():
    """The branch condition is read BEFORE either triage trigger fires."""
    src = _engine_src()
    branch = src.index('_fails = row.get("assertion_failures")')
    real = src.index("flow.fire(_Trg.triaged_real_defect)")
    waiver = src.index("flow.fire(_Trg.triaged_waiver_eligible)")
    case_failed = src.index("flow.fire(_Trg.case_failed)")
    assert case_failed < branch < real
    assert branch < waiver


def test_real_defect_carries_the_whole_failure_list():
    """One verdict carries every field failure — the round is fixed in one
    pass, not one round-trip per field."""
    src = _engine_src()
    assert re.search(r'"assertion_failures":\s*_fails', src)


def test_no_waiver_exchange_on_the_real_defect_branch():
    """The real-defect branch must not grant a waiver for a genuine
    violation: CERT_WAIVER_DECISION appears only after the waiver branch."""
    src = _engine_src()
    real_branch = src.index('"classification": "real_defect"')
    waiver_decision = src.index("CERT_WAIVER_DECISION")
    waiver_branch = src.index('"classification": "waiver_eligible"')
    assert real_branch < waiver_branch < waiver_decision
