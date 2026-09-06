# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Anthropic review subagent (§10). Parsing is pure; the runner uses a scripted
LLM (read-only) against a real local git workspace."""
import asyncio
import subprocess

import pytest

from app.core.config import settings
from app.core.llm import ClaudeToolTurn, ToolUseRequest
from app.agents.context_assembler import ContextPack
from app.agents import agentic_runtime as RT
from app.agents import agentic_review as R
from app.agents.agentic_review import parse_findings

RID = "repo-1"
RUN = "run-1"


# ── parse_findings (pure) ─────────────────────────────────────────────────────

def test_parse_fenced_json_and_blocker_is_blocking():
    text = ('Here is my review.\n```json\n'
            '[{"severity":"blocker","category":"security","file":"A.java","line":3,'
            '"why":"hardcoded token","suggested_fix":"use config","blocking":true}]\n```')
    fs = parse_findings(text)
    assert len(fs) == 1 and fs[0].blocking is True
    assert fs[0].category == "security" and fs[0].file == "A.java" and fs[0].line == 3


def test_parse_bare_array_and_severity_blocker_implies_blocking():
    fs = parse_findings('[{"severity":"blocker","category":"correctness","why":"npe"}]')
    assert fs[0].blocking is True   # severity=blocker → blocking even if field omitted


def test_parse_empty_array_is_no_findings():
    assert parse_findings("[]") == []


def test_parse_unknown_severity_category_defaults():
    fs = parse_findings('[{"severity":"weird","category":"bogus","why":"x"}]')
    assert fs[0].severity == "warning" and fs[0].category == "correctness"


def test_parse_garbage_is_inconclusive_not_silently_clean():
    fs = parse_findings("the change looks fine to me, no findings here")
    assert len(fs) == 1 and fs[0].blocking is False and "JSON" in fs[0].why


def test_parse_prose_then_trailing_fenced_json():
    """The b060dc2a run's shape: pages of reasoning prose — containing literal [D1]
    bracket references that defeat the greedy array regex — with the verdict appended
    as a final ```json fence. Must parse the verdict, not return the unparseable marker."""
    prose = ("Let me check the diff first.\n[D1] looks satisfied per FRMServiceImpl.java:88.\n"
             "Ready to produce findings.\n\n")
    verdict = ('[{"severity": "info", "category": "directive", '
               '"why": "[D1] PASS — FRMServiceImpl.java:88", "blocking": false}]')
    fs = parse_findings(prose + "```json\n" + verdict + "\n```\nDone.")
    assert [f.why for f in fs] == ["[D1] PASS — FRMServiceImpl.java:88"]
    assert not any(f.why == R.UNPARSEABLE_WHY for f in fs)


def test_parse_prose_last_fenced_array_wins_over_earlier_fences():
    early = '```json\n{"not": "the verdict"}\n```'          # object → skipped (array expected)
    late = ('```json\n[{"severity":"blocker","category":"correctness",'
            '"why":"real finding","blocking":true}]\n```')
    fs = parse_findings("intro\n" + early + "\nmore prose\n" + late + "\ntrailing note")
    assert len(fs) == 1 and fs[0].why == "real finding" and fs[0].blocking


def test_reextract_findings_salvages_verdict(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return ('[{"severity":"info","category":"directive",'
                '"why":"[D1] PASS — evidence","blocking":false}]')
    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
    out = asyncio.run(R._reextract_findings("prose with no json anywhere"))
    assert out and out[0].why == "[D1] PASS — evidence"


def test_reextract_findings_empty_or_garbage_keeps_marker(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return "[]"                                         # extractor found nothing
    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
    assert asyncio.run(R._reextract_findings("prose")) is None   # caller keeps the marker


def test_parse_malformed_json_never_blocks_or_crashes():
    fs = parse_findings("```json\n[{bad json,,}]\n```")
    assert not any(f.blocking for f in fs)


def test_parse_brackets_in_string_value_are_not_lost():
    # A real blocking finding whose `why` contains [ ] must survive (the old
    # hand-rolled bracket counter corrupted on this and downgraded it to info).
    fs = parse_findings('[{"severity":"blocker","category":"correctness",'
                        '"why":"arr[0] is null and list[i] is unchecked","blocking":true}]')
    assert len(fs) == 1 and fs[0].blocking is True and "arr[0]" in fs[0].why


def test_parse_float_line_number_kept():
    fs = parse_findings('[{"severity":"warning","category":"correctness","why":"x","line":12.0}]')
    assert fs[0].line == 12


# ── run_review (scripted) ─────────────────────────────────────────────────────

@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / RUN / RID
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "A.java").write_text("class A {\n    String token = \"abc\";\n}\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"],
              ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(c, cwd=rd, check=True)
    # an uncommitted edit so `git diff` has something to render
    (rd / "src" / "A.java").write_text("class A {\n    String token = \"hardcoded\";\n}\n")
    return rd


def _op(op, path, content=None):
    from types import SimpleNamespace
    return SimpleNamespace(op=op, repo_id=RID, path=path, content=content)


def test_run_review_parses_findings_and_persists_nothing_when_db_none(ws, monkeypatch):
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return ClaudeToolTurn(
            text='```json\n[{"severity":"blocker","category":"security","file":"src/A.java",'
                 '"line":2,"why":"hardcoded token","blocking":true}]\n```',
            tool_uses=[], stop_reason="end_turn", assistant_content=[{"type": "text", "text": "x"}])
    monkeypatch.setattr(RT, "call_claude_tools", fake)

    ctx = ContextPack(selected_repo_ids=[RID])
    rf = asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                                  change_set=type("CS", (), {"operations": [_op("modify", "src/A.java")]})(),
                                  intent="store token", reviewer_model="claude-test"))
    assert rf.blocking is True and len(rf.findings) == 1
    assert rf.reviewer_model == "claude-test"
    # review is read-only — no mutating tools were offered to the model
    tool_names = {t["name"] for t in captured["tools"]}
    assert not (tool_names & {"edit_file", "create_file", "delete_file", "submit_plan"})
    # the rendered diff (token change) reached the model
    assert "hardcoded" in captured["messages"][0]["content"]


