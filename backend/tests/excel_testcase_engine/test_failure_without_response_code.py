# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""A Failure case that names no error code must be reported, not skipped.

`_check_failure_terminus` used to `return` when `stub.response_code` was empty
— bailing out on precisely the case it exists to catch, so the defect passed as
clean. Downstream, the workbook then ships that case with no `_Response code:_`
marker, `cert_catalogue` has nothing authoritative to assert, and it falls back
to scraping the first error code out of the narrative. On a partner sheet that
code is usually the AUTHORITY's rejection rather than the partner's own answer,
so the partner is failed for behaving correctly.

Change 8ebc2b48's Operator sheet reached a live certification run with 8 cases
and 0 markers this way.
"""
from app.excel_testcase_engine.agents.step_linter import _check_failure_terminus
from app.excel_testcase_engine.schemas.workbook_plan import TestCaseStub


def _stub(expected_status: str, response_code: str) -> TestCaseStub:
    return TestCaseStub(
        test_id="OP_4",
        apis=["ReqAirworthinessCheck"],
        api_type="Check",
        entities=["Operator"],
        scenario_summary="operator answers, authority rejects",
        expected_status=expected_status,
        response_code=response_code,
        coverage_tag="happy_path",
    )


STEPS_WITH_CODE = '1. Resp is returned with error code "E008".'
STEPS_WITHOUT_CODE = "1. The submission is rejected and no record is written."


def _codes(issues):
    return [i.code for i in issues]


def test_failure_case_with_no_response_code_is_reported():
    issues = []
    _check_failure_terminus(_stub("Failure", ""), STEPS_WITHOUT_CODE, set(), issues)
    assert "failure_without_response_code" in _codes(issues)


def test_it_is_reported_even_when_the_steps_mention_some_code():
    """The steps naming a code is not the same as the CASE declaring one. A
    partner-sheet case's steps routinely cite the authority's rejection code,
    which is exactly the value that must not be adopted as the expectation."""
    issues = []
    _check_failure_terminus(_stub("Failure", ""), STEPS_WITH_CODE, set(), issues)
    assert "failure_without_response_code" in _codes(issues)


def test_success_cases_are_untouched():
    issues = []
    _check_failure_terminus(_stub("Success", ""), STEPS_WITHOUT_CODE, set(), issues)
    assert issues == []


def test_declared_code_present_in_steps_is_clean():
    issues = []
    _check_failure_terminus(_stub("Failure", "E008"), STEPS_WITH_CODE,
                            {"E008"}, issues)
    assert issues == []


def test_declared_code_absent_from_steps_still_reports_the_old_defect():
    """The pre-existing rule must keep working — this fix adds a branch, it
    does not replace one."""
    issues = []
    _check_failure_terminus(_stub("Failure", "E008"), STEPS_WITHOUT_CODE,
                            {"E008"}, issues)
    assert "missing_failure_code" in _codes(issues)
    assert "failure_without_response_code" not in _codes(issues)


# NOTE: the severity ("warning", not "critical") lives in an inline dict inside
# `validator.plan_defects` and is not reachable without building a whole
# WorkbookPlan, so it is not pinned here. Warning is deliberate: the generator
# cannot invent a code the BRD never named, so failing the build would block
# kits whose source genuinely is silent — it must be visible, not fatal.
