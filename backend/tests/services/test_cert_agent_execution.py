# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The readiness gate and the internal→wire status map.

These two decide, respectively, whether a case runs at all and how its outcome
reads to the bank — so both failure modes here are silent-and-wrong rather than
loud, which is why they get their own tests.
"""
from __future__ import annotations

import pytest

from app.services.cert_agent.execution import case_details_payload, is_ready, wire_status


# ── wire vocabulary ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(("internal", "expected"), [
    ("PASS", "passed"),
    ("FAIL", "failed"),
    ("SKIP", "error"),    # the spec has no "skipped"
    ("ERROR", "error"),
    ("pass", "passed"),   # case-insensitive
    ("", "error"),        # unknown degrades to error, never to a pass
    (None, "error"),
])
def test_wire_status_is_the_spec_vocabulary(internal, expected):
    assert wire_status(internal) == expected


def test_wire_status_never_emits_a_non_spec_value():
    assert set(wire_status(s) for s in ("PASS", "FAIL", "SKIP", "ERROR", "weird")) <= {
        "passed", "failed", "error"}


def test_skip_and_error_stay_distinguishable_in_details():
    """Both flatten to `error` on the wire; `details` is what keeps them apart."""
    skipped = case_details_payload(
        {"test_case_id": "BE_22", "error_message": "unverifiable: no respVal"},
        internal_status="SKIP")
    broke = case_details_payload({"test_case_id": "X", "error_message": "boom"},
                                 internal_status="ERROR")
    assert skipped["internal_status"] == "SKIP"
    assert "unverifiable" in skipped["reason"]
    assert broke["internal_status"] == "ERROR"


def test_details_carry_what_is_known_and_invent_nothing():
    d = case_details_payload(
        {"txn_id": "MYB123", "expected_resp_code": "00", "actual_resp_code": "00"},
        internal_status="PASS")
    assert d["txn_id"] == "MYB123"
    assert d["expected_code"] == "00" and d["actual_code"] == "00"
    # No fabricated blob refs for plumbing this stack does not have.
    assert "request_payload_ref" not in d and "response_payload_ref" not in d
    assert "reason" not in d


# ── readiness gate ────────────────────────────────────────────────────────────

def test_absent_ready_flag_still_executes():
    """The current partner sends case_data with no `ready` key at all.

    Reading absence as "not ready" would take a live run from 10 executed cases
    to zero, so only an explicit false holds a case back.
    """
    assert is_ready({}, initiator="npci") is True
    assert is_ready(None, initiator="npci") is True
    assert is_ready({"amount": "100.00"}, initiator="npci") is True


def test_explicit_false_holds_an_npci_case_back():
    assert is_ready({"ready": False}, initiator="npci") is False
    assert is_ready({"ready": True}, initiator="npci") is True


def test_bank_initiated_cases_ignore_the_flag():
    """Spec: "authority-initiated cases: true permits execution. Bank-initiated: ignored.""" ""
    assert is_ready({"ready": False}, initiator="bank") is True
    assert is_ready({"ready": False}, initiator="BANK") is True
