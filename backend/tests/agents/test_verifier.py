# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pluggable verification backend + graceful degrade + structured errors (§9).

The build toolchain must NEVER be a hard dependency: when it's absent the gate
returns `unverified` (deferred to CI), not a crash. These tests pin that contract
plus the file:line error formatting the agent + retry feedback rely on.
"""
from types import SimpleNamespace

import pytest

from app.agents import verifier as V
from app.agents import agentic_tools as T
from app.agents.verification_plan import (
    VerificationOutcome, VerificationStep, StepResult, format_errors,
)


# ── backend selection ─────────────────────────────────────────────────────────

def test_select_verifier_forced_modes():
    assert V.select_verifier(mode="local").name == "local"
    assert V.select_verifier(mode="ci").name == "deferred"
    assert V.select_verifier(mode="off").name == "deferred"


def test_select_verifier_auto_degrades_on_missing_toolchain():
    assert V.select_verifier(report=SimpleNamespace(build_ready=True), mode="auto").name == "local"
    assert V.select_verifier(report=SimpleNamespace(build_ready=False), mode="auto").name == "deferred"


def test_deferred_is_unverified_never_failure():
    out = V.DeferredVerifier("no toolchain here").verify(None, "run-1", SimpleNamespace(operations=[]))
    assert out.status == "unverified"          # NOT needs_fix, NOT a crash
    assert "toolchain" in out.reason


def test_local_verifier_empty_change_is_verified(monkeypatch):
    # Nothing buildable changed → nothing to fail.
    monkeypatch.setattr(V.verification_plan, "build_plan", lambda db, rid, cs, **kw: ([], set()))
    out = V.LocalToolchainVerifier().verify(None, "run-1", SimpleNamespace(operations=[]))
    assert out.status == "verified"


# ── structured error formatting ────────────────────────────────────────────────

def test_format_errors_parses_file_line():
    step = VerificationStep("compile", "r1", ["mvn", "compile"])
    mvn_out = ("[INFO] Building network-core 2.0.0\n"
               "[ERROR] /ws/r1/src/main/java/Foo.java:[42,13] cannot find symbol\n"
               "[INFO] BUILD FAILURE\n")
    sr = StepResult(step, exit_code=1, timed_out=False, gate_pass=False, output_tail=mvn_out)
    outcome = VerificationOutcome(status="needs_fix", gates={"compile": False}, rounds=[sr])
    errs = format_errors(outcome)
    assert any("Foo.java:42" in e and "cannot find symbol" in e for e in errs)


def test_format_errors_surfaces_nondiagnostic_failure():
    # An opaque, unrecognised failure (no compile diagnostic, no known infra
    # signature) must still be surfaced with its exit code and tail — never lost.
    # Since the 2026-07-20 fix the gate's own argv comes FIRST (reproducibility),
    # then the exit/tail line.
    step = VerificationStep("install", "r1", ["mvn", "install"])
    sr = StepResult(step, exit_code=1, timed_out=False, gate_pass=False,
                    output_tail="org.apache.maven.plugin.MojoExecutionException: boom")
    outcome = VerificationOutcome(status="needs_fix", gates={"compile": False}, rounds=[sr])
    errs = format_errors(outcome)
    assert errs and any("exit 1" in e and "boom" in e for e in errs)
    assert any("install command: $ mvn install" in e for e in errs)


def test_format_errors_timeout_labeled_infra_not_code():
    # A timed-out step must NEVER read as a code defect (or worse, as an empty
    # error list): the agent would spend fix rounds editing code to fix time.
    step = VerificationStep("test", "r1", ["mvn", "-o", "test", "-Dtest=FooTest"])
    sr = StepResult(step, exit_code=None, timed_out=True, gate_pass=False,
                    output_tail="[INFO] Running FooTest\n")
    outcome = VerificationOutcome(status="needs_fix",
                                  gates={"required_tests": False, "timeout": False}, rounds=[sr])
    errs = format_errors(outcome)
    assert errs, "a timed-out step must never produce an empty error list"
    assert "TIMED OUT" in errs[0] and "NOT a code defect" in errs[0]
    assert "mvn -o test -Dtest=FooTest" in errs[0]          # reproducible argv


def test_diagnostics_parsed_from_full_output_survive_display_clip():
    # A --fail-at-end reactor log keeps its inline compiler errors in the MIDDLE;
    # the 2K+2K display clip drops exactly that region. Diagnostics must be parsed
    # from the full output at capture time, not re-parsed from the clipped tail.
    from app.agents.verification_plan import _diagnostic_lines, format_step_errors
    step = VerificationStep("install", "r1", ["mvn", "-fae", "install"])
    full = ("[INFO] head padding\n" * 200
            + "[ERROR] /ws/r1/src/main/java/Mid.java:[42,17] cannot find symbol\n"
            + "[INFO] tail padding\n" * 200)
    assert len(full) > 4000
    tail = full[:2000] + "\n…[truncated]…\n" + full[-2000:]      # run_plan's display clip
    assert "Mid.java" not in tail                                 # the clip really drops it
    sr = StepResult(step, exit_code=1, timed_out=False, gate_pass=False, output_tail=tail,
                    diagnostics=_diagnostic_lines(full, step))
    errs = format_step_errors(sr)
    assert any("Mid.java:42" in e and "cannot find symbol" in e for e in errs)


def test_format_errors_exit_none_runner_failure_is_surfaced():
    # exit_code=None without a timeout = the step runner itself failed (killed
    # process / toolchain-guard refusal). Previously the fallback guard skipped
    # this case entirely → "Verification failed — 0 error(s)".
    step = VerificationStep("compile", "r1", ["mvn", "compile"])
    sr = StepResult(step, exit_code=None, timed_out=False, gate_pass=False,
                    output_tail="CommandNotAllowed: 'mvn' blocked by toolchain guard")
    outcome = VerificationOutcome(status="needs_fix", gates={"compile": False}, rounds=[sr])
    errs = format_errors(outcome)
    assert errs and "no exit code" in errs[0] and "not a code defect" in errs[0]
    assert "CommandNotAllowed" in errs[0]


# ── verify_change tool (diagnostic, degrades gracefully) ─────────────────────────

def test_verify_change_no_changes_yet():
    ctx = T.RunContext(run_id="r", selected_repo_ids=["repo-1"])
    assert "no changes yet" in T.verify_change(ctx)


def test_verify_change_reports_structured_errors(monkeypatch):
    ctx = T.RunContext(run_id="r", selected_repo_ids=["repo-1"])
    ctx.file_ops[("repo-1", "A.java")] = T.FileOp(op="modify", repo_id="repo-1", path="A.java",
                                                  content="x", content_hash=None)
    step = VerificationStep("compile", "repo-1", ["mvn", "compile"])
    sr = StepResult(step, exit_code=1, timed_out=False, gate_pass=False,
                    output_tail="[ERROR] /ws/A.java:[3,1] bad symbol")

    class _Fake:
        name = "local"
        def verify(self, db, run_id, cs):
            return VerificationOutcome("needs_fix", {"compile": False}, [sr])

    monkeypatch.setattr(V, "select_verifier", lambda *a, **k: _Fake())
    out = T.verify_change(ctx)
    assert "needs_fix" in out and "A.java:3" in out


def test_verify_change_no_diagnostics_hint_names_the_failing_gate(monkeypatch):
    # When nothing parses, the old hint prescribed `mvn compile` — a DIFFERENT goal
    # that can pass while the scoped gate fails (the Surefire incident shape). The
    # hint must point at the failing gate's own command and carry the verifier reason.
    ctx = T.RunContext(run_id="r", selected_repo_ids=["repo-1"])
    ctx.file_ops[("repo-1", "A.java")] = T.FileOp(op="modify", repo_id="repo-1", path="A.java",
                                                  content="x", content_hash=None)

    class _Fake:
        name = "local"
        def verify(self, db, run_id, cs):
            return VerificationOutcome("needs_fix", {"required_tests": False}, [],
                                       reason="surefire gate failed")

    monkeypatch.setattr(V, "select_verifier", lambda *a, **k: _Fake())
    out = T.verify_change(ctx)
    assert "needs_fix" in out
    assert "run_command 'mvn compile'" not in out  # the old wrong repro prescription is gone
    assert "surefire gate failed" in out           # verifier reason surfaced
    assert "FAILING gate" in out


def test_verify_change_verifier_crash_is_not_deferred_to_ci(monkeypatch):
    # "unverified" covers two different worlds: a deliberate no-toolchain deferral
    # (CI covers it) and the verifier itself crashing. The crash must NOT tell the
    # agent to stop self-verifying.
    ctx = T.RunContext(run_id="r", selected_repo_ids=["repo-1"])
    ctx.file_ops[("repo-1", "A.java")] = T.FileOp(op="modify", repo_id="repo-1", path="A.java",
                                                  content="x", content_hash=None)
    monkeypatch.setattr(T, "_disk_ops", lambda c: ([], []))

    class _Fake:
        name = "local"
        def verify(self, db, run_id, cs):
            return VerificationOutcome("unverified", {}, [], reason="no space left on device")

    monkeypatch.setattr(V, "select_verifier", lambda *a, **k: _Fake())
    out = T.verify_change(ctx)
    assert "FAILED to run" in out and "NOT a pass" in out
    assert "CI after approval" not in out


def test_verify_change_toolchain_deferral_still_reads_as_ci(monkeypatch):
    ctx = T.RunContext(run_id="r", selected_repo_ids=["repo-1"])
    ctx.file_ops[("repo-1", "A.java")] = T.FileOp(op="modify", repo_id="repo-1", path="A.java",
                                                  content="x", content_hash=None)
    monkeypatch.setattr(T, "_disk_ops", lambda c: ([], []))

    class _Fake:
        name = "deferred"
        def verify(self, db, run_id, cs):
            return VerificationOutcome("unverified", {}, [], reason="no toolchain here")

    monkeypatch.setattr(V, "select_verifier", lambda *a, **k: _Fake())
    out = T.verify_change(ctx)
    assert "CI after approval" in out and "FAILED to run" not in out


def test_verify_change_partial_workspace_read_is_loud(monkeypatch):
    # A repo whose workspace enumeration failed is EXCLUDED from the build — even a
    # green verdict must say so, or the agent trusts a verify that never saw the repo.
    ctx = T.RunContext(run_id="r", selected_repo_ids=["repo-1", "bad"])
    ctx.file_ops[("repo-1", "A.java")] = T.FileOp(op="modify", repo_id="repo-1", path="A.java",
                                                  content="x", content_hash=None)
    monkeypatch.setattr(T, "_disk_ops", lambda c: ([], ["bad"]))

    class _Fake:
        name = "local"
        def verify(self, db, run_id, cs):
            return VerificationOutcome("verified", {"compile": True}, [])

    monkeypatch.setattr(V, "select_verifier", lambda *a, **k: _Fake())
    out = T.verify_change(ctx)
    assert "✅" in out
    assert "bad" in out and "EXCLUDES" in out


def test_code_search_semantic_tool(monkeypatch):
    ctx = T.RunContext(run_id="r", selected_repo_ids=["repo1"], db=object())
    monkeypatch.setattr("app.rag.retrieval.retrieve",
                        lambda *a, **k: [          # accept any signature (prod added use_reranker=)
                            {"source_file": "svc/PayService.java", "symbol_name": "validate",
                             "content": "class PayService { void validate(){} }"}])
    out = T.code_search_semantic(ctx, "where are payments validated")
    assert "PayService.java" in out and "validate" in out


def test_verify_change_degrades_to_ci(monkeypatch):
    ctx = T.RunContext(run_id="r", selected_repo_ids=["repo-1"])
    ctx.file_ops[("repo-1", "A.java")] = T.FileOp(op="modify", repo_id="repo-1", path="A.java",
                                                  content="x", content_hash=None)
    monkeypatch.setattr(V, "select_verifier", lambda *a, **k: V.DeferredVerifier("no local toolchain"))
    out = T.verify_change(ctx)
    assert "verified by CI" in out and "unavailable" in out
