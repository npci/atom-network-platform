# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The precert engine's pass/fail rule — the truth table, spelled out so it can never
quietly change.

This is the single most consequential rule in a certification run: a test case declares the
response it EXPECTS from the bank, the bank's switch returns what it ACTUALLY did, and this
decides PASS or FAIL. It is a pure function, which is what makes a certificate reproducible
and auditable.
"""
from __future__ import annotations

from app.services.precert_engine.verdict import (
    Expectation as E,
    ResponseOutcome as O,
    ResultStatus as R,
    Verdict as V,
    decide,
)

# (name, expected, observed, wanted verdict)
CASES = [
    ("success, codes match",         E(R.SUCCESS, "00"),    O(R.SUCCESS, "00"),  V.PASSED),
    ("success, empty code = 00",     E(R.SUCCESS, "00"),    O(R.SUCCESS, ""),    V.PASSED),
    ("expected success, got fail",   E(R.SUCCESS, "00"),    O(R.FAILURE, "ZA"),  V.FAILED),
    ("failure, codes match",         E(R.FAILURE, "ZA"),    O(R.FAILURE, "ZA"),  V.PASSED),
    ("failure, codes differ",        E(R.FAILURE, "ZA"),    O(R.FAILURE, "ZM"),  V.FAILED),
    ("failure U30 match",            E(R.FAILURE, "U30"),   O(R.FAILURE, "U30"), V.PASSED),
    ("deemed accepted (DEEMED)",     E(R.DEEMED, "DEEMED"), O(R.DEEMED, ""),     V.PASSED),
    ("deemed accepted (expects 00)", E(R.SUCCESS, "00"),    O(R.DEEMED, ""),     V.PASSED),
    ("deemed rejected",              E(R.FAILURE, "ZA"),    O(R.DEEMED, ""),     V.FAILED),
    ("partial, matches",             E(R.PARTIAL, ""),      O(R.PARTIAL, ""),    V.PASSED),
    ("partial, not expected",        E(R.SUCCESS, "00"),    O(R.PARTIAL, ""),    V.FAILED),
    ("pending is not final",         E(R.SUCCESS, "00"),    O(R.PENDING, ""),    V.PENDING),
]


def test_verdict_truth_table():
    for name, expected, observed, want in CASES:
        got = decide(expected, observed)
        assert got == want, f"{name}: wanted {want.value}, got {got.value}"


def test_never_executed_is_error():
    assert decide(E(R.SUCCESS, "00"), None) is V.ERROR
