# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 15 — self-correction loop orchestrator + LLM fix helper.

Pure — no Docker, no real LLM. Sandbox and LLM are dependency-injected
callables, so the orchestrator is fully testable in-process.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.agents import self_correction


# ──────────────────────────────────────────────────────────────────────────────
# Test fixtures — minimal SandboxResult stand-in
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FakeSandboxResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


def _make_sandbox(results: list[FakeSandboxResult]):
    """Build a sandbox callable that returns successive results from the list.

    Records the number of invocations in `calls` for assertion.
    """
    calls = {"n": 0, "last_files": None}

    def _runner(files: dict[str, str]):
        calls["last_files"] = dict(files)
        idx = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return results[idx]

    return _runner, calls


# ──────────────────────────────────────────────────────────────────────────────
# self_correct — orchestrator behaviour
# ──────────────────────────────────────────────────────────────────────────────

class TestSelfCorrectOrchestrator:
    @pytest.mark.asyncio
    async def test_clean_compile_first_try(self):
        """exit_code=0 at iteration 0 → success, no fixes invoked."""
        sandbox, sandbox_calls = _make_sandbox([FakeSandboxResult(exit_code=0)])
        fix_calls = {"n": 0}

        async def no_fix(code, err):
            fix_calls["n"] += 1
            return code

        result = await self_correction.self_correct(
            {"Main.java": "class M{}"},
            generate_fix=no_fix, run_sandbox=sandbox, max_iterations=3,
        )

        assert result.success is True
        assert result.iterations == 0
        assert result.final_stderr == ""
        assert len(result.attempts) == 1
        assert result.attempts[0]["exit_code"] == 0
        assert sandbox_calls["n"] == 1
        assert fix_calls["n"] == 0

    @pytest.mark.asyncio
    async def test_fails_once_then_succeeds(self):
        """First compile fails; fix produces new code; second compile succeeds."""
        sandbox, sandbox_calls = _make_sandbox([
            FakeSandboxResult(exit_code=1, stderr="Main.java: cannot find symbol 'x'"),
            FakeSandboxResult(exit_code=0),
        ])
        fix_calls = {"n": 0, "received_stderr": None}

        async def fix(code, err):
            fix_calls["n"] += 1
            fix_calls["received_stderr"] = err
            return {"Main.java": "class M{int x;}"}

        result = await self_correction.self_correct(
            {"Main.java": "class M{}"},
            generate_fix=fix, run_sandbox=sandbox, max_iterations=3,
        )

        assert result.success is True
        assert result.iterations == 1
        assert result.final_code == {"Main.java": "class M{int x;}"}
        assert fix_calls["n"] == 1
        assert "cannot find symbol" in fix_calls["received_stderr"]
        assert sandbox_calls["n"] == 2
        assert len(result.attempts) == 2

    @pytest.mark.asyncio
    async def test_all_iterations_fail(self):
        """With max_iterations=3, sandbox called 4 times; all fail → success=False."""
        sandbox, sandbox_calls = _make_sandbox([
            FakeSandboxResult(exit_code=1, stderr="error 1"),
            FakeSandboxResult(exit_code=1, stderr="error 2"),
            FakeSandboxResult(exit_code=1, stderr="error 3"),
            FakeSandboxResult(exit_code=1, stderr="final error after all fixes"),
        ])
        fix_calls = {"n": 0}

        async def fix(code, err):
            fix_calls["n"] += 1
            return {**code, "Fix.java": f"// attempt {fix_calls['n']}"}

        result = await self_correction.self_correct(
            {"Main.java": "class M{}"},
            generate_fix=fix, run_sandbox=sandbox, max_iterations=3,
        )

        assert result.success is False
        assert result.iterations == 3
        assert "final error after all fixes" in result.final_stderr
        # 1 initial + 3 fix attempts = 4 sandbox calls, 3 fix calls.
        assert sandbox_calls["n"] == 4
        assert fix_calls["n"] == 3
        assert len(result.attempts) == 4

    @pytest.mark.asyncio
    async def test_loop_cap_honored_with_max_0(self):
        """max_iterations=0 → single compile, no fix attempt."""
        sandbox, sandbox_calls = _make_sandbox([
            FakeSandboxResult(exit_code=1, stderr="initial error"),
        ])
        fix_calls = {"n": 0}

        async def fix(code, err):
            fix_calls["n"] += 1
            return code

        result = await self_correction.self_correct(
            {"x.java": "x"}, generate_fix=fix, run_sandbox=sandbox, max_iterations=0,
        )

        assert result.success is False
        assert sandbox_calls["n"] == 1
        assert fix_calls["n"] == 0
        assert "initial error" in result.final_stderr

    @pytest.mark.asyncio
    async def test_fix_generator_exception_stops_loop_gracefully(self):
        sandbox, _ = _make_sandbox([
            FakeSandboxResult(exit_code=1, stderr="compile error"),
        ])

        async def broken_fix(code, err):
            raise RuntimeError("LLM crashed")

        result = await self_correction.self_correct(
            {"x.java": "x"}, generate_fix=broken_fix, run_sandbox=sandbox, max_iterations=3,
        )

        assert result.success is False
        assert "LLM crashed" in result.final_stderr
        assert result.iterations == 0

    @pytest.mark.asyncio
    async def test_empty_fix_dict_stops_loop(self):
        """Fix generator returning {} is the 'give up' signal — loop stops."""
        sandbox, sandbox_calls = _make_sandbox([
            FakeSandboxResult(exit_code=1, stderr="err"),
        ])
        fix_calls = {"n": 0}

        async def empty_fix(code, err):
            fix_calls["n"] += 1
            return {}

        result = await self_correction.self_correct(
            {"x.java": "x"}, generate_fix=empty_fix, run_sandbox=sandbox, max_iterations=3,
        )

        assert result.success is False
        assert "empty" in result.final_stderr.lower()
        assert sandbox_calls["n"] == 1   # only the initial compile
        assert fix_calls["n"] == 1

    @pytest.mark.asyncio
    async def test_sandbox_callable_exception_captured(self):
        """A raising sandbox doesn't propagate — captured into the result."""
        def _boom(files):
            raise ConnectionError("docker socket closed")

        async def fix(code, err):
            return code

        result = await self_correction.self_correct(
            {"x.java": "x"}, generate_fix=fix, run_sandbox=_boom, max_iterations=3,
        )

        assert result.success is False
        assert "docker socket closed" in result.final_stderr

    @pytest.mark.asyncio
    async def test_does_not_mutate_caller_dict(self):
        """Orchestrator must not mutate the caller-supplied initial_code dict."""
        sandbox, _ = _make_sandbox([
            FakeSandboxResult(exit_code=1, stderr="err"),
            FakeSandboxResult(exit_code=0),
        ])

        async def fix(code, err):
            return {**code, "New.java": "// added"}

        initial = {"Main.java": "class M{}"}
        initial_copy = dict(initial)
        await self_correction.self_correct(
            initial, generate_fix=fix, run_sandbox=sandbox, max_iterations=2,
        )
        assert initial == initial_copy    # unchanged

    @pytest.mark.asyncio
    async def test_attempt_log_truncates_long_output(self):
        sandbox, _ = _make_sandbox([
            FakeSandboxResult(exit_code=0, stdout="x" * 10_000, stderr="y" * 10_000),
        ])

        async def fix(code, err):
            return code

        result = await self_correction.self_correct(
            {"x.java": "x"}, generate_fix=fix, run_sandbox=sandbox,
        )
        assert len(result.attempts[0]["stdout_excerpt"]) == 500
        assert len(result.attempts[0]["stderr_excerpt"]) == 500


