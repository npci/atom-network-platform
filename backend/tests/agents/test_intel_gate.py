# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the references-before-Java-edit gate (§8 tool policy).

The gate is a loop-level POLICY predicate (intel_gate_reason), not part of the
edit_file mechanic — so it returns a blocking reason (or None) rather than raising.
"""
from app.core.config import settings
from app.agents.agentic_tools import RunContext, FileOp, intel_gate_reason


def _ctx(**kw) -> RunContext:
    return RunContext(run_id="r1", selected_repo_ids=["repo1"], **kw)


def _edit(path="src/main/java/Pay.java", repo_id="repo1"):
    return {"repo_id": repo_id, "path": path, "old_string": "a", "new_string": "b"}


def test_gate_on_by_default_blocks_blind_java_edit():
    reason = intel_gate_reason(_ctx(), "edit_file", _edit())
    assert reason and "impact_analysis" in reason          # default is ON now


def test_gate_off_allows_blind_edit(monkeypatch):
    monkeypatch.setattr(settings, "agentic_require_intel_before_java_edit", False)
    assert intel_gate_reason(_ctx(), "edit_file", _edit()) is None


def test_structural_intel_unlocks_edit():
    ctx = _ctx()
    ctx.intel_queried.add("symbol:processPay")             # a prior callers/impact_analysis call
    assert intel_gate_reason(ctx, "edit_file", _edit()) is None


def test_xsd_token_does_not_unlock_java_edit():
    ctx = _ctx()
    ctx.intel_queried.add("xsd:repo1:ReqTransfer.xsd")          # schema_guardian — different concern
    assert intel_gate_reason(ctx, "edit_file", _edit()) is not None


def test_non_java_and_non_edit_not_gated():
    assert intel_gate_reason(_ctx(), "edit_file", _edit("schema/ReqTransfer.xsd")) is None
    assert intel_gate_reason(_ctx(), "edit_file", _edit("pom.xml")) is None
    assert intel_gate_reason(_ctx(), "create_file", _edit()) is None
    assert intel_gate_reason(_ctx(), "read_file", _edit()) is None


def test_file_created_this_run_is_exempt():
    ctx = _ctx()
    ctx.file_ops[("repo1", "src/main/java/New.java")] = FileOp("add", "repo1", "src/main/java/New.java", "x", "h")
    # editing a file you created this run needs no blast-radius intel
    assert intel_gate_reason(ctx, "edit_file", _edit("src/main/java/New.java")) is None
