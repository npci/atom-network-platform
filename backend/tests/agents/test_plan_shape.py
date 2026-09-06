# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the Phase-B plan-shape safety net (research-canvas vs implementation plan)."""
from app.agents.agentic_subagents import _plan_looks_research_shaped, _ANALYSIS_PREFACE


def test_research_canvas_plan_is_flagged():
    canvas = ("network-core: REFERENCED ONLY, not modified. Delegation is NOT MODELLED TODAY; "
              "Corporate Circle is a net-new construct described for future design in the Product Canvas.")
    assert _plan_looks_research_shaped(canvas) is True


def test_concrete_implementation_plan_is_not_flagged():
    impl = ("Create SpendLimitService.java in dataaccessor. Modify ReqTransferValidator.java to call it. "
            "Add to ValidatorCommons a validateSpendLimit method. Edit network-common.xsd to add the element.")
    assert _plan_looks_research_shaped(impl) is False


def test_empty_plan_is_not_flagged():
    assert _plan_looks_research_shaped("") is False
    assert _plan_looks_research_shaped(None) is False


def test_preface_forbids_reference_only_framing():
    # Fix 1: the analysis agent is told to build now, not produce a canvas.
    assert "IMPLEMENTATION, NOT A CANVAS" in _ANALYSIS_PREFACE
    assert "reference only" in _ANALYSIS_PREFACE.lower()
    assert "FAILED plan" in _ANALYSIS_PREFACE
