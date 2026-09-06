# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Judge-retry + actionability contract (215ead25 post-mortem): a deficient VERDICT is the
reviewer's failure to correct — retried once against the same diff, then routed to human
adjudication via reviewer_gaps. Never converted into code-agent work."""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.agents import agentic_review as R
from app.agents.agentic_orchestrator import _review_feedback_errors
from app.agents.context_assembler import ContextPack

RID = "repo-1"
RUN = "run-1"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / RUN / RID
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "A.java").write_text("class A {}\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"],
              ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(c, cwd=rd, check=True)
    return rd


def _script_review(monkeypatch, texts):
    """run_agent_loop stub returning scripted final_texts; records the user prompts sent."""
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs["user_prompt"])
        return SimpleNamespace(final_text=texts[min(len(calls) - 1, len(texts) - 1)])
    monkeypatch.setattr(R, "run_agent_loop", fake)
    # keep the prose-salvage path inert so tests exercise the RETRY, not the extractor
    async def no_salvage(text):
        return None
    monkeypatch.setattr(R, "_reextract_findings", no_salvage)
    return calls


def _cs():
    return type("CS", (), {"operations": []})()


# ── classification primitives ─────────────────────────────────────────────────

def test_actionable_floor():
    f = R.Finding("blocker", "correctness", "cap not enforced", blocking=True)
    assert not R._actionable(f)                                     # no anchor at all
    f2 = R.Finding("blocker", "correctness", "MISSING: AdjAmount persistence", blocking=True)
    assert R._actionable(f2)                                        # MISSING is an anchor
    f3 = R.Finding("blocker", "correctness", "hardcoded token", file="src/A.java", blocking=True)
    assert R._actionable(f3)                                        # file anchor alone suffices


def test_synthesized_and_unparseable_classified_as_reviewer_gaps():
    synth = R.Finding("blocker", "directive",
                      "[D3] NOT VERIFIED — the reviewer did not return a verdict for this "
                      "binding directive: x", blocking=True)
    sentinel = R.Finding("info", "convention", R.UNPARSEABLE_WHY, blocking=False)
    real = R.Finding("blocker", "correctness", "NPE in dispatch", file="src/A.java",
                     suggested_fix="null-check payer before dispatch", blocking=True)
    assert R._is_reviewer_gap(synth) and R._is_reviewer_gap(sentinel)
    assert not R._is_reviewer_gap(real)


# ── the retry loop ────────────────────────────────────────────────────────────

def test_unaddressed_directives_trigger_one_corrective_retry(ws, monkeypatch):
    good = ('[{"severity":"info","category":"directive","why":"[D1] PASS — src/A.java:1 ok",'
            '"blocking":false}]')
    calls = _script_review(monkeypatch, ["no json here at all", good])
    ctx = ContextPack(selected_repo_ids=[RID])
    rf = asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx, change_set=_cs(),
                                  reviewer_model="claude-test", directives=["directive one"]))
    assert len(calls) == 2                              # pass 1 deficient → one judge retry
    assert "corrective re-review" in calls[1].lower() or "DEFICIENT" in calls[1]
    assert rf.reviewer_gaps == [] and rf.blocking is False   # retry produced a clean verdict


def test_persistent_gaps_block_push_but_not_code_round(ws, monkeypatch):
    calls = _script_review(monkeypatch, ["still not json", "still not json"])
    ctx = ContextPack(selected_repo_ids=[RID])
    rf = asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx, change_set=_cs(),
                                  reviewer_model="claude-test", directives=["d1", "d2"]))
    assert len(calls) == 2                              # bounded: exactly one retry
    assert len(rf.reviewer_gaps) >= 2                   # synthesized [Dn] gaps survived
    assert rf.blocking is False                         # ⇒ no code round is dispatched
    assert any(f.blocking for f in rf.findings)         # ⇒ the push is still held (fail-closed)


def test_clean_verdict_never_retries(ws, monkeypatch):
    good = ('[{"severity":"blocker","category":"correctness","why":"NPE","file":"src/A.java",'
            '"suggested_fix":"null-check","done_when":"grep shows the guard","blocking":true}]')
    calls = _script_review(monkeypatch, [good])
    ctx = ContextPack(selected_repo_ids=[RID])
    rf = asyncio.run(R.run_review(None, run_id=RUN, ctx=ctx, change_set=_cs(),
                                  reviewer_model="claude-test"))
    assert len(calls) == 1
    assert rf.blocking is True and rf.reviewer_gaps == []
    assert rf.findings[0].done_when == "grep shows the guard"


# ── feedback routing (orchestrator side) ──────────────────────────────────────

def test_reviewer_gap_items_never_reach_the_code_agent():
    items = [
        {"category": "correctness", "why": "NPE in dispatch", "suggested_fix": "null-check",
         "file": "A.java", "line": 3, "severity": "blocker", "done_when": "guard present"},
        {"category": "directive", "why": "[D2] NOT VERIFIED — the reviewer did not return a "
         "verdict", "file": None, "line": None, "severity": "blocker", "reviewer_gap": True},
    ]
    errs = _review_feedback_errors(items, [])
    assert len(errs) == 1 and "NPE in dispatch" in errs[0]
    assert "done when: guard present" in errs[0]
    assert not any("NOT VERIFIED" in e for e in errs)
