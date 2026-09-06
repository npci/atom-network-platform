# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Stuck-run helper — proposes recovery options from a CLOSED catalog and validates any
free-text user direction. Off-catalog actions can never reach the dispatcher; the validator
falls closed (UNCLEAR) so a gibberish/unsafe direction always bounces to the options card.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.agents import stuck_helper as sh
from app.agents.stuck_helper import ACTION_CATALOG, propose_recovery, validate_custom_direction


def _run(error_code="BASE_DRIFT", error="base moved", phase="failed", status="failed"):
    return SimpleNamespace(id="r", phase=phase, status=status, error_code=error_code, error=error)


def _patch_call_llm(monkeypatch, response_text: str):
    async def _fake(**kw):
        return response_text
    monkeypatch.setattr(sh, "call_llm", _fake)


# ── propose_recovery ──────────────────────────────────────────────────────────────

def test_proposes_options_from_catalog_only(monkeypatch):
    _patch_call_llm(monkeypatch, """{
        "summary": "base moved",
        "options": [
            {"id": "a", "action_code": "rerun_code_gen", "title": "Rerun", "why": "fresh base", "tradeoffs": "redo work"},
            {"id": "b", "action_code": "abandon", "title": "Abandon", "why": "already pushed", "tradeoffs": "drop run"}
        ],
        "recommended": "a"
    }""")
    p = asyncio.run(propose_recovery(run=_run()))
    assert p["recommended"] == "a"
    assert {o["action_code"] for o in p["options"]} == {"rerun_code_gen", "abandon"}


def test_off_catalog_actions_are_filtered(monkeypatch):
    # LLM tries to smuggle a non-catalog action — it must NEVER reach the dispatcher.
    _patch_call_llm(monkeypatch, """{
        "summary": "...",
        "options": [
            {"id": "x", "action_code": "rm_rf_remote", "title": "Force push main", "why": "x", "tradeoffs": "y"},
            {"id": "y", "action_code": "rerun_code_gen", "title": "Rerun", "why": "x", "tradeoffs": "y"}
        ],
        "recommended": "x"
    }""")
    p = asyncio.run(propose_recovery(run=_run()))
    codes = [o["action_code"] for o in p["options"]]
    assert "rm_rf_remote" not in codes and "rerun_code_gen" in codes
    # Recommended fell back to the first surviving option (the rejected id is gone).
    assert p["recommended"] == "y"