def test_extra_tools_reach_the_model_alongside_the_read_only_set(ws, monkeypatch):
    """Governance BUNDLE stages hand in run_skill_script + the sandboxed bash so SKILL.md's
    own procedure runs verbatim. run_review must ACCEPT them (a signature mismatch here
    killed every bundle stage with a TypeError) and offer them on top of REVIEW_TOOLS."""
    from app.agents.agentic_tools import GOV_BASH_SCHEMA, RUN_SKILL_SCRIPT_SCHEMA
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return ClaudeToolTurn(text="[]", tool_uses=[], stop_reason="end_turn",
                              assistant_content=[{"type": "text", "text": "[]"}])
    monkeypatch.setattr(RT, "call_claude_tools", fake)

    ctx = ContextPack(selected_repo_ids=[RID])
    asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                             change_set=type("CS", (), {"operations": []})(),
                             reviewer_model="claude-test",
                             extra_tools=[RUN_SKILL_SCRIPT_SCHEMA, GOV_BASH_SCHEMA]))
    tool_names = [t["name"] for t in captured["tools"]]
    assert {"run_skill_script", "bash"} <= set(tool_names)
    assert {"read_file", "grep"} <= set(tool_names)          # the read-only set is still there
    assert len(tool_names) == len(set(tool_names))           # no duplicate schemas


def test_extra_tools_may_not_smuggle_in_an_editing_tool(ws):
    """The reviewer's read-only invariant is what makes its verdict trustworthy — it must
    hold no matter what a caller passes."""
    ctx = ContextPack(selected_repo_ids=[RID])
    with pytest.raises(ValueError, match="edit_file"):
        asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                                 change_set=type("CS", (), {"operations": []})(),
                                 reviewer_model="claude-test",
                                 extra_tools=[{"name": "edit_file"}]))


def test_gpt_reviewer_model_is_accepted(ws, monkeypatch):
    # Setting a gpt id IS the opt-in — no separate enable flag (AiNxt-recognized prefix).
    # Requires the route it actually runs on: ainxt + anthropic-compat.
    monkeypatch.setattr(settings, "llm_provider", "ainxt")
    monkeypatch.setattr(settings, "ainxt_compat_mode", "anthropic")

    async def fake(**kwargs):
        return ClaudeToolTurn(text="[]", tool_uses=[], stop_reason="end_turn",
                              assistant_content=[{"type": "text", "text": "[]"}])
    monkeypatch.setattr(RT, "call_claude_tools", fake)

    ctx = ContextPack(selected_repo_ids=[RID])
    rf = asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                                  change_set=type("CS", (), {"operations": []})(),
                                  reviewer_model="gpt-5.4"))
    assert rf.reviewer_model == "gpt-5.4" and rf.blocking is False


