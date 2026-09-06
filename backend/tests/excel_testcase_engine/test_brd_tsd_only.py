# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""BRD/TSD-only cert engine — pure-Python invariants.

The larger v1 test suite (test_excel_engine_v1.py) was scrapped in the
BRD/TSD-only refactor: every class in it exercised helpers (npci_specs,
xsd_diff, coverage_matrix, scope_ownership) that no longer exist. These
replacement tests cover the smaller set of invariants the new engine keeps.
"""
from __future__ import annotations

import pytest

from app.excel_testcase_engine.schemas.enriched_brief import EnrichedBrief
from app.excel_testcase_engine.schemas.workbook_plan import (
    SheetSpec, TestCaseStub, WorkbookPlan,
)


class TestSchemaLoosening:
    """coverage_tag is free-form; response_code accepts BRD-authored codes."""

    def test_coverage_tag_accepts_tsd_authored_slug(self):
        stub = TestCaseStub(
            test_id="PPS_1", apis=["ReqNovelPay"], api_type="Pay",
            entities=["Payer PSP"], scenario_summary="TSD scenario",
            expected_status="Failure", response_code="X99",
            coverage_tag="duplicate_vpa",   # not in the recommended set
        )
        assert stub.coverage_tag == "duplicate_vpa"

    def test_response_code_accepts_new_brd_code(self):
        stub = TestCaseStub(
            test_id="PPS_1", apis=["ReqNovelPay"], api_type="Pay",
            entities=["Payer PSP"], scenario_summary="TSD scenario",
            expected_status="Failure", response_code="X99",
            coverage_tag="happy_path",
        )
        assert stub.response_code == "X99"

    def test_response_code_empty_is_valid(self):
        stub = TestCaseStub(
            test_id="PPS_1", apis=["ReqTransfer"], api_type="Pay",
            entities=["Payer PSP"], scenario_summary="Ambiguous scenario",
            expected_status="Failure", response_code="",
            coverage_tag="happy_path",
        )
        assert stub.response_code == ""


class TestEnrichedBriefTsdDriven:
    """api_classification is free-form and defaults to tsd_driven."""

    def test_default_classification_is_tsd_driven(self):
        b = EnrichedBrief(original_brief="anything")
        assert b.api_classification == "tsd_driven"

    def test_accepts_legacy_classification_values(self):
        # Old cached briefs pre-refactor may carry these; they must
        # deserialise cleanly (field is no longer a Literal).
        for val in ("existing_modified", "new_api", "mixed", "unknown"):
            b = EnrichedBrief(original_brief="anything", api_classification=val)
            assert b.api_classification == val


class TestPlannerBrdCodeExtraction:
    """_codes_from_brd reads the Authority-style tokens out of the BRD text."""

    def test_extracts_bare_codes(self):
        from app.excel_testcase_engine.agents.planner import _codes_from_brd
        brief = EnrichedBrief(
            original_brief="On decline, RespTransfer returns ZM. On timeout U09.",
        )
        codes = _codes_from_brd(brief)
        assert "ZM" in codes
        assert "U09" in codes
        assert "NPCI" not in codes   # stopword

    def test_empty_brd_returns_empty(self):
        from app.excel_testcase_engine.agents.planner import _codes_from_brd
        brief = EnrichedBrief(original_brief="")
        assert _codes_from_brd(brief) == frozenset()


class TestGraphWiring:
    """flow_generate node is gone; planner → strategy directly."""

    def test_flow_generate_removed(self):
        from app.excel_testcase_engine.orchestrator import graph as g
        # No node function, no import.
        assert not hasattr(g, "node_flow_generate")
        assert "flow_generator" not in g.__dict__


class TestStepLinterTsdAllowlist:
    """lint_plan uses the caller-provided allowlist verbatim; a stub's own
    apis and response_code always pass through."""

    def test_stub_declared_api_never_flagged(self):
        from app.excel_testcase_engine.agents.step_linter import lint_plan
        from app.excel_testcase_engine.schemas.workbook_plan import RenderedTestCase
        plan = WorkbookPlan(
            filename="test.xlsx", archetype="A",
            sheets=[SheetSpec(
                name="Payer PSP", layout="A1",
                test_cases=[TestCaseStub(
                    test_id="PPS_1", apis=["ReqNovelPay", "RespNovelPay"],
                    api_type="Pay", entities=["Payer PSP"],
                    scenario_summary="new api scenario",
                    expected_status="Success", response_code="",
                    coverage_tag="happy_path",
                    rendered=RenderedTestCase(
                        test_id="PPS_1",
                        details_block="API Involved: ReqNovelPay, RespNovelPay\nType : Pay\nEntity Involved: Payer PSP",
                        description_block="To verify the new API happy path.",
                        steps_block="1. ReqNovelPay sent.\n2. RespNovelPay returned with result-SUCCESS.",
                    ),
                )],
            )],
        )
        report = lint_plan(plan)
        # A stub's own APIs should not be linted as unknown.
        issues = report.for_stub("PPS_1")
        assert not any(i.code == "invalid_api_in_steps" for i in issues), issues