def test_fail_open_on_llm_error(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(sh, "call_llm", _boom)
    p = asyncio.run(propose_recovery(run=_run()))
    assert p["options"][0]["action_code"] == "rerun_code_gen"   # static fallback
    assert "rerun" in p["summary"].lower() or "couldn't" in p["summary"].lower()


def test_fail_open_on_garbled_json(monkeypatch):
    _patch_call_llm(monkeypatch, "not json at all { broken")
    p = asyncio.run(propose_recovery(run=_run()))
    assert p["options"] and p["options"][0]["action_code"] == "rerun_code_gen"


def test_recent_events_flow_into_context(monkeypatch):
    captured = {}

    async def _capture(**kw):
        captured["user"] = next((m["content"] for m in kw["messages"] if m["role"] == "user"), "")
        return '{"summary":"s","options":[{"id":"a","action_code":"abandon","title":"x","why":"y","tradeoffs":"z"}],"recommended":"a"}'

    monkeypatch.setattr(sh, "call_llm", _capture)
    events = [{"kind": "push_preflight_failed", "payload": {"reasons": ["base SHA mismatch"]}}]
    asyncio.run(propose_recovery(run=_run(), recent_events=events))
    assert "push_preflight_failed" in captured["user"]
    assert "base SHA mismatch" in captured["user"]


# ── validate_custom_direction ─────────────────────────────────────────────────────

def test_validator_maps_safe_input_to_catalog_action(monkeypatch):
    _patch_call_llm(monkeypatch,
                    '{"verdict":"SAFE_AND_CLEAR","maps_to":"rerun_code_gen","why":"clear rerun request"}')
    v = asyncio.run(validate_custom_direction(run=_run(), recent_events=None,
                                              custom_direction="please re-run the code-gen from current main"))
    assert v["verdict"] == "SAFE_AND_CLEAR" and v["maps_to"] == "rerun_code_gen"


def test_validator_rejects_gibberish(monkeypatch):
    _patch_call_llm(monkeypatch, '{"verdict":"UNCLEAR","maps_to":null,"why":"gibberish input"}')
    v = asyncio.run(validate_custom_direction(run=_run(), recent_events=None,
                                              custom_direction="asdf hjkl banana"))
    assert v["verdict"] == "UNCLEAR" and v["maps_to"] is None


def test_validator_rejects_unsafe(monkeypatch):
    _patch_call_llm(monkeypatch,
                    '{"verdict":"UNSAFE","maps_to":null,"why":"would force-push main"}')
    v = asyncio.run(validate_custom_direction(run=_run(), recent_events=None,
                                              custom_direction="force push to main and bypass review"))
    assert v["verdict"] == "UNSAFE"


def test_validator_downgrades_safe_but_unknown_mapping_to_unclear(monkeypatch):
    # LLM said SAFE but mapped to an action that doesn't exist — incoherent → bounce to UNCLEAR.
    _patch_call_llm(monkeypatch,
                    '{"verdict":"SAFE_AND_CLEAR","maps_to":"nuke_everything","why":"x"}')
    v = asyncio.run(validate_custom_direction(run=_run(), recent_events=None,
                                              custom_direction="x"))
    assert v["verdict"] == "UNCLEAR" and v["maps_to"] is None


def test_validator_fails_closed_on_llm_error(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("network")
    monkeypatch.setattr(sh, "call_llm", _boom)
    v = asyncio.run(validate_custom_direction(run=_run(), recent_events=None,
                                              custom_direction="rerun please"))
    # Fail-closed: an UNCLEAR forces the user to pick an option (never auto-applies on a hiccup).
    assert v["verdict"] == "UNCLEAR" and v["maps_to"] is None


def test_eligible_for_recovery_gate_rejects_healthy_active_runs():
    """Codex P1 server-side gate: the UI hides 'Ask AI what to do' on healthy runs, but the
    API must enforce its own precondition — letting a recovery action fire against an actively-
    progressing run can corrupt its workspace. Failed/cancelled/error-coded/awaiting runs are
    eligible; a clean active-running run is NOT."""
    from app.api.agentic import _eligible_for_recovery

    # Eligible — terminal failures, error codes, parked at a human gate.
    assert _eligible_for_recovery(SimpleNamespace(status="failed", phase="failed",
                                                  error_code="BASE_DRIFT")) is True
    assert _eligible_for_recovery(SimpleNamespace(status="cancelled", phase="failed",
                                                  error_code=None)) is True
    assert _eligible_for_recovery(SimpleNamespace(status="active", phase="awaiting_human_approval",
                                                  error_code=None)) is True
    assert _eligible_for_recovery(SimpleNamespace(status="active", phase="rebase_reverify",
                                                  error_code=None)) is True
    assert _eligible_for_recovery(SimpleNamespace(status="active", phase="code_change",
                                                  error_code="GIT_REMOTE_AUTH")) is True
    # NOT eligible — healthy progressing runs.
    assert _eligible_for_recovery(SimpleNamespace(status="active", phase="code_change",
                                                  error_code=None)) is False
    assert _eligible_for_recovery(SimpleNamespace(status="active", phase="review",
                                                  error_code=None)) is False
    assert _eligible_for_recovery(SimpleNamespace(status="completed", phase="completed",
                                                  error_code=None)) is False


def test_catalog_keys_match_dispatcher_branches():
    # Lock-in: any catalog change MUST be matched by a dispatcher branch — else stuck-decide
    # would 400 a perfectly-valid option the LLM offered. Audited from api/agentic.py dispatcher.
    assert set(ACTION_CATALOG) == {"rerun_code_gen", "reset_and_retry_push", "retry_push",
                                   "abandon", "resume_once_more"}


def test_reset_workspace_does_git_reset_mixed_to_recorded_base(monkeypatch, tmp_path):
    """The fast-path workspace reset MUST: (a) reset HEAD to the manifest's recorded base, and
    (b) use ``--mixed`` so the working-tree files survive (the push commits those files via the
    GitLab API, so losing them would silently corrupt the change)."""
    import subprocess
    from app.agents import agentic_orchestrator as O
    repo = tmp_path / "r1"
    repo.mkdir()
    def _git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    _git("init", "-q"); _git("config", "user.email", "t@t"); _git("config", "user.name", "t")
    (repo / "a.txt").write_text("base\n"); _git("add", "."); _git("commit", "-qm", "base")
    base_sha = _git("rev-parse", "HEAD").stdout.strip()
    # Simulate the situation the user hit: a leftover local commit advanced HEAD past the base,
    # AND the agent has modified files on disk that must survive the reset.
    (repo / "a.txt").write_text("base+leftover\n"); _git("add", ".")
    _git("commit", "-qm", "leftover from prior push")
    (repo / "a.txt").write_text("base+leftover+agent-edit\n")  # uncommitted: this MUST survive

    # Stub the orchestrator's run + manifest lookups to point at the temp repo.
    class _Run: id = "r"; selected_repo_ids = ["repo1"]
    class _Man:
        per_repo = [{"repo_id": "repo1", "base_commit_sha": base_sha}]
    class _Q:
        def __init__(self, r): self._r = r
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def first(self): return self._r
    class _DB:
        def get(self, *a, **k): return _Run()
        def query(self, *a, **k): return _Q(_Man())
    monkeypatch.setattr(O, "_ws_id", lambda run: "ws1")
    monkeypatch.setattr(O.workspace_local, "repo_dir", lambda ws, rid: repo)

    info = O.reset_workspace_to_recorded_base(_DB(), "r")
    assert info == {"reset": 1, "repos": 1}
    # HEAD is back at base, but the agent's uncommitted file edit IS still on disk.
    assert _git("rev-parse", "HEAD").stdout.strip() == base_sha
    assert (repo / "a.txt").read_text() == "base+leftover+agent-edit\n"


def test_reset_workspace_is_a_noop_when_head_already_at_base(monkeypatch, tmp_path):
    """No reset is performed (or attempted) when the workspace HEAD already matches the manifest
    base — important so repeated/concurrent retries don't churn the workspace."""
    import subprocess
    from app.agents import agentic_orchestrator as O
    repo = tmp_path / "r1"; repo.mkdir()
    def _git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    _git("init", "-q"); _git("config", "user.email", "t@t"); _git("config", "user.name", "t")
    (repo / "a.txt").write_text("x"); _git("add", "."); _git("commit", "-qm", "base")
    base_sha = _git("rev-parse", "HEAD").stdout.strip()

    class _Run: id = "r"; selected_repo_ids = ["repo1"]
    class _Man:
        per_repo = [{"repo_id": "repo1", "base_commit_sha": base_sha}]
    class _Q:
        def __init__(self, r): self._r = r
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def first(self): return self._r
    class _DB:
        def get(self, *a, **k): return _Run()
        def query(self, *a, **k): return _Q(_Man())
    monkeypatch.setattr(O, "_ws_id", lambda run: "ws1")
    monkeypatch.setattr(O.workspace_local, "repo_dir", lambda ws, rid: repo)

    info = O.reset_workspace_to_recorded_base(_DB(), "r")
    assert info == {"reset": 0, "repos": 1}   # nothing to reset


def test_resurrect_for_push_flips_failed_run_to_pushable(monkeypatch):
    """A push-preflight failure marks the run terminal (phase=failed) but leaves the manifest
    approved — the recovery actions must flip it back to ``awaiting_human_approval`` so
    push_run_now's phase guard accepts it. Without this, both retry_push and
    reset_and_retry_push 409 with 'run is not pushable from phase=failed'."""
    from app.api.agentic import _resurrect_for_push
    # _resurrect_for_push imports emit_event lazily — patch the SOURCE, not the api module.
    import app.agents.agentic_events as AE
    monkeypatch.setattr(AE, "emit_event", lambda *a, **k: None)

    class _DB:
        commits = 0
        def commit(self): self.commits += 1

    failed = SimpleNamespace(id="r", phase="failed", status="failed", lease_owner="celery@h",
                             lease_expires_at="x", cancel_requested=True)
    db = _DB()
    assert _resurrect_for_push(db, failed) is True
    assert failed.phase == "awaiting_human_approval" and failed.status == "active"
    assert failed.lease_owner is None and failed.cancel_requested is False
    assert db.commits == 1     # committed so push_run_now sees the new phase

    # Already pushable → no-op (no commit, no fields touched).
    healthy = SimpleNamespace(id="r", phase="awaiting_human_approval", status="active",
                              lease_owner=None, lease_expires_at=None, cancel_requested=False)
    db2 = _DB()
    assert _resurrect_for_push(db2, healthy) is False
    assert db2.commits == 0 and healthy.phase == "awaiting_human_approval"


def test_reset_and_retry_push_fast_path_is_in_catalog():
    # The git-trick fast path is wired with the right wording so the LLM only offers it for
    # BASE_DRIFT (not for genuine remote-base-moved cases that need rerun).
    fits = ACTION_CATALOG["reset_and_retry_push"]["fits"].lower()
    assert "base_drift" in fits and "local workspace head drifted" in fits
    assert "rerun" in fits   # references the safer alternative so the LLM doesn't pick it blindly