# ──────────────────────────────────────────────────────────────────────────────
# generate_fix_via_llm — mocked LLM
# ──────────────────────────────────────────────────────────────────────────────

class TestGenerateFixViaLLM:
    @pytest.mark.asyncio
    async def test_returns_merged_files_on_success(self, monkeypatch):
        async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
            return json.dumps({
                "files": {
                    "Main.java": "class M { int x; }",   # modified
                    # Helper.java unchanged — LLM must NOT include it (rule in prompt)
                }
            })

        monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

        current = {
            "Main.java":   "class M {}",
            "Helper.java": "class H {}",
        }
        out = await self_correction.generate_fix_via_llm(
            current, "Main.java:1: error: cannot find symbol 'x'",
        )
        assert out["Main.java"]   == "class M { int x; }"
        assert out["Helper.java"] == "class H {}"   # unchanged file preserved

    @pytest.mark.asyncio
    async def test_empty_stderr_returns_empty(self, monkeypatch):
        called = {"n": 0}

        async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
            called["n"] += 1
            return "{}"

        monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
        assert await self_correction.generate_fix_via_llm({"x.java": "x"}, "") == {}
        assert called["n"] == 0   # short-circuit before LLM

    @pytest.mark.asyncio
    async def test_empty_code_returns_empty(self, monkeypatch):
        called = {"n": 0}

        async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
            called["n"] += 1
            return "{}"

        monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
        assert await self_correction.generate_fix_via_llm({}, "some error") == {}
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_llm_exception_returns_empty(self, monkeypatch):
        async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
            raise RuntimeError("LLM down")

        monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
        assert await self_correction.generate_fix_via_llm({"x.java": "x"}, "error") == {}

    @pytest.mark.asyncio
    async def test_non_json_response_returns_empty(self, monkeypatch):
        async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
            return "Looking at the error, I suggest adding `int x;` to Main.java."

        monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
        assert await self_correction.generate_fix_via_llm({"x.java": "x"}, "error") == {}

    @pytest.mark.asyncio
    async def test_missing_files_key_returns_empty(self, monkeypatch):
        async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
            return json.dumps({"not_files": {"x.java": "y"}})

        monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
        assert await self_correction.generate_fix_via_llm({"x.java": "x"}, "error") == {}

    @pytest.mark.asyncio
    async def test_unchanged_result_returns_empty(self, monkeypatch):
        """If LLM proposes files identical to current, that's 'give up'."""
        async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
            return json.dumps({"files": {"x.java": "original content"}})

        monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
        assert await self_correction.generate_fix_via_llm(
            {"x.java": "original content"}, "some error",
        ) == {}

    @pytest.mark.asyncio
    async def test_filters_unsafe_paths(self, monkeypatch):
        async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
            return json.dumps({
                "files": {
                    "/etc/passwd":   "pwned",     # absolute — drop
                    "../escape.sh":  "pwned",     # traversal — drop
                    "Main.java":     "class M{}", # OK
                }
            })

        monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

        out = await self_correction.generate_fix_via_llm(
            {"Main.java": "orig"}, "error",
        )
        assert "/etc/passwd"   not in out
        assert "../escape.sh"  not in out
        assert out["Main.java"] == "class M{}"

    @pytest.mark.asyncio
    async def test_filters_non_string_content(self, monkeypatch):
        async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
            return json.dumps({"files": {"x.java": 42}})   # non-string content

        monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)
        # After filtering the single proposed change, nothing actually changed.
        assert await self_correction.generate_fix_via_llm({"x.java": "orig"}, "err") == {}


