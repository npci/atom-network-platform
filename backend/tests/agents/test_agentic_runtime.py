# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The bounded agentic loop (§8) with the LLM scripted via a fake call_claude_tools."""
import asyncio
import subprocess

import pytest

from app.core.config import settings
from app.core.llm import ToolUseRequest, ClaudeToolTurn
from app.agents import agentic_runtime as R

RID = "repo-1"
RUN = "run-1"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / RUN / RID
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "A.java").write_text("class A {\n    int x = 1;\n}\n")
    subprocess.run(["git", "init", "-q"], cwd=rd, check=True)
    subprocess.run(["git", "add", "-A"], cwd=rd, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=rd, check=True)
    return rd


def _turn(tool_uses, text="", stop="tool_use"):
    ac = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input} for tu in tool_uses
    ]
    return ClaudeToolTurn(text=text, tool_uses=tool_uses, stop_reason=stop, assistant_content=ac)


def _script_runner(monkeypatch, script):
    state = {"i": 0}

    async def fake(**kwargs):
        t = script[state["i"]]
        state["i"] += 1
        return t

    monkeypatch.setattr(R, "call_claude_tools", fake)


def test_full_loop_plan_read_edit_finish(ws, monkeypatch):
    _script_runner(monkeypatch, [
        _turn([ToolUseRequest("t1", "submit_plan", {"summary": "bump x"})]),
        _turn([ToolUseRequest("t2", "read_file", {"repo_id": RID, "path": "src/A.java"})]),
        # §8 gate: a .java edit needs structural intel first — ast_query satisfies it
        _turn([ToolUseRequest("t2b", "ast_query", {"repo_id": RID, "path": "src/A.java"})]),
        _turn([ToolUseRequest("t3", "edit_file", {"repo_id": RID, "path": "src/A.java",
                                                  "old_string": "int x = 1;", "new_string": "int x = 2;"})]),
        _turn([], text="done", stop="end_turn"),
    ])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=None))

    assert res.stopped == "completed" and res.iterations == 5
    assert res.plan["summary"] == "bump x"
    assert len(res.change_set) == 1 and res.change_set[0].op == "modify"
    assert "int x = 2;" in (ws / "src" / "A.java").read_text()


def test_mutation_blocked_before_plan(ws, monkeypatch):
    # read is fine, but edit before submit_plan is refused (turn-1 enforcement).
    _script_runner(monkeypatch, [
        _turn([ToolUseRequest("t1", "read_file", {"repo_id": RID, "path": "src/A.java"})]),
        _turn([ToolUseRequest("t2", "edit_file", {"repo_id": RID, "path": "src/A.java",
                                                  "old_string": "int x = 1;", "new_string": "int x = 9;"})]),
        _turn([], text="stopped", stop="end_turn"),
    ])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=None))
    assert res.change_set == []                              # edit refused
    assert "int x = 1;" in (ws / "src" / "A.java").read_text()  # file untouched


def test_iteration_cap_stops_the_loop(ws, monkeypatch):
    # Always asks for a tool → must stop at the cap, not spin forever.
    always = _turn([ToolUseRequest("t", "grep", {"repo_id": RID, "pattern": "x"})])

    async def fake(**kwargs):
        return always
    monkeypatch.setattr(R, "call_claude_tools", fake)

    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_iterations=3))
    assert res.stopped == "max_iterations" and res.iterations == 3


def test_events_emitted_for_turns_and_tools(ws, monkeypatch):
    events = []
    monkeypatch.setattr(R, "emit_event",
                        lambda db, run_id, kind, payload: events.append((kind, payload)))
    _script_runner(monkeypatch, [
        _turn([ToolUseRequest("t1", "submit_plan", {"summary": "x"})]),
        _turn([], text="done", stop="end_turn"),
    ])
    asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=object()))
    kinds = [k for k, _ in events]
    assert kinds.count("llm_turn") == 2
    assert ("tool_call" in kinds) and ("loop_done" in kinds)


def test_empty_assistant_turn_ends_loop_without_crashing(ws, monkeypatch):
    # stop_reason='tool_use' but NO blocks → would make the next request's
    # assistant content empty (Anthropic 400). The loop must end cleanly instead.
    bad = ClaudeToolTurn(text="", tool_uses=[], stop_reason="tool_use", assistant_content=[])

    async def fake(**kwargs):
        return bad
    monkeypatch.setattr(R, "call_claude_tools", fake)

    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_iterations=5))
    assert res.stopped == "completed" and res.iterations == 1


def test_final_text_carried_on_cap(ws, monkeypatch):
    t = _turn([ToolUseRequest("t", "grep", {"repo_id": RID, "pattern": "x"})], text="still working")

    async def fake(**kwargs):
        return t
    monkeypatch.setattr(R, "call_claude_tools", fake)

    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_iterations=2))
    assert res.stopped == "max_iterations" and res.final_text == "still working"


def test_cancel_check_stops_before_first_turn(ws, monkeypatch):
    _script_runner(monkeypatch, [_turn([], text="never", stop="end_turn")])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, cancel_check=lambda: True))
    assert res.stopped == "cancelled" and res.iterations == 0
