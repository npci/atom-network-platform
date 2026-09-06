# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Legacy test-compile fail-open (verification_plan.legacy_test_compile_reason).

Policy under test: a feature-test step that dies because PRE-EXISTING test sources
don't compile (files the change never touched) is skipped visibly instead of failing
required_tests — Maven test-compiles all of src/test/java before running anything,
so one rotten legacy file would otherwise loop the code agent on errors it must not
fix. Failures of the change's OWN tests still gate normally.
"""
from app.agents.verification_plan import legacy_test_compile_reason

OWN = ["SplitAssemblerInvariantTest.java", "ReqSplitPayValidatorTest.java",
       "SplitAssemblerInvariantTest.kt", "ReqSplitPayValidatorTest.kt"]

LEGACY_COMPILE_ERROR = """
[INFO] Compiling 42 test sources to /w/transaction-processor/target/test-classes
[ERROR] /w/transaction-processor/src/test/java/com/example/tpu/OldFlowTest.java:[12,8] cannot find symbol
[ERROR] /w/transaction-processor/src/test/java/com/example/tpu/LegacyUtilTest.java:[7,1] package org.gone does not exist
[INFO] BUILD FAILURE
"""

OWN_COMPILE_ERROR = """
[ERROR] /w/transaction-processor/src/test/java/com/example/tpu/SplitAssemblerInvariantTest.java:[31,9] ';' expected
[INFO] BUILD FAILURE
"""

SUREFIRE_FAILURE = """
[ERROR] Failures:
[ERROR]   SplitAssemblerInvariantTest.moneyConservation:88 expected:<150000> but was:<149999>
[INFO] BUILD FAILURE
"""


def test_legacy_only_compile_break_returns_reason():
    reason = legacy_test_compile_reason(LEGACY_COMPILE_ERROR, OWN)
    assert reason
    assert "OldFlowTest.java" in reason and "LegacyUtilTest.java" in reason
    assert "fail-open" in reason


def test_own_test_compile_break_still_gates():
    # The change's own test file failing to compile is the agent's problem — no fail-open.
    assert legacy_test_compile_reason(OWN_COMPILE_ERROR, OWN) == ""


def test_mixed_break_still_gates():
    # If the change's own file is among the offenders, gate normally even when
    # legacy files are broken too — the agent must fix its own file regardless.
    assert legacy_test_compile_reason(LEGACY_COMPILE_ERROR + OWN_COMPILE_ERROR, OWN) == ""


def test_genuine_test_failure_still_gates():
    # Surefire failure lines carry class#method, not /src/test/ source paths —
    # a real red test must keep failing required_tests.
    assert legacy_test_compile_reason(SUREFIRE_FAILURE, OWN) == ""


def test_empty_and_garbage_are_safe():
    assert legacy_test_compile_reason("", OWN) == ""
    assert legacy_test_compile_reason(None, OWN) == ""
    assert legacy_test_compile_reason(LEGACY_COMPILE_ERROR, None) != ""   # no own files known → legacy break
    assert legacy_test_compile_reason("[ERROR] random noise", OWN) == ""