def test_gpt_reviewer_rejected_when_provider_cannot_route_it(ws, monkeypatch):
    # A gpt id under llm_provider=claude would pass the prefix guard and then hard-fail
    # at api.anthropic.com (non-transient NotFoundError) — must fail loudly at the guard.
    monkeypatch.setattr(settings, "llm_provider", "claude")
    ctx = ContextPack(selected_repo_ids=[RID])
    with pytest.raises(ValueError, match="ainxt_compat_mode"):
        asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                                 change_set=type("CS", (), {"operations": []})(),
                                 reviewer_model="gpt-5.4"))


def test_unrecognized_reviewer_model_rejected(ws):
    # AiNxt B5: an id matching neither claude nor gpt/o1/o3/o4 silently routes to Claude
    # on the gateway — must fail loudly here instead.
    ctx = ContextPack(selected_repo_ids=[RID])
    with pytest.raises(ValueError, match="gpt/o1/o3/o4"):
        asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                                 change_set=type("CS", (), {"operations": []})(),
                                 reviewer_model="azure-gpt4-internal"))


def test_dedicated_reviewer_model_setting_used(ws, monkeypatch):
    monkeypatch.setattr(settings, "agentic_reviewer_model", "claude-reviewer-test")

    async def fake(**kwargs):
        return ClaudeToolTurn(text="[]", tool_uses=[], stop_reason="end_turn",
                              assistant_content=[{"type": "text", "text": "[]"}])
    monkeypatch.setattr(RT, "call_claude_tools", fake)

    ctx = ContextPack(selected_repo_ids=[RID])
    rf = asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                                  change_set=type("CS", (), {"operations": []})()))
    assert rf.reviewer_model == "claude-reviewer-test"


def test_resume_read_set_renders_note_and_threads_progress(ws, monkeypatch):
    # RESUME WITH MEMORY: an interrupted-and-re-entered round hands the reviewer the files it
    # already explored (so it skips re-discovery) and threads the read-set checkpoint into the loop.
    captured = {}
    beats = []

    async def fake(**kwargs):
        captured.update(kwargs)
        return ClaudeToolTurn(text="[]", tool_uses=[], stop_reason="end_turn",
                              assistant_content=[{"type": "text", "text": "[]"}])
    monkeypatch.setattr(RT, "call_claude_tools", fake)

    ctx = ContextPack(selected_repo_ids=[RID])
    asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                             change_set=type("CS", (), {"operations": [_op("modify", "src/A.java")]})(),
                             intent="store token", reviewer_model="claude-test",
                             resume_read_files=[[RID, "src/A.java"], [RID, "src/B.java"]],
                             progress=lambda rs: beats.append(rs)))
    prompt = captured["messages"][0]["content"]
    assert "RESUMED REVIEW" in prompt                       # the reviewer is told it is a resume
    assert "src/A.java" in prompt and "src/B.java" in prompt # the already-explored files are listed
    assert beats                                            # the progress checkpoint was threaded into the loop


def test_no_resume_note_on_a_cold_review(ws, monkeypatch):
    captured = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return ClaudeToolTurn(text="[]", tool_uses=[], stop_reason="end_turn",
                              assistant_content=[{"type": "text", "text": "[]"}])
    monkeypatch.setattr(RT, "call_claude_tools", fake)

    ctx = ContextPack(selected_repo_ids=[RID])
    asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                             change_set=type("CS", (), {"operations": [_op("modify", "src/A.java")]})(),
                             reviewer_model="claude-test"))
    assert "RESUMED REVIEW" not in captured["messages"][0]["content"]


def test_phantom_path_finding_is_annotated_never_demoted(ws, monkeypatch):
    async def fake(**kwargs):
        return ClaudeToolTurn(
            text=('[{"severity":"blocker","category":"correctness","file":"src/Ghost.java",'
                  '"why":"missing mapper","blocking":true},'
                  '{"severity":"warning","category":"convention","file":"src/A.java",'
                  '"why":"naming","blocking":false}]'),
            tool_uses=[], stop_reason="end_turn", assistant_content=[{"type": "text", "text": "x"}])
    monkeypatch.setattr(RT, "call_claude_tools", fake)

    ctx = ContextPack(selected_repo_ids=[RID])
    rf = asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx,
                                  change_set=type("CS", (), {"operations": [_op("modify", "src/A.java")]})(),
                                  reviewer_model="claude-test"))
    ghost = next(f for f in rf.findings if f.file == "src/Ghost.java")
    real = next(f for f in rf.findings if f.file == "src/A.java")
    assert "not found" in ghost.why            # annotated — the anchor is unverified
    assert ghost.blocking is True              # never silently demoted
    assert "not found" not in real.why         # existing / changed files untouched
