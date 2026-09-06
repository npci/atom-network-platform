# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Turn-level recovery (§8, grok-build parity): a max_tokens-truncated turn is discarded and
retried with a doubled budget; an empty turn gets one retry; a replayed transcript ending in
unanswered tool_use blocks is repaired; compaction re-injects deterministic run state."""
import asyncio
import subprocess

import pytest

from app.core.config import settings
from app.core.llm import ClaudeToolTurn, ToolUseRequest
from app.agents import agentic_runtime as R
from app.agents import agentic_tools as T

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


def _turn(text="", stop="end_turn", tool_uses=()):
    ac = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input} for tu in tool_uses
    ]
    return ClaudeToolTurn(text=text, tool_uses=list(tool_uses), stop_reason=stop,
                          assistant_content=ac)


def _capture_runner(monkeypatch, script):
    calls = []

    async def fake(**kwargs):
        # snapshot the messages list — the loop appends to the same object afterwards
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return script[min(len(calls) - 1, len(script) - 1)]

    monkeypatch.setattr(R, "call_claude_tools", fake)
    return calls


def test_truncated_turn_discarded_and_retried_with_double_budget(ws, monkeypatch):
    calls = _capture_runner(monkeypatch, [
        _turn(text="partial thought that got cut", stop="max_tokens"),
        _turn(text="done", stop="end_turn"),
    ])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_tokens=1000))
    assert res.stopped == "completed" and res.iterations == 1
    assert res.final_text == "done"                     # truncated turn was discarded
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 2000               # retry ran at double the budget


def test_empty_turn_retried_once_then_completes(ws, monkeypatch):
    calls = _capture_runner(monkeypatch, [
        ClaudeToolTurn(text="", tool_uses=[], stop_reason="tool_use", assistant_content=[]),
        _turn(text="done", stop="end_turn"),
    ])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=None))
    assert res.stopped == "completed" and res.final_text == "done"
    assert len(calls) == 2


def test_persistently_empty_turn_still_ends_cleanly(ws, monkeypatch):
    empty = ClaudeToolTurn(text="", tool_uses=[], stop_reason="tool_use", assistant_content=[])
    calls = _capture_runner(monkeypatch, [empty])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_iterations=5))
    assert res.stopped == "completed" and res.iterations == 1
    assert len(calls) == 2                              # exactly one retry, no retry storm


def test_recovery_flag_off_preserves_legacy_single_call(ws, monkeypatch):
    monkeypatch.setattr(settings, "agentic_turn_recovery", False)
    empty = ClaudeToolTurn(text="", tool_uses=[], stop_reason="tool_use", assistant_content=[])
    calls = _capture_runner(monkeypatch, [empty])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=None))
    assert res.stopped == "completed" and len(calls) == 1


def test_replayed_transcript_with_dangling_tool_use_is_repaired(ws, monkeypatch):
    calls = _capture_runner(monkeypatch, [_turn(text="done", stop="end_turn")])
    dangling = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu-lost", "name": "grep",
             "input": {"repo_id": RID, "pattern": "x"}}]},
    ]
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, initial_messages=dangling))
    assert res.stopped == "completed"
    sent = calls[0]["messages"]
    # the repair appended a user turn answering the dangling tool_use — wire-valid
    assert sent[-1]["role"] == "user"
    stub = sent[-1]["content"][0]
    assert stub["type"] == "tool_result" and stub["tool_use_id"] == "tu-lost"
    assert stub.get("is_error") is True


# ── compaction fidelity helpers (pure) ────────────────────────────────────────

def test_pin_ground_truth_lists_edits_and_replaces_prior_block():
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID])
    ctx.file_ops[(RID, "src/A.java")] = T.FileOp("modify", RID, "src/A.java", "x", "h")
    ctx.plan = {"summary": "bump x", "files": [{"path": "src/A.java"}]}
    messages = [{"role": "user", "content": "the brief"}]
    R._pin_ground_truth(messages, ctx)
    content = messages[0]["content"]
    assert R._STATE_MARKER in content
    assert "modify" in content and "src/A.java" in content and "bump x" in content
    # a second compaction replaces, never stacks
    ctx.file_ops[(RID, "src/B.java")] = T.FileOp("add", RID, "src/B.java", "y", "h2")
    R._pin_ground_truth(messages, ctx)
    content = messages[0]["content"]
    assert content.count(R._STATE_MARKER) == 1 and "src/B.java" in content
    assert content.startswith("the brief")


def test_clean_summary_strips_fences_and_neutralizes_marker_echo():
    fenced = "```markdown\n1. GOAL — ship it\n```"
    assert R._clean_summary(fenced) == "1. GOAL — ship it"
    echoed = f"progress\n{R._SUMMARY_MARKER}\nmore"
    cleaned = R._clean_summary(echoed)
    assert R._SUMMARY_MARKER not in cleaned      # can't truncate future refreshes
    assert "progress" in cleaned and "more" in cleaned


# ── P1: anchored estimation, preflight compaction, convergence nudge ──────────

def test_estimated_input_tokens_anchors_on_usage():
    msgs = [{"role": "user", "content": "x" * 4000}]
    # no anchor yet → whole history at ~4 chars/token
    assert R._estimated_input_tokens(msgs, 0, 0) == 1000
    # anchored: reported usage + only the chars appended since
    chars_at_anchor = R._total_msg_chars(msgs)
    msgs.append({"role": "user", "content": "y" * 8000})
    assert R._estimated_input_tokens(msgs, 500_000, chars_at_anchor) == 500_000 + 2000


def test_preflight_compaction_fires_before_next_call(ws, monkeypatch):
    monkeypatch.setattr(settings, "agentic_context_window_tokens", 1000)
    monkeypatch.setattr(settings, "agentic_compact_at_fraction", 0.8)
    # keep_tail=0 so the one big tool result is evictable, and a BIG grep result so there is
    # real reclaimable bulk over the evict floor — the thrash guard correctly refuses to
    # "compact" a conversation with nothing to evict (that path emits context_at_floor).
    monkeypatch.setattr(settings, "agentic_compact_keep_recent_turns", 0)
    async def _no_summary(*a, **k): return False      # summary is always-on now — skip its LLM call in-test
    monkeypatch.setattr(R, "_pin_progress_summary", _no_summary)
    (ws / "src" / "Big.java").write_text("\n".join(f"int x{i} = {i};" for i in range(400)))
    events = []
    monkeypatch.setattr(R, "emit_event", lambda db, run_id, kind, p: events.append((kind, p)))
    # turn 1 reports huge usage (anchor 5000 > 800 policy) and asks for a tool whose result is
    # large; the preflight before call 2 must compact rather than let the call overflow.
    t1 = _turn(stop="tool_use", tool_uses=[ToolUseRequest("t1", "grep", {"repo_id": RID, "pattern": "x"})])
    t1.usage = {"input_tokens": 5000, "output_tokens": 10}
    calls = _capture_runner(monkeypatch, [t1, _turn(text="done", stop="end_turn")])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=object(), max_iterations=5))
    assert res.stopped == "completed"
    triggers = [p.get("trigger") for k, p in events if k == "history_compacted"]
    assert "preflight" in triggers


def test_convergence_nudge_drives_completion_then_accepts_stop(ws, monkeypatch):
    checks = ["- MISSING: producer for retryFlag", None]     # unmet once, then satisfied
    calls = _capture_runner(monkeypatch, [
        _turn(text="done", stop="end_turn"),
        _turn(text="really done", stop="end_turn"),
    ])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, completion_check=lambda: checks.pop(0)))
    assert res.stopped == "completed" and res.final_text == "really done"
    assert len(calls) == 2
    nudge = calls[1]["messages"][-1]
    assert nudge["role"] == "user" and "UNMET" in nudge["content"]
    assert "retryFlag" in nudge["content"]


def test_convergence_nudges_are_bounded(ws, monkeypatch):
    monkeypatch.setattr(settings, "agentic_convergence_nudges", 2)
    calls = _capture_runner(monkeypatch, [_turn(text="done", stop="end_turn")])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, completion_check=lambda: "- still missing"))     # never satisfied
    assert res.stopped == "completed"                # accepted after the budget, no spin
    assert len(calls) == 3                          # initial + 2 nudged rounds


# ── P2: doom-loop-lite + harness-truth exploration nudge ──────────────────────

def test_looks_degenerate_detects_tail_repetition():
    assert R._looks_degenerate("I will fix it. " * 40)          # stuck loop
    assert not R._looks_degenerate("normal prose about the fix, nothing repeated")
    assert not R._looks_degenerate("done. " * 10)               # unit too short / span too small
    assert not R._looks_degenerate("")


def test_degenerate_turn_discarded_and_resampled(ws, monkeypatch):
    calls = _capture_runner(monkeypatch, [
        _turn(text="applying the fix now. " * 30, stop="end_turn"),   # degenerate tail
        _turn(text="ok done", stop="end_turn"),
    ])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=None))
    assert res.stopped == "completed" and res.final_text == "ok done"
    assert len(calls) == 2                          # one resample, then accepted


def test_exploration_nudge_fires_in_code_phase_only(ws, monkeypatch):
    monkeypatch.setattr(settings, "agentic_exploration_nudge_every", 2)
    grepping = _turn(stop="tool_use",
                     tool_uses=[ToolUseRequest("t", "grep", {"repo_id": RID, "pattern": "x"})])

    def _has_nudge(transcript):
        return any(isinstance(b, dict) and b.get("type") == "text"
                   and "harness record" in (b.get("text") or "")
                   for m in transcript if isinstance(m.get("content"), list)
                   for b in m["content"])

    _capture_runner(monkeypatch, [grepping])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_iterations=4, code_phase=True, require_plan=False))
    assert _has_nudge(res.transcript)               # 4 read-only iters, nudged at every=2

    _capture_runner(monkeypatch, [grepping])
    res2 = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_iterations=4, require_plan=False))       # analysis-style: read-only by design
    assert not _has_nudge(res2.transcript)


def test_summary_ladder_retries_on_overflow(monkeypatch):
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("prompt is too long: 900000 tokens > 800000 maximum")
        return ClaudeToolTurn(text="1. GOAL — finish", tool_uses=[], stop_reason="end_turn",
                              assistant_content=[{"type": "text", "text": "s"}])
    monkeypatch.setattr(R, "call_claude_tools", fake)

    messages = [{"role": "user", "content": "brief"},
                {"role": "assistant", "content": [{"type": "text", "text": "thinking"}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                              "content": "z" * 5000}]}]
    ok = asyncio.run(R._pin_progress_summary(messages, "sys", None, "code_change", None))
    assert ok and calls["n"] == 2                   # overflow → Lossy step succeeded
    assert "1. GOAL — finish" in messages[0]["content"]
