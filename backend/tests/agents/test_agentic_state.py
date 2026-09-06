# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pure-logic tests for the agentic state machine (no DB).

DB-backed behaviour (create_run dup-prevention, lease races, recovery,
event seq) is exercised by the integration smoke in S2 verification; here we
cover the deterministic pieces: the transition table and secret redaction.
"""
from app.agents.agentic_state import (
    VALID_TRANSITIONS,
    _can_transition,
    _TERMINAL_PHASES,
)
from app.agents.agentic_events import redact
from app.models.agentic import AgenticPhase as P


def test_terminal_phases_have_no_outgoing_edges():
    for phase in _TERMINAL_PHASES:
        if phase is P.COMPLETED:
            # The one sanctioned exception: a deferred-push approval completes the
            # run, and "Push to git now" re-opens it for its single remote write.
            assert VALID_TRANSITIONS[phase] == {P.PUSHING}
            continue
        assert VALID_TRANSITIONS[phase] == set()


def test_every_phase_is_in_the_transition_table():
    assert set(VALID_TRANSITIONS) == set(P)


def test_representative_legal_transitions():
    assert _can_transition("pending", P.WORKSPACE_READY)
    assert _can_transition("verification", P.CODE_CHANGE)   # gate-fail loop
    assert _can_transition("review", P.CODE_CHANGE)         # blocking-finding loop
    assert _can_transition("awaiting_human_approval", P.PUSHING)
    assert _can_transition("awaiting_human_approval", P.REBASE_REVERIFY)
    assert _can_transition("pushing", P.REBASE_REVERIFY)    # base-SHA drift


def test_representative_illegal_transitions():
    assert not _can_transition("pending", P.VERIFICATION)   # no skipping phases
    assert not _can_transition("completed", P.CODE_CHANGE)  # terminal is a sink
    assert not _can_transition("pushing", P.CODE_CHANGE)
    assert not _can_transition("bogus_phase", P.CODE_CHANGE)


def test_redact_masks_common_secret_shapes():
    assert "[REDACTED]" in redact("PRIVATE-TOKEN: glpat-abc123def456")
    assert "[REDACTED]" in redact("Authorization: Bearer eyJhbGciOi.J9.sig")
    assert "[REDACTED]" in redact("export GITLAB_API_KEY=supersecretvalue")
    assert "[REDACTED]" in redact("MY_DB_PASSWORD=hunter2")
    assert redact("https://user:s3cr3t@gitlab.example.com/x.git") == \
        "https://user:[REDACTED]@gitlab.example.com/x.git"


def test_redact_leaves_clean_text_untouched():
    clean = "Running upgrade 0066 -> 0067; compiled OK in 12s"
    assert redact(clean) == clean


def test_verification_gate_transitions():
    # After 3 failed auto-verifications, VERIFICATION parks at the human gate
    # (not a silent give-up), and the human can retry (→CODE_CHANGE) or skip (→REVIEW).
    assert _can_transition(P.VERIFICATION, P.AWAITING_VERIFY_DECISION)
    assert _can_transition(P.AWAITING_VERIFY_DECISION, P.CODE_CHANGE)   # "try once more"
    assert _can_transition(P.AWAITING_VERIFY_DECISION, P.REVIEW)        # "skip & proceed"
    assert not _can_transition(P.AWAITING_VERIFY_DECISION, P.PUSHING)   # never straight to push


def test_governance_stage_transitions():
    # gov_* stage runs branch CONTEXT_READY → REVIEW (no codegen phases) and a clean
    # review (zero staged fixes) short-circuits REVIEW → COMPLETED without a gate.
    assert _can_transition("context_ready", P.REVIEW)
    assert _can_transition("review", P.COMPLETED)
    # The codegen loop edges are untouched.
    assert _can_transition("review", P.CODE_CHANGE)
    assert _can_transition("review", P.AWAITING_HUMAN_APPROVAL)


def test_governance_unverified_fixes_park_edge():
    # A gov_* stage whose fix budget is spent parks its fix-delta manifest for the
    # human straight from VERIFICATION (the unverified-fixes park).
    assert _can_transition("verification", P.AWAITING_HUMAN_APPROVAL)
