# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Working-memory fact sheet (record_fact + platform auto-facts + pinning + handoff).

In-context knowledge decays over a long agentic loop: the purpose-code incident's agent
read the constants file at iter028 and invented a colliding value at iter066, with the
file still in context. The sheet fixes the mechanics: facts are captured at the moment of
discovery (provenance-enforced), pinned into the brief whenever the sheet changes (not
only at compaction), and persisted across phase handoffs where full payloads get digested.
"""
from types import SimpleNamespace

from app.agents import agentic_tools as T
from app.agents.agentic_tools import (RunContext, ask_clarifications, format_facts,
                                      record_fact, _FACTS_MAX)


# ── provenance enforcement ───────────────────────────────────────────────────────

def test_record_fact_requires_fact_and_source():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    assert "NOT recorded" in record_fact(ctx, "BT is in use", "")
    assert "NOT recorded" in record_fact(ctx, "", "src/CommonConstant.java")
    assert ctx.facts == [] and ctx.facts_rev == 0


def test_record_fact_rejects_unread_file_source():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    out = record_fact(ctx, "'BT' is bound to P2M_DEEMED_RESP_CODE", "src/CommonConstant.java")
    assert "NOT recorded" in out and "not a file you read" in out
    assert ctx.facts == []


def test_record_fact_accepts_read_file_and_declared_kinds():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    ctx.read_files = {("core", "src/CommonConstant.java")}
    out = record_fact(ctx, "'BT' is bound to P2M_DEEMED_RESP_CODE (in use)",
                      "src/CommonConstant.java")
    assert "F1 recorded" in out
    record_fact(ctx, "PM decided: purpose code assigned by the Authority Scheme", "human_decision")
    record_fact(ctx, "banks likely prefer dedicated reporting codes", "assumption")
    kinds = [f["kind"] for f in ctx.facts]
    assert kinds == ["verified", "human_decision", "assumption"]
    assert ctx.facts_rev == 3
    # the tool echoes the whole sheet so recency rides every write
    assert "FACT SHEET" in out and "P2M_DEEMED_RESP_CODE" in out


def test_record_fact_dedupes_caps_and_supersedes():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    first = record_fact(ctx, "PM decided: X", "human_decision")
    assert "F1 recorded" in first
    assert "not duplicated" in record_fact(ctx, "PM decided: X", "human_decision")
    assert len(ctx.facts) == 1
    for n in range(_FACTS_MAX - 1):
        record_fact(ctx, f"fact {n}", "human_decision")
    assert len(ctx.facts) == _FACTS_MAX
    assert "sheet is full" in record_fact(ctx, "one more", "human_decision")
    out = record_fact(ctx, "PM decided: X, then reversed to Y", "human_decision",
                      supersedes="F1")
    assert "F1 superseded" in out
    assert ctx.facts[0]["fact"] == "PM decided: X, then reversed to Y"
    assert "NOT recorded" in record_fact(ctx, "z", "human_decision", supersedes="F99")


def test_format_facts_renders_glyphs_and_sources():
    txt = format_facts([
        {"id": "F1", "fact": "'BT' bound to P2M_DEEMED_RESP_CODE",
         "source": "src/CommonConstant.java", "kind": "verified"},
        {"id": "F2", "fact": "PM decided X", "source": "human_decision",
         "kind": "human_decision"},
    ])
    assert "F1 ✓" in txt and "[src/CommonConstant.java]" in txt
    assert "F2 ⚖" in txt
    assert format_facts([]) == ""


# ── platform auto-facts: the occupancy verdict pins itself ───────────────────────

def _patch_repo(monkeypatch, stdout="", exit_code=0):
    monkeypatch.setattr(T, "_repo_root", lambda ctx, rid: f"/clones/{rid}")
    monkeypatch.setattr(T, "adapter", SimpleNamespace(
        run_command=lambda root, argv: SimpleNamespace(
            stdout=stdout, stderr="", exit_code=exit_code, timed_out=False)))


def test_occupancy_check_writes_an_auto_fact(monkeypatch):
    _patch_repo(monkeypatch, stdout='src/CommonConstant.java:56:    X = "BT";')
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    ctx.read_files = {("core", "src/CommonConstant.java")}
    ask_clarifications(ctx, [{
        "id": "q", "text": "which value?", "recommended": "opt",
        "options": [{"id": "opt", "label": "BT", "proposed_value": "BT"}],
        "evidence": [{"claim": "constants", "file": "src/CommonConstant.java"}],
    }])
    assert any("'BT' already appears at 1" in f["fact"] for f in ctx.facts)
    assert ctx.facts[0]["kind"] == "verified"
    assert ctx.facts[0]["source"] == "platform:occupancy-check"


# ── pinning: the sheet lands in the brief's ground-truth block ───────────────────

def test_pin_ground_truth_includes_the_fact_sheet():
    from app.agents.agentic_runtime import _pin_ground_truth, _STATE_MARKER
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    ctx.facts = [{"id": "F1", "fact": "'BT' is in use", "source": "c.java",
                  "kind": "verified"}]
    messages = [{"role": "user", "content": "the brief"}]
    _pin_ground_truth(messages, ctx)
    assert _STATE_MARKER in messages[0]["content"]
    assert "'BT' is in use" in messages[0]["content"]
    # re-pin replaces, never accumulates
    ctx.facts.append({"id": "F2", "fact": "second", "source": "human_decision",
                      "kind": "human_decision"})
    _pin_ground_truth(messages, ctx)
    assert messages[0]["content"].count(_STATE_MARKER) == 1
    assert "second" in messages[0]["content"]


def test_runtime_result_carries_facts():
    from app.agents.agentic_runtime import RuntimeResult
    r = RuntimeResult("t", [], None, 1, "completed")
    assert r.facts == []


# ── handoff: merged across drives, rendered into the XSD phase ───────────────────

def test_merge_facts_dedupes_and_renumbers():
    from app.agents.agentic_orchestrator import _merge_facts
    old = [{"id": "F1", "fact": "a", "source": "s", "kind": "verified"}]
    new = [{"id": "F1", "fact": "a", "source": "s", "kind": "verified"},
           {"id": "F2", "fact": "b", "source": "human_decision", "kind": "human_decision"}]
    merged = _merge_facts(old, new)
    assert [f["fact"] for f in merged] == ["a", "b"]
    assert [f["id"] for f in merged] == ["F1", "F2"]
    assert _merge_facts(None, None) == []


def test_facts_block_renders_from_handoff():
    from app.agents.agentic_orchestrator import _facts_block
    h = {"facts": [{"id": "F1", "fact": "'BT' is in use", "source": "c.java",
                    "kind": "verified"}]}
    assert "FACT SHEET" in _facts_block(h) and "'BT' is in use" in _facts_block(h)
    assert _facts_block({}) == "" and _facts_block(None) == ""


# ── loop-level: every sheet change is pinned into the brief AND logged as an event ──

import asyncio

from app.agents import agentic_runtime as RT
from app.core.llm import ToolUseRequest, ClaudeToolTurn


def _turn(tool_uses, text="", stop="tool_use"):
    ac = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input} for tu in tool_uses]
    return ClaudeToolTurn(text=text, tool_uses=tool_uses, stop_reason=stop, assistant_content=ac)


def _script(monkeypatch, turns):
    st = {"i": 0}

    async def fake(**kwargs):
        t = turns[st["i"]]; st["i"] += 1; return t
    monkeypatch.setattr(RT, "call_claude_tools", fake)


def _capture_events(monkeypatch):
    ev = []
    monkeypatch.setattr(RT, "emit_event",
                        lambda db, rid, kind, payload=None: ev.append((kind, payload)))
    return ev


def _loop(**kw):
    return asyncio.run(RT.run_agent_loop(
        run_id="r", selected_repo_ids=["core"], system="s", user_prompt="go",
        tools=[], require_plan=False, db=SimpleNamespace(), **kw))


def test_loop_logs_and_pins_each_fact_write(monkeypatch):
    events = _capture_events(monkeypatch)
    _script(monkeypatch, [
        _turn([ToolUseRequest("f", "record_fact",
                              {"fact": "PM decided X", "source": "human_decision"})]),
        _turn([], text="done", stop="end_turn"),
    ])
    res = _loop()
    assert [f["fact"] for f in res.facts] == ["PM decided X"]
    # pinned into the brief → this is the copy every transcript dump carries
    assert RT._STATE_MARKER in res.transcript[0]["content"]
    assert "PM decided X" in res.transcript[0]["content"]
    # logged as a run event → activity feed / events API / devlog
    sheets = [p for k, p in events if k == "fact_sheet"]
    assert sheets and sheets[0]["count"] == 1 and "PM decided X" in sheets[0]["sheet"]


def test_loop_gate_stop_still_captures_gate_turn_facts(monkeypatch):
    # ask_clarifications/ask_decision END the pass BEFORE the tool results are appended —
    # facts written by that same turn (e.g. occupancy auto-facts) must still be pinned
    # into the persisted transcript and logged, or the gate turn's truth is invisible.
    events = _capture_events(monkeypatch)
    _script(monkeypatch, [
        _turn([ToolUseRequest("f", "record_fact",
                              {"fact": "'BT' already bound", "source": "human_decision"}),
               ToolUseRequest("g", "ask_decision",
                              {"question": "which value?", "blocked_item": "purpose code"})]),
    ])
    res = _loop()
    assert res.stopped == "awaiting_decision"
    assert RT._STATE_MARKER in res.transcript[0]["content"]
    assert "'BT' already bound" in res.transcript[0]["content"]
    assert any(k == "fact_sheet" for k, _ in events)


def test_loop_seeds_prior_drive_facts(monkeypatch):
    events = _capture_events(monkeypatch)
    _script(monkeypatch, [_turn([], text="done", stop="end_turn")])
    seeded = [{"id": "F1", "fact": "'BT' is in use", "source": "c.java", "kind": "verified"}]
    res = _loop(initial_facts=seeded)
    # visible from turn 1 and returned intact for the next handoff merge
    assert RT._STATE_MARKER in res.transcript[0]["content"]
    assert "'BT' is in use" in res.transcript[0]["content"]
    assert [f["fact"] for f in res.facts] == ["'BT' is in use"]
    # a seed is the baseline, not an update — no fact_sheet event until something changes
    assert not any(k == "fact_sheet" for k, _ in events)


def test_run_analysis_seeds_handoff_facts(monkeypatch):
    from app.agents import agentic_subagents as S
    from app.agents.context_assembler import ContextPack
    _script(monkeypatch, [_turn([], text="no questions", stop="end_turn")])
    proposal, transcript, facts = asyncio.run(S.run_analysis(
        None, run_id="r", ctx=ContextPack(selected_repo_ids=["x"]), intent="i",
        facts=[{"id": "F1", "fact": "seeded from drive 1", "source": "human_decision",
                "kind": "human_decision"}]))
    assert [f["fact"] for f in facts] == ["seeded from drive 1"]
    assert "seeded from drive 1" in transcript[0]["content"]
