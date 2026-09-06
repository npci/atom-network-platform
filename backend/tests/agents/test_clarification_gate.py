# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Clarification-gate anti-hallucination hardening.

The purpose-code incident: the analysis agent invented a code value in an
ask_clarifications option ("BT, after BR/BS which are the most recently used" — BR/BS do
not exist and BT sat bound in a constants file the agent had read 38 iterations earlier),
marked it recommended, a PM clicked it, and the click became a "human-ratified" binding
ledger directive. These tests pin the gate that makes that path impossible:

* a value-proposing option with no verified evidence is REFUSED (not recorded);
* every proposed value is occupancy-checked SERVER-SIDE and the result rides the option;
* a recommendation cannot ride on a value found occupied (or unscannable);
* the ledger records whether the PM typed the answer or clicked an agent suggestion,
  and the decisions block renders that provenance.
"""
from types import SimpleNamespace

from app.agents import agentic_tools as T
from app.agents.agentic_tools import RunContext, ask_clarifications, _value_occupancy


def _fake_adapter(stdout="", exit_code=0, timed_out=False, calls=None):
    def run_command(root, argv):
        if calls is not None:
            calls.append((root, argv))
        return SimpleNamespace(stdout=stdout, stderr="", exit_code=exit_code,
                               timed_out=timed_out)
    return SimpleNamespace(run_command=run_command)


def _patch_repo(monkeypatch, adapter):
    monkeypatch.setattr(T, "_repo_root", lambda ctx, rid: f"/clones/{rid}")
    monkeypatch.setattr(T, "adapter", adapter)


def _value_question(recommended="opt-bt"):
    return {
        "id": "purpose-code-value",
        "text": "Which value should the new purpose code take?",
        "options": [
            {"id": "opt-bt", "label": "Use BT (next free alpha code)",
             "consequence": "BT is allocated to this feature",
             "proposed_value": "BT"},
            {"id": "opt-defer", "label": "the Authority Scheme assigns the value later",
             "consequence": "value allocated by the authority at onboarding"},
        ],
        "recommended": recommended,
        "evidence": [{"claim": "existing purpose constants",
                      "file": "src/CommonConstant.java"}],
    }


# ── refusal: value proposals must be grounded in files actually read ────────────

def test_value_option_without_evidence_is_bounced():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    q = _value_question()
    q.pop("evidence")                     # value proposed, nothing cited
    out = ask_clarifications(ctx, [q])
    assert ctx.proposal is None and ctx.awaiting_decision is False
    assert "not recorded" in out.lower() and "BT" in out
    assert "defer" in out.lower()         # the no-invention escape hatch is offered


def test_value_option_with_unread_citation_is_bounced():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    # cited file was never read this run → citation unverified → refuse
    out = ask_clarifications(ctx, [_value_question()])
    assert ctx.proposal is None and "not recorded" in out.lower()


# ── occupancy: server-side check rides the option; recommendation is conditional ──

def test_occupied_value_strips_recommendation_and_attaches_evidence(monkeypatch):
    calls = []
    _patch_repo(monkeypatch, _fake_adapter(
        stdout='src/CommonConstant.java:56:    P2M_DEEMED_RESP_CODE = "BT";', calls=calls))
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    ctx.read_files = {("core", "src/CommonConstant.java")}
    out = ask_clarifications(ctx, [_value_question()])

    assert ctx.awaiting_decision is True
    q = ctx.proposal["questions"][0]
    opt = q["options"][0]
    assert opt["occupancy"]["hits"] == 1 and opt["occupancy"]["complete"] is True
    assert "CommonConstant.java" in opt["occupancy"]["sample"][0]
    assert "NOT confirmed" in opt["consequence"]          # PM sees the verdict inline
    assert q["recommended"] is None                        # endorsement removed
    assert "recommendation removed" in out
    # the sweep searched the literal as a fixed quoted string, not a regex
    argv = calls[0][1]
    assert "-F" in argv and '"BT"' in argv and "'BT'" in argv


def test_clean_value_keeps_recommendation_with_negative_scan_marker(monkeypatch):
    _patch_repo(monkeypatch, _fake_adapter(stdout="", exit_code=1))   # git grep: no matches
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    ctx.read_files = {("core", "src/CommonConstant.java")}
    ask_clarifications(ctx, [_value_question()])
    q = ctx.proposal["questions"][0]
    assert q["options"][0]["occupancy"]["hits"] == 0
    assert q["recommended"] == "opt-bt"                    # negative scan → may stand
    assert "no quoted occurrence" in q["options"][0]["consequence"]
    assert q["verified_evidence"]                          # provenance for UI + ledger


def test_unscannable_repo_reads_as_unverified_not_clean(monkeypatch):
    _patch_repo(monkeypatch, _fake_adapter(stdout="", exit_code=2))   # scan failed
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    ctx.read_files = {("core", "src/CommonConstant.java")}
    ask_clarifications(ctx, [_value_question()])
    q = ctx.proposal["questions"][0]
    assert q["options"][0]["occupancy"]["complete"] is False
    assert q["recommended"] is None                        # can't-check ≠ free
    assert "INCOMPLETE" in q["options"][0]["consequence"]


def test_value_occupancy_empty_literal_is_incomplete():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    occ = _value_occupancy(ctx, "  ")
    assert occ["hits"] == 0 and occ["complete"] is False


# ── the general case is untouched: business questions need no evidence ───────────

def test_plain_business_question_records_as_before():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    out = ask_clarifications(ctx, [{
        "id": "scope", "text": "Launch for P2P only or all flows?",
        "options": [{"id": "p2p", "label": "P2P only"},
                    {"id": "all", "label": "All flows"}],
        "recommended": "p2p",
    }])
    assert ctx.awaiting_decision is True
    assert ctx.proposal["questions"][0]["recommended"] == "p2p"
    assert "occupancy-checked" not in out


# ── ledger provenance: clicked suggestion ≠ typed answer, and the block says so ──

def test_provenance_tag_renders_origin_and_occupancy():
    from app.services.decision_ledger import _provenance_tag
    def entry(**da):
        return SimpleNamespace(kind="clarification", decided_against=da)

    assert _provenance_tag(entry(origin="human_typed")) == " (PM-provided answer)"
    assert _provenance_tag(entry(origin="llm_option")) == " (PM selected an agent-suggested option)"
    occupied = _provenance_tag(entry(
        origin="llm_option", occupancy={"value": "BT", "hits": 2, "complete": True}))
    assert "'BT'" in occupied and "2 code location(s)" in occupied
    unscanned = _provenance_tag(entry(
        origin="llm_option", occupancy={"value": "80", "hits": 0, "complete": False}))
    assert "could not fully scan" in unscanned and "'80'" in unscanned
    # pre-provenance entries and other kinds render nothing
    assert _provenance_tag(SimpleNamespace(kind="clarification", decided_against=None)) == ""
    assert _provenance_tag(SimpleNamespace(kind="plan_ratification",
                                           decided_against={"origin": "llm_option"})) == ""