# ──────────────────────────────────────────────────────────────────────────────
# Integration — orchestrator + default LLM fix helper
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orchestrator_with_default_fix_helper(monkeypatch):
    """Composition smoke test: default generate_fix_via_llm plugged into
    self_correct with a mocked sandbox. LLM returns a valid fix; loop succeeds
    on the second attempt."""
    async def fake_call_llm(system, messages, max_tokens=4000, **kwargs):
        return json.dumps({"files": {"Main.java": "class M { int x = 0; }"}})

    monkeypatch.setattr("app.core.llm.call_llm", fake_call_llm)

    sandbox, calls = _make_sandbox([
        FakeSandboxResult(exit_code=1, stderr="cannot find symbol 'x'"),
        FakeSandboxResult(exit_code=0),
    ])

    result = await self_correction.self_correct(
        {"Main.java": "class M {}"},
        generate_fix=self_correction.generate_fix_via_llm,
        run_sandbox=sandbox,
        max_iterations=3,
    )

    assert result.success is True
    assert result.iterations == 1
    assert result.final_code["Main.java"] == "class M { int x = 0; }"
    assert calls["n"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# _inloop_self_correct — orchestrator adapter (disk routing, degrade, restore)
# ──────────────────────────────────────────────────────────────────────────────

import asyncio
from types import SimpleNamespace

from app.agents import agentic_orchestrator as O
from app.core.config import settings as _settings


def _fake_verifier(name: str, statuses: list[str]):
    """Verifier stub: exposes `.name` and a `.verify()` returning successive statuses."""
    calls = {"n": 0}

    def verify(db, run_id, change_set, *, app_blast_radius=True):
        idx = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        return SimpleNamespace(status=statuses[idx], gates={}, reason="", module_results={})

    return SimpleNamespace(name=name, verify=verify), calls


def _setup_ws(monkeypatch, tmp_path, original: str):
    """Point repo_dir at tmp_path, seed one touched .java file, capture events."""
    repo = tmp_path / "repo-1"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "Main.java").write_text(original, encoding="utf-8")
    events: list = []
    op = SimpleNamespace(op="modify", repo_id="repo-1", path="Main.java", content=original)
    monkeypatch.setattr(O, "_ws_id", lambda run: "ws")
    monkeypatch.setattr(O.workspace_local, "repo_dir", lambda ws, rid: tmp_path / rid)
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(operations=[op]))
    monkeypatch.setattr(O, "emit_event",
                        lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(O.verification_plan, "format_errors", lambda outcome, **k: ["err"])
    monkeypatch.setattr(_settings, "agentic_progress_ledger", False)
    monkeypatch.setattr(_settings, "self_correction_max_iterations", 3)
    return repo, events


_DB = SimpleNamespace(add=lambda x: None)
_RUN = lambda: SimpleNamespace(id="r", selected_repo_ids=["repo-1"], progress_ledger_json=None)


def test_inloop_degrades_when_no_local_toolchain(monkeypatch, tmp_path):
    """No local mvn (verifier=deferred) → no-op, never compiles, files untouched."""
    repo, events = _setup_ws(monkeypatch, tmp_path, "ORIG")
    verifier, vcalls = _fake_verifier("deferred", ["unverified"])
    monkeypatch.setattr(O, "select_verifier", lambda *a, **k: verifier)
    asyncio.run(O._inloop_self_correct(_DB, _RUN(), {}))
    assert vcalls["n"] == 0                                  # never tried to compile
    assert (repo / "Main.java").read_text() == "ORIG"        # untouched
    assert not events                                        # nothing emitted


def test_inloop_clean_compile_is_noop(monkeypatch, tmp_path):
    """Agent's output already compiles → one compile, no fix, content unchanged."""
    repo, events = _setup_ws(monkeypatch, tmp_path, "ORIG")
    verifier, vcalls = _fake_verifier("local", ["verified"])
    monkeypatch.setattr(O, "select_verifier", lambda *a, **k: verifier)
    fix_calls = {"n": 0}

    async def fix(code, err):
        fix_calls["n"] += 1
        return {"repo-1/Main.java": "WRONG"}

    monkeypatch.setattr(self_correction, "generate_fix_via_llm", fix)
    asyncio.run(O._inloop_self_correct(_DB, _RUN(), {}))
    assert vcalls["n"] == 1 and fix_calls["n"] == 0
    assert (repo / "Main.java").read_text() == "ORIG"
    assert any(k == "self_correction" and p["success"] for k, p in events)


def test_inloop_fixes_then_succeeds(monkeypatch, tmp_path):
    """Compile fails once, fixer corrects it, second compile clean → fixed content kept."""
    repo, events = _setup_ws(monkeypatch, tmp_path, "ORIG")
    verifier, _ = _fake_verifier("local", ["needs_fix", "verified"])
    monkeypatch.setattr(O, "select_verifier", lambda *a, **k: verifier)

    async def fix(code, err):
        return {**code, "repo-1/Main.java": "FIXED"}

    monkeypatch.setattr(self_correction, "generate_fix_via_llm", fix)
    asyncio.run(O._inloop_self_correct(_DB, _RUN(), {}))
    assert (repo / "Main.java").read_text() == "FIXED"       # clean state kept on disk
    assert any(k == "self_correction" and p["success"] for k, p in events)


def test_inloop_restores_original_on_failure(monkeypatch, tmp_path):
    """Never reaches a clean compile → original edits restored (NON-DESTRUCTIVE)."""
    repo, events = _setup_ws(monkeypatch, tmp_path, "ORIG")
    verifier, vcalls = _fake_verifier("local", ["needs_fix"])     # always fails
    monkeypatch.setattr(O, "select_verifier", lambda *a, **k: verifier)

    async def fix(code, err):
        return {**code, "repo-1/Main.java": "BROKEN_ATTEMPT"}

    monkeypatch.setattr(self_correction, "generate_fix_via_llm", fix)
    asyncio.run(O._inloop_self_correct(_DB, _RUN(), {}))
    assert (repo / "Main.java").read_text() == "ORIG"             # restored, never left worse
    assert vcalls["n"] == 4                                       # 1 initial + 3 fix attempts
    assert any(k == "self_correction" and not p["success"] for k, p in events)
