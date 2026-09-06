# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""XSD-Discovery + Code-Change subagents (§4/§9). Prompt building is pure; the
runners drive the loop with a scripted LLM against a real local git workspace."""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.llm import ToolUseRequest, ClaudeToolTurn
from app.agents.context_assembler import ContextPack
from app.agents import agentic_runtime as RT
from app.agents import agentic_subagents as S

RID = "repo-1"
RUN = "run-1"


# ── prompt building (pure) ────────────────────────────────────────────────────

def test_rectification_clause_is_comply_first_and_party_flows():
    # Comply-first at the plan loop: a PM rectification is a binding functional choice —
    # feasibility-checked, applied (never overridden), repercussions on record — and every
    # API it touches must arrive with its party route in flow_spec.
    #
    # The route requirement is spelled `party_flows`, NOT "four-party": the preface now
    # states many UPI flows involve only 2-3 parties and must say so "rather than padding
    # to four", so asserting the old wording would push the prompt back to a shape it
    # deliberately moved away from.
    c = S._RECTIFICATION_CLAUSE
    assert "FUNCTIONAL choice" in c and "user_rectifications" in c and "repercussions" in c
    assert "do NOT override" in c
    assert "party_flows" in c
    assert "party_flows" in S._ANALYSIS_PREFACE     # required on first-pass plans too


def test_analysis_preface_lists_external_counterparty_dimension():
    # The critical-decision dimension list is the ONLY place the ratification gate's
    # coverage duty is defined — dropping a dimension silently un-gates it.
    assert "external_counterparty_contract" in S._ANALYSIS_PREFACE
    for dim in ("settlement_model", "money_movement_legs", "atomicity_mechanism",
                "idempotency_keys", "event_ordering", "expiry_semantics",
                "error_codes", "backward_compat_scope"):
        assert dim in S._ANALYSIS_PREFACE


def test_system_segments_carry_context_and_bounded_cache():
    ctx = ContextPack(
        selected_repo_ids=[RID],
        brd_sections={"1. Overview": "foo", "10. Regulatory & Compliance": "RB01 declined"},
        tsd_sections={"5. API": "POST /refund/status"},
        module_notes={RID: "xsd-domain holds the schemas"},
        stale_index={RID: True},
        impact_files=["network-core/A.java"],
    )
    segs = S.build_system_segments(ctx, S._CODE_PREFACE)
    blob = "\n".join(s["text"] for s in segs)
    assert "Code-Change agent" in blob
    assert "10. Regulatory & Compliance" in blob and "RB01" in blob   # regulatory survives end-to-end
    assert "5. API" in blob and "xsd-domain" in blob
    assert "Index may be STALE" in blob and "network-core/A.java" in blob
    # §9 static standards appended — the exact-quantity rule is pack data
    # (UPI: "integer paise"; NLLN: loan periods), so assert the invariant
    # tail every pack's rule carries rather than one domain's word.
    assert "never floating point" in blob and "DO-NOT-EDIT" in blob
    cached = sum(1 for s in segs if s.get("cache_control"))
    assert 1 <= cached <= 3      # leaves a breakpoint for the tools cache (S5)


def test_docs_block_is_outline_plus_seed_not_full_dump():
    ctx = ContextPack(
        selected_repo_ids=[RID],
        brd_sections={"1. Overview": "scope text only here",
                      "10. Regulatory & Compliance": "RB01 declined",
                      "7. Refund Flow": "refund details here"},
        tsd_sections={"5. API": "POST /refund/status"},
        intent="add a refund status field",
    )
    blob = "\n".join(s["text"] for s in S.build_system_segments(ctx, S._CODE_PREFACE))
    for h in ("1. Overview", "10. Regulatory & Compliance", "7. Refund Flow", "5. API"):
        assert h in blob                       # every heading is in the outline
    assert "RB01 declined" in blob             # compliance section force-seeded (body present)
    assert "refund details here" in blob       # intent-matching heading seeded (body present)
    assert "scope text only here" not in blob  # non-matching body stays behind the pull
    assert "read_doc" in blob                  # prompt tells the model how to pull


def test_seed_prioritises_compliance_over_intent_under_budget():
    # 6 intent-matching sections (enough to fill the section cap in document order) followed by a
    # compliance section LAST. Compliance must still be force-seeded — the shared seed budget can
    # never drop a MANDATORY section in favour of an intent match (regulatory-safety for payments).
    brd = {f"{i}. Refund detail {i}": f"refund body {i}" for i in range(1, 7)}
    brd["99. Regulatory & Compliance"] = "RB99 mandatory error handling"
    ctx = ContextPack(selected_repo_ids=[RID], brd_sections=brd, intent="refund")
    blob = "\n".join(s["text"] for s in S.build_system_segments(ctx, S._CODE_PREFACE))
    assert "RB99 mandatory error handling" in blob   # compliance body seeded despite being last + over cap
    # And a pure-compliance section with NO intent overlap is still seeded.
    only = ContextPack(selected_repo_ids=[RID],
                       brd_sections={"x. Validation Rules": "VR-7 amount must be paise"}, intent="unrelated")
    blob2 = "\n".join(s["text"] for s in S.build_system_segments(only, S._CODE_PREFACE))
    assert "VR-7 amount must be paise" in blob2


def test_read_doc_tool_outline_fetch_search():
    from app.agents import agentic_tools as T
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID], doc_sections={
        "brd": {"1. Overview": "the system does X", "10. Regulatory": "error RB01 on decline"},
        "tsd": {"5. API": "POST /refund/status returns 200"},
    })
    outline = T.read_doc(ctx)
    assert "1. Overview" in outline and "5. API" in outline
    assert "RB01" in T.read_doc(ctx, heading="Regulatory")          # fuzzy heading fetch
    assert "POST /refund/status" in T.read_doc(ctx, query="refund status")  # keyword search
    empty = T.read_doc(T.RunContext(run_id=RUN, selected_repo_ids=[RID]))
    assert "no BRD" in empty or "work from" in empty                 # no docs → friendly msg


def test_module_notes_is_thin_index():
    import app.agents.context_assembler as CA

    class Row:
        module_path, depth = "network-parent/api-gateway", 2
        summary = "the gateway routes requests " * 30
        entry_points = [{"kind": "RestController", "name": "AdminController"}]
        functional_flow = "VERY LONG FLOW NARRATIVE " * 40

    class Q:
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def all(self): return [Row()]

    class DB:
        def query(self, *a, **k): return Q()

    txt = CA.module_notes(DB(), [RID])[RID]
    assert "network-parent/api-gateway" in txt        # name present (discovery index)
    assert "FLOW NARRATIVE" not in txt            # full flow NOT dumped (pull via tool)
    assert "RestController" not in txt            # entry points NOT dumped
    assert len(txt) < 400                         # thin


# ── runner harness ────────────────────────────────────────────────────────────

@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / RUN / RID
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "A.java").write_text("class A {\n    int x = 1;\n}\n")
    (rd / "schema").mkdir()
    (rd / "schema" / "refund.xsd").write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="Refund"/></xs:schema>')
    for c in (["git", "init", "-q"], ["git", "add", "-A"],
              ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(c, cwd=rd, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=rd, capture_output=True, text=True).stdout.strip()
    return rd, base


def _turn(tool_uses, text="", stop="tool_use"):
    ac = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input} for tu in tool_uses]
    return ClaudeToolTurn(text=text, tool_uses=tool_uses, stop_reason=stop, assistant_content=ac)


def _script(monkeypatch, turns):
    st = {"i": 0}

    async def fake(**kwargs):
        t = turns[st["i"]]; st["i"] += 1; return t
    monkeypatch.setattr(RT, "call_claude_tools", fake)


def test_run_code_change_produces_changeset(ws, monkeypatch):
    _script(monkeypatch, [
        _turn([ToolUseRequest("p", "submit_plan", {"summary": "bump x",
              "reuse_decisions": [{"thing": "Counter", "decision": "extend"}]})]),
        _turn([ToolUseRequest("r", "read_file", {"repo_id": RID, "path": "src/A.java"})]),
        # §8 gate: structural intel before a .java edit — ast_query satisfies it
        _turn([ToolUseRequest("a", "ast_query", {"repo_id": RID, "path": "src/A.java"})]),
        _turn([ToolUseRequest("e", "edit_file", {"repo_id": RID, "path": "src/A.java",
              "old_string": "int x = 1;", "new_string": "int x = 2;"})]),
        _turn([], text="done", stop="end_turn"),
    ])
    ctx = ContextPack(selected_repo_ids=[RID])
    cs = asyncio.run(S.run_code_change(None, run_id=RUN, ctx=ctx, intent="bump x"))
    assert [o.op for o in cs.operations] == ["modify"]
    assert cs.reused == [{"thing": "Counter", "decision": "extend"}]
    assert cs.created == []
    assert "int x = 2;" in (ws[0] / "src" / "A.java").read_text()


def test_run_xsd_discovery_records_deterministic_diff(ws, monkeypatch):
    rd, base = ws
    _script(monkeypatch, [
        _turn([ToolUseRequest("p", "submit_plan", {"summary": "add Receipt",
              "reuse_decisions": [{"element": "Receipt", "decision": "new", "why": "no existing match"}]})]),
        _turn([ToolUseRequest("r", "read_file", {"repo_id": RID, "path": "schema/refund.xsd"})]),
        _turn([ToolUseRequest("e", "edit_file", {"repo_id": RID, "path": "schema/refund.xsd",
              "old_string": '<xs:element name="Refund"/>',
              "new_string": '<xs:element name="Refund"/><xs:element name="Receipt"/>'})]),
        _turn([], text="scope done", stop="end_turn"),
    ])
    ctx = ContextPack(selected_repo_ids=[RID], repo_base_sha={RID: base})
    scope = asyncio.run(S.run_xsd_discovery(None, run_id=RUN, ctx=ctx, intent="add Receipt"))

    assert scope.edits_applied == [f"{RID}:schema/refund.xsd"]
    assert scope.decisions and scope.decisions[0]["decision"] == "new"
    # deterministic record computed from base (git show) vs the edited file
    rec = scope.diff_record[f"{RID}:schema/refund.xsd"]
    assert rec["new"] == ["element:Receipt"] and rec["modified"] == []
    assert scope.determinism_ok is True


def test_xsd_discovery_flags_base_unavailable_without_false_new(ws, monkeypatch):
    # A MODIFY whose base version can't be read (no repo_base_sha) must NOT be
    # reported as all-NEW — it's flagged base_unavailable and determinism_ok=False.
    _script(monkeypatch, [
        _turn([ToolUseRequest("p", "submit_plan", {"summary": "edit"})]),
        _turn([ToolUseRequest("r", "read_file", {"repo_id": RID, "path": "schema/refund.xsd"})]),
        _turn([ToolUseRequest("e", "edit_file", {"repo_id": RID, "path": "schema/refund.xsd",
              "old_string": '<xs:element name="Refund"/>',
              "new_string": '<xs:element name="Refund"/><xs:element name="Receipt"/>'})]),
        _turn([], text="done", stop="end_turn"),
    ])
    ctx = ContextPack(selected_repo_ids=[RID])     # NO repo_base_sha → base can't be read
    scope = asyncio.run(S.run_xsd_discovery(None, run_id=RUN, ctx=ctx, intent="edit"))
    rec = scope.diff_record[f"{RID}:schema/refund.xsd"]
    assert rec.get("base_unavailable") is True and rec["new"] == []
    assert scope.determinism_ok is False


def test_xsd_discovery_strips_propose_revision_once_settled(monkeypatch):
    # Once the human accepted the risk or chose one of the agent's own alternatives, the
    # conversation is OVER — propose_revision must physically leave the toolset so the
    # agent cannot re-litigate (the prod 58ab724c three-round refusal loop).
    captured = {}

    async def fake_loop(**kwargs):
        captured["tools"] = kwargs["tools"]
        return SimpleNamespace(change_set=[], plan={}, final_text="", concerns=[],
                               stopped="completed", proposal=None)

    monkeypatch.setattr(S, "run_agent_loop", fake_loop)
    ctx = ContextPack(selected_repo_ids=[RID])

    asyncio.run(S.run_xsd_discovery(None, run_id=RUN, ctx=ctx, intent="x",
                                    change_request="apply my alternative", via_revision=True))
    assert "propose_revision" not in {t["name"] for t in captured["tools"]}

    asyncio.run(S.run_xsd_discovery(None, run_id=RUN, ctx=ctx, intent="x",
                                    change_request="do it anyway", accepted_risk=True))
    assert "propose_revision" not in {t["name"] for t in captured["tools"]}

    # A fresh human change request still gets its one conversation round.
    asyncio.run(S.run_xsd_discovery(None, run_id=RUN, ctx=ctx, intent="x",
                                    change_request="remove the Amount element"))
    assert "propose_revision" in {t["name"] for t in captured["tools"]}


def test_xsd_discovery_populates_created_files(monkeypatch):
    # `created` feeds the plan-supersession detection (comply-first refine → plan v+1 at
    # approval) and the coverage advisory — and must survive the handoff round-trip.
    from app.agents.agentic_tools import FileOp

    async def fake_loop(**kwargs):
        return SimpleNamespace(
            change_set=[FileOp("add", RID, "schema/ReqVerifyPayee.xsd", "<x/>", None),
                        FileOp("modify", RID, "schema/refund.xsd", "<y/>", None)],
            plan={}, final_text="", concerns=[], stopped="completed", proposal=None)

    monkeypatch.setattr(S, "run_agent_loop", fake_loop)
    scope = asyncio.run(S.run_xsd_discovery(None, run_id=RUN,
                                            ctx=ContextPack(selected_repo_ids=[RID]), intent="x"))
    assert scope.created == [f"{RID}:schema/ReqVerifyPayee.xsd"]
    assert S.xsd_scope_from_dict(S.xsd_scope_to_dict(scope)).created == scope.created


# ── _analysis_resume_messages: replay-transcript continuation (analysis replay) ──

def _asst_gate(tool_use_id, name="ask_clarifications"):
    """A trailing assistant turn ending in a gate tool_use with no tool_result —
    the exact shape run_agent_loop leaves when it breaks at the decision gate."""
    return {"role": "assistant", "content": [
        {"type": "text", "text": "Here is what I found."},
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": {"questions": []}},
    ]}


def _base_transcript(last):
    return [{"role": "user", "content": "Requirement: do X"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t0",
                                               "name": "read_file", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0",
                                          "content": "file body"}]},
            last]


def test_resume_appends_followup_as_gate_tool_result():
    tr = _base_transcript(_asst_gate("gate-1"))
    out = S._analysis_resume_messages(tr, "PM says: cap at 5000")
    assert out is not None
    # original transcript preserved, one user turn appended
    assert len(out) == len(tr) + 1
    added = out[-1]
    assert added["role"] == "user"
    tr_block = added["content"][0]
    assert tr_block["type"] == "tool_result"
    assert tr_block["tool_use_id"] == "gate-1"          # answers routed to the gate call
    assert "cap at 5000" in tr_block["content"]


def test_resume_does_not_mutate_input_transcript():
    tr = _base_transcript(_asst_gate("gate-1"))
    n = len(tr)
    S._analysis_resume_messages(tr, "feedback")
    assert len(tr) == n                                  # caller's list untouched


def test_resume_returns_none_when_last_turn_is_not_assistant():
    # e.g. loop ended on a user turn / completed without a pending gate → fresh fallback
    tr = [{"role": "user", "content": "Requirement"},
          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": "x"}]}]
    assert S._analysis_resume_messages(tr, "f") is None


def test_resume_returns_none_when_no_tool_use_in_last_turn():
    tr = [{"role": "user", "content": "Requirement"},
          {"role": "assistant", "content": [{"type": "text", "text": "just prose, no tool call"}]}]
    assert S._analysis_resume_messages(tr, "f") is None


def test_resume_returns_none_on_empty_transcript():
    assert S._analysis_resume_messages([], "f") is None
    assert S._analysis_resume_messages(None, "f") is None


def test_resume_stubs_sibling_tool_uses_and_routes_followup_to_gate():
    # A final turn with a non-gate tool_use BEFORE the gate: every tool_use needs a
    # tool_result, siblings get a benign stub, the gate gets the follow-up.
    last = {"role": "assistant", "content": [
        {"type": "tool_use", "id": "read-9", "name": "read_file", "input": {}},
        {"type": "tool_use", "id": "gate-9", "name": "propose_plan", "input": {}},
    ]}
    out = S._analysis_resume_messages(_base_transcript(last), "revise it")
    results = out[-1]["content"]
    by_id = {b["tool_use_id"]: b for b in results}
    assert set(by_id) == {"read-9", "gate-9"}            # a result for EVERY tool_use
    assert "revise it" in by_id["gate-9"]["content"]     # follow-up on the gate
    assert "revise it" not in by_id["read-9"]["content"] # sibling gets the stub


def test_resume_routes_followup_to_first_tooluse_when_no_gate_present():
    # Defensive branch: last turn has tool_uses but none is a gate tool.
    last = {"role": "assistant", "content": [
        {"type": "tool_use", "id": "x-1", "name": "grep_code", "input": {}},
    ]}
    out = S._analysis_resume_messages(_base_transcript(last), "answers here")
    assert "answers here" in out[-1]["content"][0]["content"]
