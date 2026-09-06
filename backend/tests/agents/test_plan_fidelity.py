# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Plan-fidelity gate: deterministic file-coverage + LLM behavioural/faked-logic check. The
headline test (`test_audit_run_would_have_been_caught`) replays the real Test1 run's gaps to prove
the gate flags what the adversarial reviewer missed."""
import asyncio

from app.agents import plan_fidelity as PF
from app.agents.plan_fidelity import file_coverage, behavioral_coverage, check_plan_fidelity


# ── Deterministic file coverage ────────────────────────────────────────────────────
def test_unhedged_missing_file_is_blocker():
    # A planned file with a SUBSTANTIVE, un-hedged intent that the diff never touched is a DROPPED
    # deliverable → blocker (drives the self-heal loop-back; escalates to a human only if it stays
    # unresolved). This is the fix for the false-green where load-bearing files vanished silently.
    planned = [{"path": "transaction-processor/src/main/java/com/example/tpu/service/kafka/SinkService.java",
                "intent": "Emit a HELD/auto-decline signal so Risk has visibility."}]
    touched = ["transaction-processor/src/main/java/com/example/tpu/service/velocity/VelocityCheckService.java"]
    f = file_coverage(planned, touched)
    assert len(f) == 1 and f[0]["severity"] == "blocker" and f[0]["kind"] == "missing_file"
    assert "SinkService.java" in f[0]["item"]


def test_no_intent_missing_file_is_warning_not_blocker():
    # A terse plan entry (no intent text) must NOT hard-block — otherwise a reuse-no-edit file the
    # agent reasonably didn't touch traps the run in a review<->code loop (the real-world bug).
    planned = [{"path": "x/y/ApiMessageAssembler.java"}]      # no 'intent' key
    f = file_coverage(planned, touched_files=[])
    assert len(f) == 1 and f[0]["severity"] == "warning"


def test_optional_intent_downgrades_to_warning():
    planned = [{"path": "x/y/ApiMessageAssembler.java", "intent": "No signature change; may need no edit."},
               {"path": "x/y/ConfigParamService.java", "intent": "Optional: add typed getters. Confirm in Phase B."}]
    f = file_coverage(planned, touched_files=[])
    assert {x["severity"] for x in f} == {"warning"}


def test_touched_file_matches_despite_repo_prefix():
    # plan uses the full repo path; the diff reports a shorter path — suffix match must NOT flag it.
    planned = [{"path": "transaction-processor/src/main/java/com/example/tpu/service/impl/Foo.java", "intent": "edit"}]
    touched = ["com/example/tpu/service/impl/Foo.java"]
    assert file_coverage(planned, touched) == []


def test_pseudo_path_without_real_file_is_skipped():
    # "dataaccessor (config_param seed / DDL)" is not a concrete source file → behavioural check owns it.
    planned = [{"path": "dataaccessor (config_param seed / DDL)", "intent": "Seed VELOCITY_* rows."}]
    assert file_coverage(planned, touched_files=[]) == []


def test_accepts_file_key_not_just_path():
    # REGRESSION (Test 2): the plan's per_file_changes used "file", not "path" — coverage must
    # still flag a missing required file instead of silently no-op'ing. Substantive un-hedged
    # intent ("config holder", a required ADD) → blocker.
    planned = [{"file": "x/y/DuplicateGuardProperties.java", "action": "ADD",
                "intent": "config holder"}]
    f = file_coverage(planned, touched_files=["x/y/Other.java"])
    assert len(f) == 1 and f[0]["severity"] == "blocker"
    assert "DuplicateGuardProperties.java" in f[0]["item"]


def test_dedup_same_file_listed_twice():
    planned = [{"path": "a/b/Foo.java", "intent": "x"}, {"path": "a/b/Foo.java", "intent": "y"}]
    assert len(file_coverage(planned, [])) == 1


def test_file_coverage_tolerates_garbage_input():
    assert file_coverage(None, None) == []
    assert file_coverage([{"no_path": 1}, "notadict", {"path": ""}], []) == []


# ── LLM behavioural / faked-logic check (mocked) ───────────────────────────────────
def _patch_llm(monkeypatch, text):
    async def _f(**kw): return text
    monkeypatch.setattr(PF, "call_llm", _f)


def test_behavioral_flags_missing_and_faked(monkeypatch):
    _patch_llm(monkeypatch, """{"findings": [
        {"severity": "blocker", "kind": "missing_behavior", "item": "Risk visibility",
         "detail": "plan wanted SinkService emission; diff only logs"},
        {"severity": "blocker", "kind": "faked_logic", "item": "per-user baseline",
         "detail": "baseline counter incremented by the same request; degenerate"}
    ]}""")
    r = asyncio.run(behavioral_coverage(plan_text="...", success_criteria="Risk sees triggers",
                                        diff_summary="diff..."))
    assert {x["kind"] for x in r} == {"missing_behavior", "faked_logic"}
    assert all(x["severity"] == "blocker" for x in r)


def test_behavioral_empty_when_covered(monkeypatch):
    _patch_llm(monkeypatch, '{"findings": []}')
    assert asyncio.run(behavioral_coverage(plan_text="p", success_criteria="s", diff_summary="d")) == []


def test_behavioral_surfaces_gate_error_on_llm_failure(monkeypatch):
    # Infrastructure failure must NOT read as "clean": [] is the good-case verdict, so an
    # errored gate returns a VISIBLE non-blocking gate_error finding instead (2026-08:
    # was a silent fail-open []).
    async def _boom(**kw): raise RuntimeError("llm down")
    monkeypatch.setattr(PF, "call_llm", _boom)
    r = asyncio.run(behavioral_coverage(plan_text="p", success_criteria="s", diff_summary="d"))
    assert len(r) == 1 and r[0]["kind"] == "gate_error" and r[0]["severity"] == "warning"


def test_behavioral_skips_when_no_diff(monkeypatch):
    # No diff to judge → no LLM call, empty result.
    called = {"n": 0}
    async def _f(**kw): called["n"] += 1; return "{}"
    monkeypatch.setattr(PF, "call_llm", _f)
    assert asyncio.run(behavioral_coverage(plan_text="p", success_criteria="s", diff_summary="")) == []
    assert called["n"] == 0


def test_behavioral_downgrades_unknown_severity(monkeypatch):
    _patch_llm(monkeypatch, '{"findings": [{"severity": "critical", "kind": "missing_behavior", "item": "X", "detail": "d"}]}')
    r = asyncio.run(behavioral_coverage(plan_text="p", success_criteria="s", diff_summary="d"))
    assert r[0]["severity"] == "warning"


# ── Combined gate — replay the real audited run ────────────────────────────────────
def test_audit_run_would_have_been_caught(monkeypatch):
    """The Test1 velocity run: plan named SinkService (Risk visibility), VelocityCounterStore, and a
    config seed; the code shipped 6 files WITHOUT the first two, and faked the per-user baseline. The
    adversarial reviewer missed all of it. The plan-fidelity gate must flag it as a blocking gap."""
    planned = [
        {"path": ".../service/velocity/VelocityCheckService.java", "intent": "NEW core service"},
        {"path": ".../service/velocity/VelocityCounterStore.java", "intent": "NEW Redis counter"},
        {"path": ".../service/kafka/SinkService.java", "intent": "Emit HELD/decline so Risk has visibility"},
        {"path": ".../handlers/impl/ReqTransferStageHandler.java", "intent": "call velocity check at INTIAL_REQPAY"},
        {"path": ".../pojos/TransactionStages.java", "intent": "add VELOCITY_HELD"},
        {"path": "dataaccessor (config_param seed / DDL)", "intent": "Seed VELOCITY_* rows"},
    ]
    touched = [  # the 6 files the run actually changed (NO VelocityCounterStore, NO SinkService)
        ".../service/velocity/VelocityCheckService.java",
        ".../handlers/impl/ReqTransferStageHandler.java",
        ".../pojos/TransactionStages.java",
        ".../events/listeners/TimeOutEventListener.java",
        ".../service/scheduler/InMemoryScheduler.java",
        ".../service/h2/ConfigParamService.java",
    ]
    # The behavioural LLM (mocked) flags the faked baseline + dropped Risk visibility.
    _patch_llm(monkeypatch, """{"findings": [
        {"severity": "blocker", "kind": "faked_logic", "item": "per-user baseline",
         "detail": "single cumulative counter / fixed factor; degenerates to platform-only"},
        {"severity": "blocker", "kind": "missing_behavior", "item": "Risk visibility",
         "detail": "SinkService never emitted; only logs"}
    ]}""")
    r = asyncio.run(check_plan_fidelity(
        plan_text="velocity hold...", success_criteria="Risk team can see when it triggers",
        planned_files=planned, touched_files=touched, diff_summary="<the 6-file diff>"))
    assert r["has_gap"] is True                                   # the run would be BLOCKED to fix
    items = {f["item"] for f in r["findings"]}
    assert any("VelocityCounterStore.java" in i for i in items)  # deterministic: dropped file
    assert any("SinkService.java" in i for i in items)           # deterministic: dropped Risk-visibility file
    assert "per-user baseline" in items                          # behavioural: faked logic
    assert any("SinkService.java" in m for m in r["missing_files"])


def test_clean_run_has_no_gap(monkeypatch):
    _patch_llm(monkeypatch, '{"findings": []}')
    planned = [{"path": "a/b/Foo.java", "intent": "edit"}]
    r = asyncio.run(check_plan_fidelity(plan_text="p", success_criteria="s",
                                        planned_files=planned, touched_files=["a/b/Foo.java"],
                                        diff_summary="diff"))
    assert r["has_gap"] is False and r["findings"] == []


# ── corroborate: two evidence tiers (added symbols vs touched file-stems) ──────────
def _blocker(detail, item="gap"):
    return {"severity": "blocker", "kind": "missing_behavior", "item": item, "detail": detail}


def test_corroborate_keeps_blocker_for_member_missing_from_edited_file():
    # THE M2 GUARD: "validateSplit not added to ReqTransferValidator" must STAY a blocker even though the
    # container file ReqTransferValidator.java was touched (its stem is in touched_stems). The container
    # existing does not prove the specific member was added.
    findings = [_blocker("Plan required method validateSplit on ReqTransferValidator; the diff does not add it.")]
    out = PF.corroborate(findings, added_syms=set(), touched_stems={"ReqTransferValidator"})
    assert out[0]["severity"] == "blocker" and out[0]["kind"] == "missing_behavior"


def test_corroborate_downgrades_when_member_is_actually_added():
    # Same finding, but validateSplit IS among the genuinely-added symbols → downgrade to advisory.
    findings = [_blocker("Plan required method validateSplit on ReqTransferValidator; the diff does not add it.")]
    out = PF.corroborate(findings, added_syms={"validateSplit"}, touched_stems={"ReqTransferValidator"})
    assert out[0]["severity"] == "warning" and out[0]["kind"] == "missing_behavior_refuted"


def test_corroborate_downgrades_whole_class_missing_via_file_stem():
    # The legitimate Test-8 phantom: "service SplitPayOrchestrationService is missing" while a file
    # named SplitPayOrchestrationService.java is in the change-set → still downgraded (no unaccounted
    # member token; the class name is uppercase-initial).
    findings = [_blocker("The SplitPayOrchestrationService is missing from the change entirely.")]
    out = PF.corroborate(findings, added_syms=set(), touched_stems={"SplitPayOrchestrationService"})
    assert out[0]["severity"] == "warning" and out[0]["kind"] == "missing_behavior_refuted"


def test_corroborate_never_downgrades_security_or_partial_claims():
    sec = [_blocker("The auth token is never forwarded to the beneficiary leg.")]
    assert PF.corroborate(sec, added_syms={"token"}, touched_stems={"AuthService"})[0]["severity"] == "blocker"
    partial = [_blocker("setAmount is missing on the debit leg (credit leg is fine).")]
    assert PF.corroborate(partial, added_syms={"setAmount"}, touched_stems=set())[0]["severity"] == "blocker"
