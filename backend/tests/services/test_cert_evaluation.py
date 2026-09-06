# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-3: the evaluation glue — direction rule, wire details, round unity.

The orchestrator's own async path (scope narrowing, the run loop) is
DELIBERATELY not unit-tested here — it needs precert and a live partner, and a
faked one would test the fake. What is testable is pinned: the pure direction
rule, the details payload both sides read, and source-level pins that the
round has ONE source of truth and the scope comes from the builder.
"""
from __future__ import annotations

import inspect

from app.services.cert_agent.execution import (
    apply_assertion_failures, case_details_payload, internal_initiator,
    wire_initiator,
)

FAILS = [{"field": "ReqTransfer/Amt/@value", "kind": "length",
          "expected": {"length_rule": "Max Length 10"}, "reason": "over"}]


# ── the direction rule: down, never up ───────────────────────────────────────

def test_assertion_failures_take_a_pass_down():
    assert apply_assertion_failures("PASS", FAILS) == "FAIL"


def test_no_failures_change_nothing():
    assert apply_assertion_failures("PASS", []) == "PASS"


def test_skip_and_error_keep_their_status_and_reason():
    """A case that never produced a graded outcome cannot be promoted OR
    reclassified by field assertions."""
    assert apply_assertion_failures("SKIP", FAILS) == "SKIP"
    assert apply_assertion_failures("ERROR", FAILS) == "ERROR"


def test_a_fail_stays_a_fail():
    assert apply_assertion_failures("FAIL", FAILS) == "FAIL"
    assert apply_assertion_failures("FAIL", []) == "FAIL"


# ── the wire details: variant attribution + only-failures ────────────────────

def test_details_carry_variant_and_assertion_failures():
    row = {"test_case_id": "TC1", "status": "FAIL", "txn_id": "T1",
           "variant_id": "vabc123", "assertion_failures": FAILS}
    details = case_details_payload(row, internal_status="FAIL")
    assert details["variant_id"] == "vabc123"
    assert details["assertion_failures"] == FAILS


def test_details_omit_absent_failures_and_variant():
    details = case_details_payload({"test_case_id": "TC1", "status": "PASS"},
                                   internal_status="PASS")
    assert "assertion_failures" not in details
    assert "variant_id" not in details


# ── the wire-boundary vocabulary map ─────────────────────────────────────────

def test_initiator_maps_between_internal_and_wire_vocabulary():
    assert wire_initiator("authority") == "npci"
    assert wire_initiator("partner") == "bank"
    assert internal_initiator("npci") == "authority"
    assert internal_initiator("bank") == "partner"
    # The 306-row lesson from reporter_for: anything the authority did not
    # initiate was initiated by the counterparty.
    assert internal_initiator("ACQUIRER") == "partner"
    assert internal_initiator(None) == "authority"


# ── source pins on the orchestrator ──────────────────────────────────────────

def _engine_src() -> str:
    from app.services import cert_orchestrator

    return inspect.getsource(cert_orchestrator.orchestrate_cert_run_precert_engine)


def test_single_round_source_of_truth():
    """The first pass had THREE notions of the round, two frozen at 1 —
    cert_case_specs is keyed on (cflow_id, run_number), so that strands every
    round after the first. Everything must read `_round`."""
    src = _engine_src()
    assert "cert_attempt=_round" in src
    assert "attempt=_round" in src
    assert "run_number=_round" in src
    assert "current_round=_round" in src
    for frozen in ("cert_attempt=1", "attempt=1", "run_number=1", "current_round=1"):
        assert frozen not in src, f"a frozen round notion is back: {frozen}"


def test_scope_is_derived_through_the_case_builder():
    """The executed scope must come FROM the builder, never be hand-rolled in
    the orchestrator. (The build+store+index sequence now lives in
    `cert_case_builder.derive_round_scope`, so the pin follows it there rather
    than looking for the individual calls inline.)"""
    import inspect

    from app.services import cert_case_builder

    src = _engine_src()
    assert "cert_case_builder.derive_round_scope(" in src
    assert "round_scope.case_ids" in src, "the executed set must narrow to the build"

    derive_src = inspect.getsource(cert_case_builder.derive_round_scope)
    assert "build(" in derive_src and "store(" in derive_src


def test_grading_resolves_the_codec_from_the_stored_row():
    """Format is DATA: evaluation resolves `codec_for(row.wire_format)` — the
    snapshot — never the pack at evaluation time."""
    src = _engine_src()
    assert "_codec_for(_variant.wire_format)" in src
    assert "wire_format_of" not in src


def test_down_only_goes_through_the_named_rule():
    src = _engine_src()
    assert "apply_assertion_failures(" in src
