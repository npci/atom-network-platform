# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Execution-side helpers: the readiness gate and the internal→wire status map.

Both exist because the platform's internal vocabulary and the spec's wire
vocabulary are NOT the same set, and conflating them is what produced the
"Case result [ERROR]" pills on cases that had simply never run.

Pure functions — no I/O, no DB. The orchestrator owns the loop.
"""
from __future__ import annotations

# Internal status -> spec `cert_case_result.status`.
#
# The spec allows exactly passed / failed / error. It has NO "skipped", so the
# two internal statuses that mean "did not produce a graded outcome" both land on
# `error` — which is the closest true statement available on the wire: The Authority could
# not return a result for that case. `details.reason` keeps them distinguishable
# for anyone who cares, and `results`/cert_runs keep the internal value, so the
# distinction is never actually lost — only flattened at the protocol boundary.
WIRE_STATUS = {
    "PASS": "passed",
    "FAIL": "failed",
    "SKIP": "error",
    "ERROR": "error",
}


def wire_status(internal: str) -> str:
    """Map an internal PASS/FAIL/SKIP/ERROR to the spec's vocabulary."""
    return WIRE_STATUS.get((internal or "").upper(), "error")


# Internal initiator -> spec `case_list[].initiator`. The platform's own
# vocabulary is authority|partner (domain-neutral — this is a generic
# change-management platform and NPCI is one deployment's authority); the wire
# spelling npci|bank is the shared protocol constant and cannot change
# unilaterally. Same posture as WIRE_STATUS above: internal and wire are
# different sets, mapped only at this boundary.
WIRE_INITIATOR = {"authority": "npci", "partner": "bank"}
INTERNAL_INITIATOR = {v: k for k, v in WIRE_INITIATOR.items()}


def wire_initiator(internal: str) -> str:
    """authority|partner -> the wire's npci|bank."""
    return WIRE_INITIATOR.get((internal or "").strip().lower(), "npci")


def internal_initiator(wire: str | None) -> str:
    """The wire's initiator column -> authority|partner.

    Same widening as `reporter_for`: anything the authority did not initiate
    was initiated by the counterparty, so only npci/blank map to authority.
    """
    return "authority" if (wire or "npci").strip().lower() in ("npci", "") else "partner"


def is_ready(prep: dict | None, *, initiator: str) -> bool:
    """Whether `cert_test_preparation` permits this case to execute.

    Spec, on `case_data[*].ready`:

        "authority-initiated cases: true permits execution. Bank-initiated: ignored."

    Two judgement calls the spec leaves open, both settled toward not breaking a
    live run:

      * **Absent means execute.** The spec says `true` permits; it does not say
        a missing flag forbids. Reading absence as "not ready" would take the
        current partner — which sends `case_data` with no `ready` key at all —
        from 10 executed cases to zero. So only an explicit false holds a case
        back.
      * **Bank-initiated cases ignore the flag entirely**, per the sentence
        above, so they are never gated here.
    """
    if (initiator or "npci").strip().lower() == "bank":
        return True
    if not isinstance(prep, dict) or "ready" not in prep:
        return True
    return bool(prep["ready"])


def reporter_for(initiator: str | None) -> str:
    """Which side reports a case result — the spec's binary, from a 14-value column.

    Spec: `case_list[].initiator` is `bank | npci`, and "who fires" follows it —
    bank-initiated cases are reported by the bank, authority-initiated by the Authority.

    `tbl_upi_testcases.initiatedby` is not that binary. It holds the Authority (1185),
    BANK (989), Bank (313) — and 306 rows naming a specific counterparty role:
    ACQUIRER (117), ISSUER (99), `Payer/AD bank` (36), RTSP (19), PSP (15),
    IRP (7), UserApp (6), PAYER_PSP (5), `Fx Bank` (1), REMITTER (1).

    The old test was `== "bank"`, so every one of those 306 was silently treated
    as authority-initiated — including two that literally say "bank". Anything the Authority
    did not initiate is initiated by the counterparty, so the test is
    `!= "npci"`. Blank/None stays the Authority: `case_details` already defaults it that
    way, and an unlabelled case is the Authority's to run.
    """
    return "npci" if (initiator or "npci").strip().lower() in ("npci", "") else "bank"


def apply_assertion_failures(internal_status: str, failures: list) -> str:
    """C-3's direction rule: field assertions may only take a case DOWN.

    A response-code PASS with a violated registry constraint becomes FAIL. A
    SKIP or ERROR keeps its own status and reason — those cases did not
    produce a graded outcome, and an assertion cannot promote OR reclassify
    what never ran. Never up, in any direction.
    """
    if failures and (internal_status or "").upper() == "PASS":
        return "FAIL"
    return internal_status


def case_details_payload(row: dict, *, internal_status: str) -> dict:
    """The `details` bag of a `cert_case_result`.

    The spec's example carries request/response payload refs, latency_ms,
    executed_at, stack_handled_correctly and notes. This stack has none of the
    blob plumbing those refs point at, so rather than invent URIs that resolve to
    nothing, it carries what is genuinely known: the precert transaction id, the
    expected/observed response codes, and — where the case did not run — why.

    `internal_status` is included deliberately: it is the only place the
    SKIP-vs-ERROR distinction survives onto the wire, once WIRE_STATUS has
    flattened both to "error".
    """
    details: dict = {"internal_status": internal_status}
    if row.get("txn_id"):
        details["txn_id"] = row["txn_id"]
    if row.get("expected_resp_code") is not None:
        details["expected_code"] = row["expected_resp_code"]
    if row.get("actual_resp_code") is not None:
        details["actual_code"] = row["actual_resp_code"]
    if row.get("error_message"):
        details["reason"] = row["error_message"]
    # C-3: which variant this execution exercised, and — ONLY on failure — the
    # field-level assertion failures (field + rule). Only failures travel: a
    # full outcome list would be mostly skips, and the failing rule is what
    # makes a defect notice fixable.
    if row.get("variant_id"):
        details["variant_id"] = row["variant_id"]
    if row.get("assertion_failures"):
        details["assertion_failures"] = row["assertion_failures"]
    return details
