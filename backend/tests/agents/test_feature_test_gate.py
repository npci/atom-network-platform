# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""WS3a — the feature-test gate: 'no tests' is NOT a pass for a behavioural change. Models the real
fa4631e3 change (validators edited, XSD alongside, zero tests) which the old gate let through green."""
from types import SimpleNamespace

from app.agents.verification_plan import (
    feature_test_gate, _is_behavioural_src, _is_test_path, run_plan,
)


def _cs(*ops):
    return SimpleNamespace(operations=[SimpleNamespace(op=o, path=p) for o, p in ops])


def test_behavioural_change_without_test_fails():
    cs = _cs(("modify", "transaction-processor/src/main/java/com/example/tpu/service/validation/impl/ReqTransferValidator.java"),
             ("modify", "transaction-processor/src/main/java/com/example/tpu/service/validation/impl/ValidatorCommons.java"),
             ("modify", "network-domain-xsd/src/main/resources/network-common.xsd"))
    ok, why = feature_test_gate(cs)
    assert ok is False and "no test" in why


def test_behavioural_change_with_test_passes():
    cs = _cs(("modify", "x/src/main/java/com/a/PayService.java"),
             ("add", "x/src/test/java/com/a/PayServiceTest.java"))
    assert feature_test_gate(cs)[0] is True


def test_xsd_only_change_is_exempt():
    assert feature_test_gate(_cs(("modify", "network-domain-xsd/src/main/resources/network-common.xsd")))[0] is True


def test_dto_and_constants_changes_are_exempt():
    cs = _cs(("modify", "x/src/main/java/com/a/SpendCategoryDto.java"),
             ("modify", "x/src/main/java/com/a/ProdTypeConstants.java"))
    assert feature_test_gate(cs)[0] is True


def test_empty_change_is_exempt():
    assert feature_test_gate(SimpleNamespace(operations=[]))[0] is True


def test_behavioural_and_test_detection():
    assert _is_behavioural_src("a/src/main/java/X/ReqTransferValidator.java")
    assert _is_behavioural_src("a/src/main/java/X/ValidatorCommons.java")     # substring 'validator'
    assert _is_behavioural_src("a/src/main/java/X/ApiMessageAssembler.java")
    assert not _is_behavioural_src("a/src/main/java/X/SpendCategoryDto.java")  # data holder
    assert not _is_behavioural_src("a/src/main/resources/foo.xsd")            # not java
    assert not _is_behavioural_src("a/src/test/java/X/ReqTransferValidatorTest.java")  # not src/main
    assert _is_test_path("a/src/test/java/X/FooTest.java")
    assert _is_test_path("a/src/main/java/X/FooIT.java")                      # IT suffix
    assert not _is_test_path("a/src/main/java/X/Foo.java")


def test_run_plan_seeds_feature_tests_gate():
    # An empty plan runs no steps; the seeded gate alone decides the verdict.
    assert run_plan(None, "rid", [], touched_modules=set(), feature_tests_ok=False).gates["feature_tests"] is False
    assert run_plan(None, "rid", [], touched_modules=set(), feature_tests_ok=False).status == "needs_fix"
    assert run_plan(None, "rid", [], touched_modules=set(), feature_tests_ok=True).status == "verified"
    assert "feature_tests" not in run_plan(None, "rid", [], touched_modules=set()).gates  # legacy: absent
