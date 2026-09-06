# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Auto-continue on iteration cap + disk-truth change-set (§3/§9).

Big features must FINISH, not truncate: when the code agent hits the per-batch turn
cap the orchestrator re-enters the same workspace and continues, bounded by a budget.
And the change-set used for verify/freeze is read from DISK so a continued/resumed run
covers ALL edits."""
import asyncio
import subprocess
from types import SimpleNamespace

from app.core.config import settings
from app.agents import agentic_orchestrator as O
from app.agents import workspace_local as W
from app.agents.agentic_subagents import (
    ChangeSet, _code_user_prompt, _approach_block, _is_research_noise, _doc_outline)
import pytest


@pytest.fixture(autouse=True)
def _force_legacy_reviewer(monkeypatch):
    # These tests exercise the LEGACY _phase_review gate loop (stall escalation, blocker
    # gating, plan-fidelity advisory). Pin the mode so the goal_verifier default does not
    # short-circuit them; the goal_verifier path has its own tests (test_goal_verifier_core).
    monkeypatch.setattr(settings, "agentic_reviewer_mode", "legacy", raising=False)


class _Run:
    id = "run-x"
    change_request_id = "cr-x"
    selected_repo_ids: list = []
    attempts_json: dict = {}


def _fake_code(seq, ops_per_round=1):
    """A run_code_change stub that returns `stopped` values from `seq` in order.
    Rounds report `ops_per_round` file edits so the no-progress abort (two consecutive
    zero-edit capped rounds) stays quiet unless a test asks for it with 0."""
    calls = {"n": 0, "continuations": []}

    async def fake(db, *, run_id, ctx, xsd_scope=None, intent="", model=None,
                   feedback=None, continuation=None, approach=None, decisions_block="",
                   plan_block="", cancel_check=None, heartbeat=None, workspace_run_id=None,
                   resume_transcript=None, completion_check=None):
        i = calls["n"]; calls["n"] += 1
        calls["continuations"].append(continuation)
        ops = [SimpleNamespace(path=f"f{i}.java")] * ops_per_round
        return ChangeSet(operations=ops, stopped=seq[min(i, len(seq) - 1)], iterations=60)

    return fake, calls


def test_phase_code_continues_until_done(monkeypatch):
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    fake, calls = _fake_code(["max_iterations", "max_iterations", "completed"])
    monkeypatch.setattr(O.agentic_subagents, "run_code_change", fake)

    art = {"ctx": None, "intent": "x"}
    asyncio.run(O._phase_code(None, _Run(), art, None))

    assert calls["n"] == 3                                   # 2 caps then finishes
    assert art["change_set"].stopped == "completed"
    assert sum(1 for k, _ in events if k == "loop_capped") == 2   # a warning per continuation
    assert calls["continuations"][1] and calls["continuations"][1]["round"] == 2  # continue prompt threaded


def test_phase_code_respects_continuation_budget(monkeypatch):
    monkeypatch.setattr(settings, "agentic_max_code_continuations", 2)
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    fake, calls = _fake_code(["max_iterations"])             # never completes
    monkeypatch.setattr(O.agentic_subagents, "run_code_change", fake)

    art = {"ctx": None, "intent": "x"}
    asyncio.run(O._phase_code(None, _Run(), art, None))

    assert calls["n"] == 3                                   # initial + 2 continuations, then stop
    assert art["change_set"].stopped == "max_iterations"
    capped = [p for k, p in events if k == "loop_capped"]
    assert any("exhausted" in (p.get("action", "")) for p in capped)   # clear warning, not silent


def test_phase_code_aborts_after_two_no_progress_capped_rounds(monkeypatch):
    """Two consecutive capped rounds with ZERO edits → stop early and visibly (the AiNxt
    non-convergence mode), instead of grinding the whole continuation budget."""
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    fake, calls = _fake_code(["max_iterations"], ops_per_round=0)   # never edits, never completes
    monkeypatch.setattr(O.agentic_subagents, "run_code_change", fake)

    art = {"ctx": None, "intent": "x"}
    asyncio.run(O._phase_code(None, _Run(), art, None))

    assert calls["n"] == 2                                   # aborted at the second zero-edit cap
    capped = [p for k, p in events if k == "loop_capped"]
    assert any(p.get("no_progress") for p in capped)         # distinct, greppable signal


def test_continuation_prompt_carries_memory():
    # A continuation must carry MEMORY (plan + already-changed + already-read) and tell the
    # agent to finish remaining work, not re-explore.
    from app.agents.agentic_subagents import _code_user_prompt
    from app.agents.context_assembler import ContextPack
    cont = {"round": 2, "plan": "Step 1: create X\nStep 2: wire Y", "diff_stat": "  add X.java",
            "read": "  - B.java\n  - C.java"}
    p = _code_user_prompt(ContextPack(selected_repo_ids=["r"]), None, "do circle", continuation=cont)
    assert "CONTINUATION of YOUR OWN prior work" in p
    assert "Your plan:" in p and "Step 1: create X" in p
    assert "Already changed" in p and "X.java" in p
    assert "Already explored" in p and "B.java" in p
    assert "do NOT re-explore" in p or "NOT a fresh start" in p


def _resume_run(handoff):
    return SimpleNamespace(id="run-x", change_request_id="cr-x", selected_repo_ids=["repo1"],
                           attempts_json={}, handoff_json=handoff, parent_run_id=None,
                           workspace_run_id=None)


def test_crash_resume_reconstructs_continuation_from_persisted_memory(monkeypatch):
    # The bug: a cancel / API-key failure / restart discards the in-memory transcript, so the
    # resumed code agent re-read the codebase from turn 1. Now a durable code_resume state +
    # disk edits reconstruct the continuation so the FIRST call already carries plan + read-set.
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(O, "_disk_change_count", lambda run: 7)           # edits already on disk
    monkeypatch.setattr(O, "_diff_stat", lambda run: "  M FinController.java")
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(operations=[], plan={}))
    fake, calls = _fake_code(["completed"])
    monkeypatch.setattr(O.agentic_subagents, "run_code_change", fake)

    handoff = {"code_resume": {"plan": "Step 1: add log to handleReqTransfer\nStep 2: verify",
                               "read_files": [["repo1", "FinController.java"], ["repo1", "Ack.xsd"]],
                               "round": 4}}
    art = {"ctx": None, "intent": "x"}
    asyncio.run(O._phase_code(None, _resume_run(handoff), art, None))

    first = calls["continuations"][0]
    assert first is not None                                              # NOT a cold start
    assert first["round"] == 4 and "FinController.java" in first["diff_stat"]
    assert "Step 1: add log" in first["plan"]
    assert "FinController.java" in first["read"] and "Ack.xsd" in first["read"]
    assert any(k == "code_resumed" for k, _ in events)                    # visible resume-with-memory


def test_no_resume_reconstruction_on_a_cold_first_run(monkeypatch):
    # No persisted code_resume (or no disk edits) → a normal cold start, continuation stays None.
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(O, "_disk_change_count", lambda run: 0)           # nothing on disk yet
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(operations=[], plan={}))
    fake, calls = _fake_code(["completed"])
    monkeypatch.setattr(O.agentic_subagents, "run_code_change", fake)

    art = {"ctx": None, "intent": "x"}
    asyncio.run(O._phase_code(None, _resume_run({}), art, None))
    assert calls["continuations"][0] is None                             # cold start, no memory injected
    assert not any(k == "code_resumed" for k, _ in events)


def test_code_resume_state_is_persisted_each_round(monkeypatch):
    # The persistence half: after a round the plan + read-set are written to handoff_json so a
    # later resume can reconstruct the continuation.
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: None)
    monkeypatch.setattr(O, "_disk_change_count", lambda run: 0)
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(operations=[], plan={}))
    monkeypatch.setattr(O, "_heartbeat", lambda db, run, art: (lambda: None))

    async def fake(db, *, run_id, ctx, plan_block="", **k):
        return ChangeSet(operations=[SimpleNamespace(path="A.java")],
                         plan={"files": [{"path": "A.java"}]}, stopped="completed",
                         read_files=[("repo1", "A.java")], iterations=10)
    monkeypatch.setattr(O.agentic_subagents, "run_code_change", fake)

    class _DB:
        def add(self, x): pass
        def commit(self): pass
        def rollback(self): pass
    run = _resume_run({})
    asyncio.run(O._phase_code(_DB(), run, {"ctx": None, "intent": "x"}, None))
    cr = run.handoff_json.get("code_resume")
    assert cr and ["repo1", "A.java"] in cr["read_files"]                 # read-set persisted


def test_is_transient_classification():
    # Network/API/infra → pause+resume; real logic/build errors → fail.
    class APIConnectionError(Exception): pass
    class APITimeoutError(Exception): pass
    class InternalServerError(Exception): pass
    assert O._is_transient(APIConnectionError("x"))
    assert O._is_transient(APITimeoutError("x"))
    assert O._is_transient(InternalServerError("x"))
    assert O._is_transient(Exception("Connection reset by peer"))
    assert O._is_transient(Exception("Request timed out"))
    assert O._is_transient(Exception("overloaded_error"))
    assert not O._is_transient(KeyError("ctx"))
    assert not O._is_transient(ValueError("bad xsd element"))
    assert not O._is_transient(RuntimeError("compile failed: cannot find symbol"))


def test_rehydrate_art_rebuilds_ctx_on_resume(monkeypatch):
    # A recovered run runs in a FRESH worker with empty art. Resuming at code_change
    # must rebuild art['ctx'] + art['repo_base_sha'] from the clone/DB — NOT KeyError.
    from types import SimpleNamespace
    monkeypatch.setattr(O.workspace_local, "read_base_sha", lambda run_id, rid: "sha-" + rid)
    monkeypatch.setattr(O.context_assembler, "assemble_context_pack",
                        lambda db, **k: SimpleNamespace(tag="CTX"))

    run = SimpleNamespace(id="r", phase="code_change", change_request_id="cr",
                          selected_repo_ids=["repo1"])
    art = {"intent": "x", "_owner": "o"}
    O._rehydrate_art(None, run, art)
    assert art["repo_base_sha"] == {"repo1": "sha-repo1"}
    assert getattr(art["ctx"], "tag") == "CTX"


def test_grep_finds_untracked_new_files(tmp_path, monkeypatch):
    # git grep skips untracked files by default — the agent's OWN new files would be
    # invisible. --untracked must make them searchable (e.g. a '--' in a new XSD comment).
    from app.core.config import settings
    from app.agents import agentic_tools as T
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / "run1" / "repo1"
    rd.mkdir(parents=True)
    (rd / "tracked.txt").write_text("hello\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"],
              ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(c, cwd=rd, check=True)
    (rd / "ReqCirclePay.xsd").write_text("<!-- bad -- comment -->\n")     # NEW untracked file
    ctx = T.RunContext(run_id="run1", selected_repo_ids=["repo1"])
    out = T.grep(ctx, "repo1", "--")
    assert "ReqCirclePay.xsd" in out                                      # untracked file IS searched


def test_changed_files_captures_modify_delete_and_new(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / "run-x" / "repo-1"
    rd.mkdir(parents=True)
    (rd / "A.txt").write_text("one\n")
    (rd / "gone.txt").write_text("bye\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"],
              ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(c, cwd=rd, check=True)
    (rd / "A.txt").write_text("two\n")          # modify (tracked)
    (rd / "gone.txt").unlink()                  # delete (tracked)
    (rd / "B.txt").write_text("new\n")          # add (untracked — a plain `git diff` would miss this)

    changes = {path: op for op, path in W.changed_files("run-x", "repo-1")}
    assert changes.get("A.txt") == "modify"
    assert changes.get("gone.txt") == "delete"
    assert changes.get("B.txt") == "add"


def test_resolve_pom_output_dir():
    assert W._resolve_pom_output_dir("src/xjc-out") == "src/xjc-out"
    assert W._resolve_pom_output_dir("${basedir}/src/generated-java") == "src/generated-java"
    assert W._resolve_pom_output_dir("${project.build.directory}/generated-sources/jaxb") \
        == "target/generated-sources/jaxb"
    assert W._resolve_pom_output_dir("/abs/path") is None          # absolute → can't place
    assert W._resolve_pom_output_dir("${unknown.prop}/x") is None  # unresolved → don't guess
    assert W._resolve_pom_output_dir("") is None


def test_changed_files_excludes_generated_sources_by_convention(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / "run-x" / "repo-1"
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "Keep.java").write_text("class Keep{}\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"],
              ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(c, cwd=rd, check=True)
    # Generated output relocated OUT of target/ but conventionally named — must not leak.
    (rd / "mod" / "generated-sources").mkdir(parents=True)
    (rd / "mod" / "generated-sources" / "G1.java").write_text("class G1{}\n")
    (rd / "mod" / "generated").mkdir(parents=True)
    (rd / "mod" / "generated" / "G2.java").write_text("class G2{}\n")
    (rd / "src" / "Real.java").write_text("class Real{}\n")        # genuine new source

    paths = {path for _op, path in W.changed_files("run-x", "repo-1")}
    assert "src/Real.java" in paths
    assert "mod/generated-sources/G1.java" not in paths
    assert "mod/generated/G2.java" not in paths


def test_changed_files_excludes_pom_declared_jaxb_output(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / "run-x" / "repo-1"
    mod = rd / "netc-xsd-domain"
    mod.mkdir(parents=True)
    # netc points its JAXB plugin at an UNCONVENTIONAL literal path outside target/.
    (mod / "pom.xml").write_text(
        "<project><build><plugins><plugin>"
        "<artifactId>cxf-xjc-plugin</artifactId>"
        "<configuration><sourceRoot>src/xjc-out</sourceRoot></configuration>"
        "</plugin></plugins></build></project>\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"],
              ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(c, cwd=rd, check=True)
    (mod / "src" / "xjc-out" / "com").mkdir(parents=True)
    (mod / "src" / "xjc-out" / "com" / "Foo.java").write_text("class Foo{}\n")   # generated
    (mod / "src" / "main").mkdir(parents=True)
    (mod / "src" / "main" / "Real.java").write_text("class Real{}\n")            # hand-written

    paths = {path for _op, path in W.changed_files("run-x", "repo-1")}
    assert "netc-xsd-domain/src/main/Real.java" in paths
    assert "netc-xsd-domain/src/xjc-out/com/Foo.java" not in paths


# ── Plan fidelity: the ratified plan drives Phase B, and an incomplete plan is re-driven ──

def test_render_analysis_plan_carries_the_approved_spec():
    out = O._render_analysis_plan(
        {"data_model_changes": "add tipAmount", "schema_inventory": [{"repo": "core", "path": "ReqTransfer.xsd"}],
         "modules": ["txn"], "constraints": ["paise only"]},
        {"overview": "Add tip to ReqTransfer", "assumptions": ["default tip=0"]},
        {"steps": ["debit", "credit"]})
    for must in ("OVERVIEW: Add tip", "FLOW STEPS:", "DATA MODEL CHANGES:",
                 "SCHEMA FILES: core:ReqTransfer.xsd", "RATIFIED ASSUMPTIONS"):
        assert must in out
    assert O._render_analysis_plan({}, {}, {}) == ""        # nothing ratified → empty


def test_plan_gap_feedback_flags_unfinished_plan():
    # Agent's own plan listed A+B+C but it only touched A (the "collapsed to one edit" symptom).
    cs = ChangeSet(operations=[SimpleNamespace(op="delete", path="A.java")],
                   plan={"files": [{"path": "A.java"}, {"path": "B.java"}, {"path": "C.java"}]},
                   stopped="completed")
    gaps = O._plan_gap_feedback(cs)
    assert "B.java" in gaps and "C.java" in gaps
    # Every planned file touched → no gap.
    done = ChangeSet(operations=[SimpleNamespace(op="modify", path="A.java")],
                     plan={"files": [{"path": "A.java"}]}, stopped="completed")
    assert O._plan_gap_feedback(done) == ""


def test_code_user_prompt_makes_the_plan_authoritative():
    out = _code_user_prompt(None, None, "add tip", plan_block="OVERVIEW: add tip")
    assert "Approved implementation plan (BINDING" in out
    assert "REFERENCE ONLY" in out                          # BRD/TSD demoted to reference
    # No plan (legacy run) → unchanged legacy instruction, no plan section.
    out0 = _code_user_prompt(None, None, "add tip")
    assert "Approved implementation plan" not in out0
    assert out0.endswith("Implement the change. submit_plan first, read before editing.")


def test_phase_code_drives_completion_round_when_plan_incomplete(monkeypatch):
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    plan = {"files": [{"path": "A.java"}, {"path": "B.java"}]}
    calls = {"n": 0}

    async def fake(db, *, run_id, ctx, xsd_scope=None, intent="", model=None, feedback=None,
                   continuation=None, approach=None, decisions_block="", plan_block="",
                   cancel_check=None, heartbeat=None, workspace_run_id=None,
                   resume_transcript=None, completion_check=None):
        calls["n"] += 1
        if calls["n"] == 1:                                 # declared done, but only touched A
            ops = [SimpleNamespace(op="modify", path="A.java")]
        else:                                               # the completion round finishes B
            ops = [SimpleNamespace(op="modify", path="A.java"), SimpleNamespace(op="modify", path="B.java")]
        return ChangeSet(operations=ops, plan=plan, stopped="completed", iterations=5)

    # The gap check now reads the CUMULATIVE DISK truth (not the last round's in-memory ops). Mirror
    # disk: after round 1 only A is on disk (B still missing → drive a completion round); after the
    # completion round both A and B are on disk (plan complete → stop).
    def fake_disk(db, run):
        paths = ["A.java"] if calls["n"] <= 1 else ["A.java", "B.java"]
        return SimpleNamespace(operations=[SimpleNamespace(op="modify", path=p, content="x") for p in paths])
    monkeypatch.setattr(O, "_disk_change_set", fake_disk)

    monkeypatch.setattr(O.agentic_subagents, "run_code_change", fake)
    art = {"ctx": None, "intent": "x"}
    asyncio.run(O._phase_code(None, _Run(), art, None))

    assert calls["n"] == 2                                  # one completion round was driven
    assert any(k == "plan_incomplete" for k, _ in events)
    assert calls["n"] == 2 and art["change_set"].operations[-1].path == "B.java"


# ── Tier-1 context-harness: stop the handoff leaks (produced-but-dropped context) ──

def test_code_user_prompt_carries_jaxb_links_and_concerns():
    scope = SimpleNamespace(
        edits_applied=["core:ReqTransfer.xsd"],
        diff_record={"core:ReqTransfer.xsd": {"modified": ["Payee"], "new": ["TipAmount"]}},
        java_links=[{"xpath": "Payee", "symbol": "Payee.java::Payee"},     # changed → surfaced
                    {"xpath": "Unrelated", "symbol": "X.java"}],            # unchanged → filtered
        concerns=[{"message": "renaming Foo breaks consumers", "declined_change": "rename Foo->Bar"}])
    out = _code_user_prompt(None, scope, "add tip")
    assert "Payee → Payee.java::Payee" in out                # pre-change accessor map (was dropped)
    assert "Unrelated" not in out                            # only changed elements, not noise
    assert "RISKY schema changes flagged in Phase A" in out  # concerns (were dropped)
    assert "renaming Foo breaks consumers" in out and "[declined: rename Foo->Bar]" in out
    # No xsd_scope → none of the new blocks appear (backward compatible).
    assert "JAXB element→Java accessors" not in _code_user_prompt(None, None, "x")


def test_approach_block_carries_rationale_not_just_titles():
    out = _approach_block({
        "option": {"title": "Extend ReqTransfer", "target_api": "ReqTransfer",
                   "how_it_fits": "money leg already runs here", "tradeoffs": "none"},
        "directive": "extend ReqTransfer.Payee",
        "rejected": [{"title": "New API", "why_not": "parallel state machine"}],
        "evidence": [{"file": "ReqTransfer.java"}]})
    assert "Why it fits:" in out and "money leg already runs here" in out and "rides ReqTransfer" in out
    assert "New API — parallel state machine" in out         # rejected WITH reason, not bare title
    assert _approach_block(None) == ""                       # no approach → empty


def test_render_analysis_plan_carries_flow_structure_and_risks():
    out = O._render_analysis_plan(
        {"risks": ["breaks old consumers if cardinality changes"]},
        {"overview": "add tip"},
        {"actors": ["PSP", "NPCI"], "states": ["INIT", "SETTLED"], "steps": ["debit"]})
    for must in ("FLOW ACTORS:", "STATE MACHINE:", "RISKS (implement with guardrails):",
                 "breaks old consumers"):
        assert must in out


# ── Tier 2 (working memory) + Tier 3 (new retrieval tools) ──

def test_continuation_and_feedback_memory():
    # Tier 2.1 — the agent's own notes carry into the next round.
    out = _code_user_prompt(None, None, "x", continuation={"round": 2, "notes": "assembler at line 42"})
    assert "Your own notes/findings from the last round" in out and "assembler at line 42" in out
    # Tier 2.2 — earlier failed attempts surface so a regressed fix isn't repeated.
    out2 = _code_user_prompt(None, None, "x", feedback={
        "gates": {}, "errors": ["B:10 cannot find symbol"],
        "history": [{"errors": ["A:5 incompatible types"]}, {"errors": ["B:10 cannot find symbol"]}]})
    assert "already failed verification 3×" in out2 and "A:5 incompatible types" in out2
    assert "already failed verification" not in _code_user_prompt(None, None, "x", feedback={"errors": ["e"]})


def test_new_tools_registered_and_available_to_code_agent():
    import pytest
    from app.agents.agentic_tools import _DISPATCH, TOOL_SCHEMAS, callers, jaxb_accessors, ToolError
    from app.agents.agentic_subagents import CODE_TOOLS
    assert "callers" in _DISPATCH and "jaxb_accessors" in _DISPATCH
    schema_names = {t["name"] for t in TOOL_SCHEMAS}
    assert {"callers", "jaxb_accessors"} <= schema_names
    code_names = {t["name"] for t in CODE_TOOLS}
    assert {"callers", "jaxb_accessors"} <= code_names          # code agent can call them
    # guard paths (no DB) — fail-open, never raise except on missing arg
    ctx = SimpleNamespace(db=None, selected_repo_ids=["r1"])
    with pytest.raises(ToolError):
        callers(ctx, symbol="")
    assert "call graph unavailable" in callers(ctx, symbol="foo")
    with pytest.raises(ToolError):
        jaxb_accessors(ctx, element="")
    assert "JAXB link index unavailable" in jaxb_accessors(ctx, element="ReqTransfer")


# ── Plan-execution fidelity: completion gate + plan-aware review loop ──

def test_plan_gap_feedback_catches_stubs_and_missing_files():
    Op = lambda op, path, content=None: SimpleNamespace(op=op, path=path, content=content)
    CS = lambda ops, plan: SimpleNamespace(operations=ops, plan=plan)
    # TODO/stub left in produced code → half-baked even though the file was touched
    g = O._plan_gap_feedback(CS([Op("modify", "A.java", "void route(){ // TODO settle\n}")],
                                {"files": [{"path": "A.java"}]}))
    assert "unfinished placeholders" in g and "A.java" in g
    # UnsupportedOperationException stub
    assert "placeholders" in O._plan_gap_feedback(
        CS([Op("modify", "B.java", "throw new UnsupportedOperationException();")], {"files": [{"path": "B.java"}]}))
    # planned file untouched
    g2 = O._plan_gap_feedback(CS([Op("modify", "A.java", "real")], {"files": [{"path": "A.java"}, {"path": "B.java"}]}))
    assert "planned files you have NOT changed" in g2 and "B.java" in g2
    # complete + clean → no gap
    assert O._plan_gap_feedback(CS([Op("modify", "A.java", "real impl")], {"files": [{"path": "A.java"}]})) == ""


def test_review_blocking_feedback_renders_as_finish_not_compile():
    out = _code_user_prompt(None, None, "x", feedback={
        "source": "review", "errors": ["Payee.java:5 [correctness] tipAmount parsed but never consumed"]})
    assert "REVIEWER BLOCKED" in out and "tipAmount parsed but never consumed" in out
    assert "Previous verification FAILED" not in out
    # a build failure (no source) still reads as a compile failure
    vf = _code_user_prompt(None, None, "x", feedback={"errors": ["X.java:1 cannot find symbol"]})
    assert "Previous verification FAILED" in vf and "REVIEWER BLOCKED" not in vf


def test_phase_review_stores_blocking_findings_and_feeds_plan_to_reviewer(monkeypatch):
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    captured = {}
    F = lambda blocking, why, sev="warning": SimpleNamespace(blocking=blocking, severity=sev,
                                              category="correctness", why=why,
                                              suggested_fix="do X", file="A.java", line=5)

    async def fake_review(db, **kw):
        captured.update(kw)
        return SimpleNamespace(findings=[F(True, "tipAmount parsed but never consumed", "error"),
                                         F(False, "nit", "info")],
                               blocking=True, reviewer_model="claude")

    monkeypatch.setattr(O.agentic_review, "run_review", fake_review)
    art = {"ctx": SimpleNamespace(selected_repo_ids=["r1"]), "intent": "x",
           "change_set": ChangeSet(operations=[], plan={"summary": "s", "files": [{"path": "A.java"}]})}
    blocking = asyncio.run(O._phase_review(None, _Run(), art, None))
    assert blocking is True
    items = art["review"]["items"]
    assert len(items) == 1 and "never consumed" in items[0]["why"]    # only BLOCKING finding stored
    assert "A.java" in captured.get("plan_block", "")                # reviewer received the plan


def _blocking_review(captured):
    F = lambda: SimpleNamespace(blocking=True, severity="blocker", category="correctness",
                                why="x never consumed", suggested_fix="do X", file="A.java", line=5)

    async def fake_review(db, **kw):
        captured.update(kw)
        return SimpleNamespace(findings=[F()], blocking=True, reviewer_model="claude")
    return fake_review


def test_review_resume_restores_read_set_on_the_same_round(monkeypatch):
    # An interrupted review round is re-entered (same round) → the persisted read-set is handed back
    # to the reviewer and a visible review_resumed event fires. Mirrors code_resumed for the code phase.
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    captured = {}
    monkeypatch.setattr(O.agentic_review, "run_review", _blocking_review(captured))

    handoff = {"review_resume": {"read_files": [["repo1", "A.java"], ["repo1", "B.java"]], "round": 1}}
    art = {"ctx": SimpleNamespace(selected_repo_ids=["r1"]), "intent": "x",
           "change_set": ChangeSet(operations=[], plan={"summary": "s"})}
    asyncio.run(O._phase_review(None, _resume_run(handoff), art, None))

    assert captured.get("resume_read_files") == [["repo1", "A.java"], ["repo1", "B.java"]]  # fed to reviewer
    assert any(k == "review_resumed" for k, _ in events)                                     # visible resume


def test_review_resume_ignored_when_round_has_advanced(monkeypatch):
    # A review_resume from an EARLIER round (already routed through code_change and back) is stale —
    # prior_blockers carry that memory instead. rounds=1 here (attempts empty), resume is for round 5.
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    captured = {}
    monkeypatch.setattr(O.agentic_review, "run_review", _blocking_review(captured))

    handoff = {"review_resume": {"read_files": [["repo1", "A.java"]], "round": 5}}
    art = {"ctx": SimpleNamespace(selected_repo_ids=["r1"]), "intent": "x",
           "change_set": ChangeSet(operations=[], plan={"summary": "s"})}
    asyncio.run(O._phase_review(None, _resume_run(handoff), art, None))

    assert captured.get("resume_read_files") is None                 # stale round → not replayed
    assert not any(k == "review_resumed" for k, _ in events)


def test_review_resume_persisted_via_progress_then_cleared_on_completion(monkeypatch):
    # Persistence half: the read-set checkpoint lands in handoff_json mid-review (so a crash resumes),
    # and is CLEARED once the round completes (so the next round can't replay a stale read-set).
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: None)
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(operations=[], plan={}))
    monkeypatch.setattr(O, "_analysis_plan_block", lambda db, crid: "")
    monkeypatch.setattr(O, "_success_criteria", lambda db, crid: "")

    async def _pf(db, run, plan_block, cs):
        return {"findings": [], "has_gap": False, "missing_files": []}
    monkeypatch.setattr(O, "_plan_fidelity_call", _pf)
    for flag in ("agentic_parallel_review", "agentic_contract_gate", "agentic_di_gate",
                 "agentic_doc_code_gate", "agentic_acceptance_predicates"):
        monkeypatch.setattr(O.settings, flag, False, raising=False)
    monkeypatch.setattr(O.settings, "agentic_max_stall_rounds", 0, raising=False)

    captured = {}

    async def fake_review(db, **kw):
        kw["progress"]([("repo1", "A.java"), ("repo1", "B.java")])   # the loop checkpoints its read-set
        captured["mid"] = dict(run.handoff_json or {})              # snapshot while the round is in flight
        return SimpleNamespace(findings=[], blocking=False, reviewer_model="claude",
                               reviewer_gaps=[], rounds=1)
    monkeypatch.setattr(O.agentic_review, "run_review", fake_review)

    class _DB:
        def add(self, x): pass
        def commit(self): pass
        def rollback(self): pass
        def flush(self): pass
    run = _resume_run({})
    asyncio.run(O._phase_review(_DB(), run, {"ctx": SimpleNamespace(selected_repo_ids=["r1"]),
                                             "intent": "x",
                                             "change_set": ChangeSet(operations=[], plan={})}, None))

    assert ["repo1", "A.java"] in captured["mid"]["review_resume"]["read_files"]   # checkpointed mid-round
    assert captured["mid"]["review_resume"]["round"] == 1
    assert "review_resume" not in (run.handoff_json or {})                         # cleared on completion


def test_review_resume_checkpoint_accumulates_across_attempts(monkeypatch):
    # The checkpoint must UNION with the restored set, not overwrite it. run_review hands the
    # restored paths to the reviewer as prompt text only, so ctx.read_files starts empty on a
    # resume — overwriting would shrink a 3-file memory to the 1 file the resumed reviewer
    # re-opened, and a second interruption would then resume with almost nothing.
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: None)
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(operations=[], plan={}))
    monkeypatch.setattr(O, "_analysis_plan_block", lambda db, crid: "")
    monkeypatch.setattr(O, "_success_criteria", lambda db, crid: "")

    async def _pf(db, run, plan_block, cs):
        return {"findings": [], "has_gap": False, "missing_files": []}
    monkeypatch.setattr(O, "_plan_fidelity_call", _pf)
    for flag in ("agentic_parallel_review", "agentic_contract_gate", "agentic_di_gate",
                 "agentic_doc_code_gate", "agentic_acceptance_predicates"):
        monkeypatch.setattr(O.settings, flag, False, raising=False)
    monkeypatch.setattr(O.settings, "agentic_max_stall_rounds", 0, raising=False)

    captured = {}

    async def fake_review(db, **kw):
        kw["progress"]([("repo1", "D.java")])        # the resumed loop re-opens ONE file
        captured["mid"] = dict(run.handoff_json or {})
        return SimpleNamespace(findings=[], blocking=False, reviewer_model="claude",
                               reviewer_gaps=[], rounds=1)
    monkeypatch.setattr(O.agentic_review, "run_review", fake_review)

    class _DB:
        def add(self, x): pass
        def commit(self): pass
        def rollback(self): pass
        def flush(self): pass

    prior = [["repo1", "A.java"], ["repo1", "B.java"], ["repo1", "C.java"]]
    run = _resume_run({"review_resume": {"read_files": prior, "round": 1}})
    asyncio.run(O._phase_review(_DB(), run, {"ctx": SimpleNamespace(selected_repo_ids=["r1"]),
                                             "intent": "x",
                                             "change_set": ChangeSet(operations=[], plan={})}, None))

    kept = captured["mid"]["review_resume"]["read_files"]
    assert ["repo1", "D.java"] in kept                       # the new read is added
    for p in prior:
        assert p in kept                                     # …and none of the restored set is lost
    assert len(kept) == 4


def _run_review_with_predicates(monkeypatch, predicates):
    """Drive _phase_review with a clean (non-blocking) reviewer and the given acceptance
    predicates under ENFORCE, against a diff that satisfies none of them."""
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)

    async def fake_review(db, **kw):
        return SimpleNamespace(findings=[], blocking=False, reviewer_model="claude",
                               reviewer_gaps=[], rounds=1)

    monkeypatch.setattr(O.agentic_review, "run_review", fake_review)
    monkeypatch.setattr(O.settings, "agentic_acceptance_predicates", True, raising=False)
    monkeypatch.setattr(O.settings, "agentic_acceptance_predicates_enforce", True, raising=False)
    monkeypatch.setattr(O, "_fidelity_diff_summary",
                        lambda run, cs: "diff --git a/A.java b/A.java\n+++ b/A.java\n+int y = 2;\n")
    art = {"ctx": SimpleNamespace(selected_repo_ids=["r1"]), "intent": "x",
           "change_set": ChangeSet(operations=[], plan={"summary": "s"}),
           "acceptance_predicates": predicates}
    blocking = asyncio.run(O._phase_review(None, _Run(), art, None))
    return blocking, art


def test_must_block_overflow_is_counted_not_silent(monkeypatch):
    # 215ead25 had 21 must-block findings; the 15-item cap silently dropped 6 from the
    # fix list, which resurfaced next round as "new" — incremental discovery. The cap
    # stays (Codex P0: has_blocker derives from the persisted slice) but the overflow
    # count must be surfaced.
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    F = lambda i: SimpleNamespace(blocking=True, severity="blocker", category="correctness",
                                  why=f"bug {i}", suggested_fix="fix it", file=f"F{i}.java",
                                  line=1, done_when="")

    async def fake_review(db, **kw):
        return SimpleNamespace(findings=[F(i) for i in range(21)], blocking=True,
                               reviewer_model="claude", reviewer_gaps=[], rounds=1)

    monkeypatch.setattr(O.agentic_review, "run_review", fake_review)
    art = {"ctx": SimpleNamespace(selected_repo_ids=["r1"]), "intent": "x",
           "change_set": ChangeSet(operations=[], plan={"summary": "s"})}
    blocking = asyncio.run(O._phase_review(None, _Run(), art, None))
    rv = art["review"]
    assert blocking is True and rv["has_blocker"] is True
    assert len(rv["items"]) == 15
    assert rv["dropped_must_block"] == 6


def test_bare_token_predicate_is_advisory_under_enforce(monkeypatch):
    # A naming-sensitive bare-token predicate false-unmets on correct code under a
    # different valid name (is_bare_token contract). Under ENFORCE it must surface as
    # a WARNING ("verify by behaviour"), never as a must-block "inject this literal"
    # order — while a structural miss keeps its full blocking force.
    blocking, art = _run_review_with_predicates(monkeypatch, [
        {"kind": "added_anywhere", "contains": "purposeRemark", "desc": "adds purposeRemark"},
        {"kind": "file_touched", "file": "Missing.java", "desc": "touches Missing.java"},
    ])
    rv = art["review"]
    items = rv["items"]
    bare = [i for i in items if "naming-sensitive" in i["why"]]
    hard = [i for i in items if "Missing.java" in (i.get("file") or "")]
    assert bare and bare[0]["severity"] == "warning"
    assert hard and hard[0]["severity"] == "blocker"
    assert blocking is True and rv["has_blocker"] is True        # structural miss still blocks
    # The precise definition-of-done block is SET for the structural miss only —
    # and now actually delivered to the fix list (was write-only before).
    assert "Missing.java" in art["acceptance_feedback"]
    assert "purposeRemark" not in art["acceptance_feedback"]


def test_bare_token_only_never_blocks(monkeypatch):
    blocking, art = _run_review_with_predicates(monkeypatch, [
        {"kind": "added_anywhere", "contains": "purposeRemark", "desc": "adds purposeRemark"},
    ])
    rv = art["review"]
    assert blocking is False                                     # no round spent on a rename-level miss
    assert rv["has_blocker"] is False                            # push not gated by it
    assert any("naming-sensitive" in i["why"] for i in rv["items"])   # …but the human still sees it
    assert art["acceptance_feedback"] == ""                      # nothing stale to deliver


def test_deep_research_excluded_from_codegen_docs_but_rules_kept():
    # Deep-research narrative → dropped for code-gen.
    for h in ("Market Research & Scalability", "Product & Ecosystem Context",
              "Risk Assessment", "MVP Approach", "Background"):
        assert _is_research_noise(h), h
    # Code-critical rules → KEPT even if research-flavoured (never lose mandatory validation/error codes).
    for h in ("Error Codes", "Regulatory & Compliance", "Validation Rules", "API Changes", "Data Model"):
        assert not _is_research_noise(h), h
    out = _doc_outline("BRD", {"Market Research": "x", "API Changes": "y", "Error Codes": "z"})
    assert "API Changes" in out and "Error Codes" in out and "Market Research" not in out


# ── Context window: read-cache + lossless history compaction ──

def test_history_compaction_keeps_reasoning_evicts_old_bulk():
    from app.agents.agentic_runtime import _history_tool_chars, _compact_messages
    big = "X" * 5000
    msgs = [{"role": "user", "content": "brief"}]
    for k in range(8):
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"step {k}"}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{k}", "content": big}]})
    before = _history_tool_chars(msgs)
    reclaimed = _compact_messages(msgs, keep_tail=4)
    assert reclaimed > 0 and _history_tool_chars(msgs) < before
    assert msgs[0]["content"] == "brief"                                  # the brief is kept
    assert all("step" in m["content"][0]["text"] for m in msgs if m["role"] == "assistant")  # reasoning kept
    assert msgs[-1]["content"][0]["content"] == big                      # recent tail kept verbatim
    assert "evicted earlier tool output" in msgs[2]["content"][0]["content"]  # old bulk evicted
    assert _compact_messages(msgs, keep_tail=4) == 0                      # idempotent — no double-evict


def test_reclaimable_tool_chars_drops_to_floor_after_compaction():
    # The thrash guard's core: once the history is compacted, there is nothing left to
    # evict, so _reclaimable_tool_chars falls below the evict floor and the loop STOPS
    # re-summarizing+re-compacting every turn (the "compacting too often" report). It must
    # exactly mirror what _compact_messages would evict.
    from app.agents.agentic_runtime import _reclaimable_tool_chars, _compact_messages, _EVICT_MIN_CHARS
    big = "X" * 5000
    msgs = [{"role": "user", "content": "brief"}]
    for k in range(8):
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"step {k}"}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{k}", "content": big}]})
    # Before: real evictable bulk exists → the guard would let compaction run.
    assert _reclaimable_tool_chars(msgs, keep_tail=4) >= 5000
    _compact_messages(msgs, keep_tail=4)
    # After: everything evictable is a stub → reclaimable below the floor → guard skips
    # (no thrash), even though the array (system/summary/tail) may still exceed the window.
    assert _reclaimable_tool_chars(msgs, keep_tail=4) < _EVICT_MIN_CHARS


def test_compaction_summary_folds_into_brief_and_keeps_agent_directed(monkeypatch):
    from app.agents import agentic_runtime as RT

    class _T:
        def __init__(self, t): self.text = t

    async def fake_cct(**kw):
        # the summary call must be a valid alternating conversation ending in a user instruction
        roles = [m["role"] for m in kw["messages"]]
        assert kw["messages"][-1]["role"] == "user"
        assert all(roles[k] != roles[k + 1] for k in range(len(roles) - 1))
        return _T("GOAL: add tip. FILES: Payee.java -> getPayee(). EDITS: none. LEFT: route it.")

    monkeypatch.setattr(RT, "call_claude_tools", fake_cct)
    msgs = [{"role": "user", "content": "BRIEF: implement tip"},
            {"role": "assistant", "content": [{"type": "text", "text": "reasoning"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "X" * 5000}]}]
    ok = asyncio.run(RT._pin_progress_summary(msgs, "sys", "m", "code_change", [{"type": "text", "text": "a"}]))
    assert ok and RT._SUMMARY_MARKER in msgs[0]["content"]
    assert msgs[0]["content"].startswith("BRIEF: implement tip") and "GOAL: add tip" in msgs[0]["content"]
    assert len(msgs) == 3 and msgs[0]["role"] == "user"                  # folded into brief, not inserted
    asyncio.run(RT._pin_progress_summary(msgs, "sys", "m", "code_change", [{"type": "text", "text": "a"}]))
    assert msgs[0]["content"].count(RT._SUMMARY_MARKER) == 1             # refreshed, not duplicated


def test_seed_read_files_credits_multi_repo_bare_paths(monkeypatch, tmp_path):
    # Regression: the model routinely OMITS repo_id (relies on path inference). On a MULTI-repo
    # replay this used to seed nothing → the propose/plan evidence gate bounced with "read 0
    # files" and the model re-generated a whole plan. Seeding must resolve the repo the same
    # way the tool does (T._resolve_repo_id), not only for single-repo runs.
    # INTEGRITY (2026-08): seeding also validates the replayed content against the file on
    # disk — an unchanged file seeds as a FULL read; a file that CHANGED since the
    # transcript, a skeleton result, and an errored read never seed.
    from types import SimpleNamespace
    from app.agents import agentic_runtime as RT
    import app.agents.agentic_tools as T

    (tmp_path / "A.java").write_text("class A {}")
    (tmp_path / "B.java").write_text("class B {}")
    (tmp_path / "C.java").write_text("class C { rewritten since the transcript }")

    def fake_resolve_repo(ctx, repo_id, path=None):
        if repo_id:
            return repo_id
        return ({"A.java": "r1", "B.java": "r2", "C.java": "r1", "S.java": "r1"}.get(path)
                or (_ for _ in ()).throw(T.ToolError("x")))
    monkeypatch.setattr(T, "_resolve_repo_id", fake_resolve_repo)
    monkeypatch.setattr(T, "_resolve", lambda ctx, rid, path: tmp_path / path)

    ctx = SimpleNamespace(selected_repo_ids=["r1", "r2"], read_files=set(),
                          full_reads=set(), read_ranges={}, read_hashes={})
    transcript = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "read_file", "input": {"path": "A.java"}},   # repo_id omitted
            {"type": "tool_use", "id": "b", "name": "read_file", "input": {"path": "B.java"}},
            {"type": "tool_use", "id": "c", "name": "read_file", "input": {"path": "Z.java"}},   # unresolvable
            {"type": "tool_use", "id": "d", "name": "read_file", "input": {"path": "C.java"}},   # changed since
            {"type": "tool_use", "id": "e", "name": "read_file", "input": {"path": "S.java"}}]},  # skeleton view
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "class A {}"},
            # notes-prefixed full read — the endswith() tolerance for _module_notes headers
            {"type": "tool_result", "tool_use_id": "b", "content": "[MODULE_NOTES — x]\n\nclass B {}"},
            {"type": "tool_result", "tool_use_id": "c", "content": "not found", "is_error": True},
            {"type": "tool_result", "tool_use_id": "d", "content": "class C { old body }"},
            {"type": "tool_result", "tool_use_id": "e",
             "content": "[S.java is large (9000 lines / 90000 bytes) — showing its STRUCTURE]"}]},
    ]
    RT._seed_read_files(ctx, transcript)
    # unchanged files seeded (as FULL reads); errored / changed-on-disk / skeleton skipped
    assert {p for (_r, p) in ctx.read_files} == {"A.java", "B.java"}
    assert {p for (_r, p) in ctx.full_reads} == {"A.java", "B.java"}
    assert ("r1", "C.java") not in ctx.read_files and ("r1", "S.java") not in ctx.read_files


def test_read_file_content_cache_invalidates_on_mtime(tmp_path):
    import os, time
    from app.agents.agentic_tools import _cached_read_text
    p = tmp_path / "F.java"; p.write_text("v1")
    assert _cached_read_text(p) == "v1"                                  # miss → disk → cached
    st = p.stat(); p.write_text("v2"); os.utime(p, (st.st_atime, st.st_mtime))
    assert _cached_read_text(p) == "v1"                                  # same mtime → served from cache
    time.sleep(0.01); p.write_text("v3"); os.utime(p, None)
    assert _cached_read_text(p) == "v3"                                  # mtime bump → invalidated → fresh


def test_introspection_tools_registered_and_scoped():
    import pytest
    from app.agents.agentic_tools import _DISPATCH, TOOL_SCHEMAS, git_history, ToolError
    from app.agents.agentic_subagents import CODE_TOOLS, XSD_TOOLS, ANALYSIS_TOOLS, PROPOSE_TOOLS
    assert {"show_diff", "git_history"} <= set(_DISPATCH)
    assert {"show_diff", "git_history"} <= {t["name"] for t in TOOL_SCHEMAS}
    assert {"show_diff", "git_history"} <= {t["name"] for t in CODE_TOOLS}            # code agent self-check + history
    assert {"show_diff", "git_history"} <= {t["name"] for t in XSD_TOOLS}             # xsd agent edits → same
    assert {"git_history", "jaxb_accessors"} <= {t["name"] for t in ANALYSIS_TOOLS}   # plan must enumerate consumers
    assert {"git_history", "callers"} <= {t["name"] for t in PROPOSE_TOOLS}           # approach must name consumers
    with pytest.raises(ToolError):
        git_history(SimpleNamespace(selected_repo_ids=["r1"]), path="")


# ── Unproductive code phase must FAIL, not silently "finalise" an empty/budget-exhausted change ──

def test_disk_change_count(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / "run-x" / "repo-1"
    rd.mkdir(parents=True)
    (rd / "A.txt").write_text("one\n")
    for c in (["git", "init", "-q"], ["git", "add", "-A"],
              ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(c, cwd=rd, check=True)
    run = SimpleNamespace(id="run-x", selected_repo_ids=["repo-1"], workspace_run_id=None)
    assert O._disk_change_count(run) == 0            # clean tree
    (rd / "A.txt").write_text("two\n"); (rd / "B.txt").write_text("new\n")
    assert O._disk_change_count(run) == 2            # one modify + one new


def test_code_phase_empty_change_is_surfaced_not_finalised(monkeypatch):
    from app.agents.agentic_subagents import ChangeSet
    from app.models.agentic import AgenticPhase as P

    def run_step(stopped, disk_n):
        outcome = {}
        async def fake_phase_code(db, run, art, model):
            art["change_set"] = ChangeSet(operations=[], stopped=stopped)
        monkeypatch.setattr(O, "_phase_code", fake_phase_code)
        monkeypatch.setattr(O, "_disk_change_count", lambda run: disk_n)
        monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
        monkeypatch.setattr(O.S, "advance", lambda db, run, ph: outcome.update(advanced=getattr(ph, "value", ph)))
        monkeypatch.setattr(O.S, "mark_terminal",
                            lambda db, run, st, error=None: outcome.update(terminal=getattr(st, "value", st)))
        run = SimpleNamespace(id="r", phase="code_change", selected_repo_ids=[], error_code=None, kind="code")
        asyncio.run(O._step(None, run, {"ctx": None}, None))
        return outcome

    # Zero edits → surfaced (not silently finalised). Note: running out of context is NOT a failure
    # anymore — the loop compacts + continues — so the guard is now purely the empty/cancelled net.
    assert run_step("completed", 0).get("terminal") == "failed"
    assert "advanced" not in run_step("completed", 0)
    assert run_step("cancelled", 5).get("terminal") == "failed"                  # cancelled → not finalised
    done = run_step("completed", 3)                                              # real edits → proceed to verify
    assert done.get("advanced") == P.VERIFICATION.value and "terminal" not in done


# ── Parallel review (default-OFF) must be OUTPUT-EQUIVALENT to sequential ──

def test_parallel_review_verdict_is_identical_to_sequential(monkeypatch):
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    F = lambda blocking, why, sev: SimpleNamespace(blocking=blocking, severity=sev,
                                                   category="correctness", why=why,
                                                   suggested_fix="fix", file="A.java", line=3)

    async def fake_review(db, **kw):
        return SimpleNamespace(findings=[F(True, "real blocker", "error"), F(False, "nit", "info")],
                               blocking=True, reviewer_model="claude")
    monkeypatch.setattr(O.agentic_review, "run_review", fake_review)

    async def fake_pf(db, run, plan_block, cs_disk):
        return {"findings": [{"item": "B.java", "detail": "never delivered",
                              "kind": "missing_file", "severity": "blocker"}],
                "has_gap": True, "missing_files": ["B.java"]}
    monkeypatch.setattr(O, "_plan_fidelity_call", fake_pf)        # both reviewers return FIXED data

    def _run(parallel):
        monkeypatch.setattr(settings, "agentic_parallel_review", parallel)
        art = {"ctx": SimpleNamespace(selected_repo_ids=["r1"]), "intent": "x",
               "change_set": ChangeSet(operations=[], plan={"summary": "s", "files": [{"path": "A.java"}]})}
        blocking = asyncio.run(O._phase_review(None, _Run(), art, None))
        return blocking, art["review"]

    seq_blocking, seq_review = _run(False)
    par_blocking, par_review = _run(True)
    assert seq_blocking is True and par_blocking is True
    assert seq_review == par_review                              # byte-identical verdict regardless of timing
    assert seq_review["has_blocker"] and seq_review["plan_fidelity_gaps"] == 1


# ── R1′: an LLM-only behavioural-completeness opinion must not spin the loop forever ──

def _review_with(monkeypatch, *, reviewer_blocking, pf):
    """Drive _phase_review with a stubbed reviewer + plan-fidelity; returns a runner(review_count)->bool."""
    monkeypatch.setattr(settings, "agentic_parallel_review", False)

    async def fake_review(db, **kw):
        F = SimpleNamespace(blocking=True, severity="error", category="correctness",
                            why="real bug", suggested_fix="fix", file="A.java", line=3)
        return SimpleNamespace(findings=[F] if reviewer_blocking else [],
                               blocking=reviewer_blocking, reviewer_model="claude")
    monkeypatch.setattr(O.agentic_review, "run_review", fake_review)

    async def fake_pf(db, run, plan_block, cs_disk):
        return pf
    monkeypatch.setattr(O, "_plan_fidelity_call", fake_pf)

    def _runner(review_count):
        run = SimpleNamespace(id="r", change_request_id="cr", selected_repo_ids=[],
                              attempts_json={"review": review_count})       # rounds = review_count + 1
        art = {"ctx": SimpleNamespace(selected_repo_ids=["r1"]), "intent": "x",
               "change_set": ChangeSet(operations=[], plan={})}
        return asyncio.run(O._phase_review(None, run, art, None))
    return _runner


_BEH_GAP = {"findings": [{"severity": "blocker", "kind": "missing_behavior", "item": "settle()",
                          "detail": "not wired"}], "has_gap": True, "missing_files": []}


def test_behavioural_gap_is_bounded_then_becomes_advisory(monkeypatch):
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(settings, "agentic_behavioral_gap_advisory", True)
    monkeypatch.setattr(settings, "agentic_max_behavioral_rounds", 2)
    review = _review_with(monkeypatch, reviewer_blocking=False, pf=_BEH_GAP)
    assert review(1) is True     # round 2 (<= cap) → behavioural gap still loops (one bounded fix attempt)
    assert review(2) is False    # round 3 (> cap)  → advisory; run proceeds to the human approval gate
    assert any(k == "plan_fidelity_advisory" for k, _ in events)   # escalation is surfaced, not silent


def test_real_reviewer_blocker_always_loops_even_past_cap(monkeypatch):
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(settings, "agentic_behavioral_gap_advisory", True)
    monkeypatch.setattr(settings, "agentic_max_behavioral_rounds", 2)
    review = _review_with(monkeypatch, reviewer_blocking=True, pf=_BEH_GAP)
    assert review(9) is True     # a real reviewer blocker is never demoted — loops regardless of round


def test_behavioural_gap_legacy_loops_when_flag_off(monkeypatch):
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(settings, "agentic_behavioral_gap_advisory", False)    # legacy behaviour
    review = _review_with(monkeypatch, reviewer_blocking=False, pf=_BEH_GAP)
    assert review(9) is True     # flag off → loops on any behavioural gap, as before


# ── R5+R2: converge-or-escalate by finding IDENTITY (a STUCK blocker, not a raw count) ──

def _review_run(monkeypatch, *, blocker_file, ledger_keys):
    monkeypatch.setattr(settings, "agentic_parallel_review", False)
    monkeypatch.setattr(settings, "agentic_behavioral_gap_advisory", True)
    events = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))

    def _f():  # a sensitive-category finding is must-block regardless of severity (is_must_block)
        return SimpleNamespace(blocking=True, severity="warning", category="security",
                               why="sensitive", suggested_fix="fix", file=blocker_file, line=1)
    async def fake_review(db, **kw):
        return SimpleNamespace(findings=[_f()], blocking=True, reviewer_model="claude")
    monkeypatch.setattr(O.agentic_review, "run_review", fake_review)
    async def fake_pf(db, run, plan_block, cs_disk):
        return {"findings": [], "has_gap": False, "missing_files": []}
    monkeypatch.setattr(O, "_plan_fidelity_call", fake_pf)

    run = SimpleNamespace(id="r", change_request_id="cr", selected_repo_ids=[],
                          attempts_json={"review": 3}, progress_ledger_json={"blocker_keys": list(ledger_keys)})
    art = {"ctx": SimpleNamespace(selected_repo_ids=["r1"]), "intent": "x",
           "change_set": ChangeSet(operations=[], plan={})}
    blocking = asyncio.run(O._phase_review(None, run, art, None))
    return blocking, art["review"], events


def test_same_blocker_stuck_across_rounds_escalates(monkeypatch):
    monkeypatch.setattr(settings, "agentic_max_stall_rounds", 2)
    # the SAME blocker (A.java/security) was open 2 rounds ago and is still open now → stuck → escalate
    blocking, review, events = _review_run(monkeypatch, blocker_file="A.java",
                                           ledger_keys=[["a.java|security"], ["a.java|security"]])
    assert blocking is False and review["escalated"] is True          # stop looping → human gate
    assert any(k == "review_stalled" for k, _ in events)
    assert review["has_blocker"] is True                              # blockers stay flagged for the human


def test_distinct_new_blockers_keep_looping(monkeypatch):
    monkeypatch.setattr(settings, "agentic_max_stall_rounds", 2)
    # a DIFFERENT blocker each round (was x/y, now z) → not the SAME stuck one → progress, keep going.
    # This is R2's precision win: a raw count (1→1→1) would have escalated here prematurely.
    blocking, review, events = _review_run(monkeypatch, blocker_file="Z.java",
                                           ledger_keys=[["x.java|security"], ["y.java|security"]])
    assert blocking is True and review["escalated"] is False
    assert not any(k == "review_stalled" for k, _ in events)


def test_stall_escalation_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(settings, "agentic_max_stall_rounds", 0)
    blocking, review, _ = _review_run(monkeypatch, blocker_file="A.java",
                                      ledger_keys=[["a.java|security"], ["a.java|security"]])
    assert blocking is True and review["escalated"] is False          # disabled → rely on absolute caps


def test_blocker_appearing_midwindow_still_escalates(monkeypatch):
    monkeypatch.setattr(settings, "agentic_max_stall_rounds", 2)
    # round 1 had NO blockers; A.java/security is open in rounds 2 AND 3 → stuck 2 consecutive rounds.
    # Guards the stall-window fix: the old `window[0]` baseline (the empty round) would have MISSED this.
    blocking, review, events = _review_run(monkeypatch, blocker_file="A.java",
                                           ledger_keys=[[], ["a.java|security"]])
    assert blocking is False and review["escalated"] is True
    assert any(k == "review_stalled" for k, _ in events)


# ── R6: the reviewer is anchored to the ratified acceptance criteria, as a FLOOR ──

def test_reviewer_gets_acceptance_criteria_as_floor(monkeypatch):
    monkeypatch.setattr(settings, "agentic_parallel_review", False)
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(O, "_success_criteria", lambda db, cr: "C1: field optional\nC2: max length 50")
    captured = {}
    async def fake_review(db, **kw):
        captured["plan_block"] = kw.get("plan_block", "")
        return SimpleNamespace(findings=[], blocking=False, reviewer_model="claude")
    monkeypatch.setattr(O.agentic_review, "run_review", fake_review)
    async def fake_pf(db, run, plan_block, cs_disk):
        return {"findings": [], "has_gap": False, "missing_files": []}
    monkeypatch.setattr(O, "_plan_fidelity_call", fake_pf)
    run = SimpleNamespace(id="r", change_request_id="cr", selected_repo_ids=[], attempts_json={})
    art = {"ctx": SimpleNamespace(selected_repo_ids=["r1"]), "intent": "x",
           "change_set": ChangeSet(operations=[], plan={})}
    asyncio.run(O._phase_review(None, run, art, None))
    assert "ACCEPTANCE CRITERIA" in captured["plan_block"] and "C2: max length 50" in captured["plan_block"]
    assert "FLOOR, not the ceiling" in captured["plan_block"]         # framed so it doesn't narrow the reviewer


# ── R3: the blast-radius directive is SEMANTIC (fix the defect), not a blind text-replace ──

def test_r3_blast_radius_directive_is_semantic_not_textual():
    from app.agents.agentic_subagents import _COMPLETENESS
    assert "DEFECT-CLASS BLAST RADIUS" in _COMPLETENESS
    assert "do NOT blindly text-replace" in _COMPLETENESS             # fix the defect, not the string
    assert "LEAVE UNTOUCHED" in _COMPLETENESS                         # safe/correct sites are left alone
    assert "STAY WITHIN this change's intended scope" in _COMPLETENESS  # no scope creep beyond intent
