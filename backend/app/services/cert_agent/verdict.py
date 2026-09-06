# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The pass/fail check — the single most important rule in the system.

A certification test case says what response it EXPECTS from the bank; the bank's switch
returns what it ACTUALLY did. This decides PASS or FAIL by comparing the two.

It is a pure function: same inputs, same answer, every time — which is what makes a
certificate auditable and reproducible. Nothing here touches a database, the network, or an
LLM. Sourced from the UPI response model (a result category + a response code), e.g. a test
that expects {result: SUCCESS, code: "00"} passes only if the bank returns exactly that.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResultStatus(str, Enum):
    SUCCESS = "SUCCESS"   # completed OK (response code is normally "00")
    FAILURE = "FAILURE"   # declined, with a reason code (e.g. "ZA", "U30")
    DEEMED = "DEEMED"     # switch acted even though the bank stayed silent
    PENDING = "PENDING"   # awaiting a deferred confirmation — not final yet
    PARTIAL = "PARTIAL"   # partially completed


class Verdict(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    PENDING = "PENDING"   # cannot be judged yet; re-check after the confirmation
    ERROR = "ERROR"       # the case could not be executed at all


@dataclass(frozen=True)
class Expectation:
    """What the test case expects the bank to return."""
    result_status: ResultStatus
    code: str


@dataclass(frozen=True)
class ResponseOutcome:
    """What the bank's switch actually returned."""
    result_status: ResultStatus
    code: str = ""


def normalized_code(outcome: ResponseOutcome) -> str:
    """A SUCCESS with no explicit code means the success code '00'."""
    if outcome.code:
        return outcome.code
    return "00" if outcome.result_status is ResultStatus.SUCCESS else outcome.code


def decide(expected: Expectation, observed: ResponseOutcome | None) -> Verdict:
    """Compare what was expected to what actually happened, and return the verdict."""
    if observed is None:
        return Verdict.ERROR                       # never ran
    if observed.result_status is ResultStatus.PENDING:
        return Verdict.PENDING                     # not final yet
    if observed.result_status is ResultStatus.DEEMED:
        ok = expected.result_status is ResultStatus.DEEMED or expected.code in ("DEEMED", "00")
        return Verdict.PASSED if ok else Verdict.FAILED
    if observed.result_status is ResultStatus.PARTIAL:
        return Verdict.PASSED if expected.result_status is ResultStatus.PARTIAL else Verdict.FAILED
    # SUCCESS or FAILURE → the response code is the decider.
    return Verdict.PASSED if normalized_code(observed) == expected.code else Verdict.FAILED
