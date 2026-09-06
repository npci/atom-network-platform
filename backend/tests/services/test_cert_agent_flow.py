# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cert lifecycle tracking and the initiator→reporter rule.

`FlowState` is the first thing to actually drive `precert_engine.state_machine`,
so these cover both that the happy path walks the spec's lifecycle and that a
wrong trigger is contained rather than fatal.
"""
from __future__ import annotations

import pytest

from app.services.cert_agent.execution import reporter_for
from app.services.cert_agent.flow import FlowState, wire_overall_state
from app.services.cert_agent.tasks import OVERALL_STATES
from app.services.precert_engine.state_machine import Phase, Trigger


# ── lifecycle ─────────────────────────────────────────────────────────────────

def test_a_clean_run_walks_the_spec_lifecycle():
    """The exact trigger sequence the orchestrator fires when nothing fails."""
    f = FlowState("CFLOW-1")
    assert f.phase is Phase.NOT_STARTED
    f.fire(Trigger.readiness_declared)
    assert f.phase is Phase.CONFIG_REQUESTED
    f.fire(Trigger.config_submitted)
    assert f.phase is Phase.CONFIG_RECEIVED
    f.fire(Trigger.setup_completed)
    assert f.phase is Phase.SETUP
    f.fire(Trigger.run_started)
    assert f.phase is Phase.RUNNING
    f.fire(Trigger.all_cases_passed)
    assert f.phase is Phase.COMPLETED
    assert [e[1] for e in f.history][-1] == "COMPLETED"


def test_a_failed_case_routes_through_triage_and_waiver_back_to_running():
    f = FlowState("CFLOW-2", phase=Phase.RUNNING)
    f.fire(Trigger.case_failed)
    assert f.phase is Phase.TRIAGE_PENDING
    f.fire(Trigger.triaged_waiver_eligible)
    assert f.phase is Phase.WAIVER_PENDING
    f.fire(Trigger.waiver_granted)
    assert f.phase is Phase.RUNNING, "a granted waiver must let the run continue"


def test_an_illegal_trigger_is_contained_not_fatal():
    """Tracking was added to a working flow; a bad trigger must not abort a run.

    The strict behaviour still exists — `next_phase` raises — but this wrapper
    logs and holds so a mistake in the orchestrator's sequence cannot take down
    a real bank's certification.
    """
    f = FlowState("CFLOW-3")
    f.fire(Trigger.all_cases_passed)          # nonsense from NOT_STARTED
    assert f.phase is Phase.NOT_STARTED
    assert f.history == [], "an illegal move must not be recorded as history"
    f.fire(Trigger.readiness_declared)        # and the flow still works afterwards
    assert f.phase is Phase.CONFIG_REQUESTED


def test_abort_is_reachable_from_anywhere_and_terminal():
    f = FlowState("CFLOW-4", phase=Phase.RUNNING)
    f.fire(Trigger.aborted)
    assert f.phase is Phase.ABORTED
    assert f.finished
    f.fire(Trigger.run_started)               # nothing follows a terminal phase
    assert f.phase is Phase.ABORTED


# ── wire vocabulary ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("phase", list(Phase))
def test_every_phase_reports_a_spec_legal_overall_state(phase):
    """No phase may leak an off-vocabulary `overall_state` onto the wire."""
    assert wire_overall_state(phase) in OVERALL_STATES


def test_the_two_engine_only_phases_map_to_their_nearest_spec_state():
    assert wire_overall_state(Phase.CERTIFIED) == "COMPLETED"
    # BLOCKED is only reachable via waiver_rejected, and the only way forward is a fix.
    assert wire_overall_state(Phase.BLOCKED) == "FIX_PENDING"


def test_overall_state_property_uses_the_wire_vocabulary():
    assert FlowState("C", phase=Phase.CERTIFIED).overall_state == "COMPLETED"
    assert FlowState("C", phase=Phase.RUNNING).overall_state == "RUNNING"


# ── initiator → reporter ──────────────────────────────────────────────────────

@pytest.mark.parametrize("initiator", ["NPCI", "npci", " NPCI ", "", None])
def test_npci_and_unlabelled_cases_are_reported_by_npci(initiator):
    assert reporter_for(initiator) == "npci"


@pytest.mark.parametrize("initiator", [
    "BANK", "Bank",
    # The 306 rows the old `== "bank"` test silently misclassified as the Authority's.
    "ACQUIRER", "ISSUER", "Payer/AD bank", "RTSP", "PSP", "IRP", "UserApp",
    "PAYER_PSP", "Fx Bank", "REMITTER",
])
def test_every_counterparty_initiator_is_reported_by_the_bank(initiator):
    assert reporter_for(initiator) == "bank"
