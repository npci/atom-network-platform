# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Test the structured XSD diff rendering for the Phase B handoff (§7.4 plan-fidelity)."""
from app.agents.agentic_subagents import _render_xsd_diff


def test_renders_structured_lines_not_dict_repr():
    rec = {
        "core:ReqTransfer.xsd": {"new": ["Foo"], "modified": ["Bar"], "deprecated": []},
        "core:RespTransfer.xsd": {"new": [], "modified": [], "deprecated": ["Old"]},
    }
    out = _render_xsd_diff(rec)
    assert "ReqTransfer.xsd" in out and "NEW: Foo" in out and "MODIFIED: Bar" in out
    assert "DEPRECATED: Old" in out
    assert "{" not in out and "'new'" not in out      # not a python dict repr


def test_empty_and_none_safe():
    assert _render_xsd_diff({}) == ""
    assert _render_xsd_diff(None) == ""


def test_no_changes_reads_clearly():
    out = _render_xsd_diff({"x:A.xsd": {"new": [], "modified": [], "deprecated": []}})
    assert "no element changes" in out
    assert "A.xsd" in out
