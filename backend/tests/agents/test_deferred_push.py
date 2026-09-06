# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deferred git push ('Approve — push later') + run_command self-healing.

Covers the new state-machine edges, the GC keep-alive for approved-but-unpushed
trees, and the argv self-heal in run_command — all without a live DB/network.
"""
from types import SimpleNamespace

from app.agents import agentic_tools as T
from app.agents import workspace_local as W
from app.agents.agentic_state import _can_transition
from app.models.agentic import AgenticPhase as P


# ── State machine ──────────────────────────────────────────────────────────────

def test_deferred_push_transitions():
    # Approve with push=False completes the run from the approval gate…
    assert _can_transition("awaiting_human_approval", P.COMPLETED)
    # …and "Push to git now" re-opens it for the single remote write.
    assert _can_transition("completed", P.PUSHING)
    # The push itself still terminates normally.
    assert _can_transition("pushing", P.COMPLETED)
    # Other terminals stay sinks.
    assert not _can_transition("failed", P.PUSHING)
    assert not _can_transition("cancelled", P.PUSHING)


# ── GC keep-alive for approved-but-unpushed workspaces ─────────────────────────

class _Q:
    """Minimal query stub: returns canned objects per model class."""
    def __init__(self, by_model):
        self.by_model = by_model

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        rows = self.by_model.get(name, [])
        return SimpleNamespace(
            filter=lambda *a, **k: SimpleNamespace(
                order_by=lambda *a2, **k2: SimpleNamespace(first=lambda: (rows[0] if rows else None)),
                all=lambda: rows,
                first=lambda: (rows[0] if rows else None)),
        )


def _run(kind="code"):
    return SimpleNamespace(id="r1", kind=kind)


def test_push_pending_keeps_deferred_tree():
    man = SimpleNamespace(approved_at="2026-06-11")
    db = _Q({"ChangeManifest": [man], "AgenticRunRepo": []})   # approved, nothing pushed
    assert W._push_pending(db, _run("code")) is True
    assert W._push_pending(db, _run("full")) is True


def test_push_pending_clears_after_push():
    man = SimpleNamespace(approved_at="2026-06-11")
    pushed = SimpleNamespace(push_state="pushed")
    db = _Q({"ChangeManifest": [man], "AgenticRunRepo": [pushed]})
    assert W._push_pending(db, _run("code")) is False


def test_push_pending_stale_push_keeps_tree():
    # Pushed under an OLDER manifest (fix rounds after the push re-froze it): the tree
    # is the source for the re-push — GC must keep it. This is the prod defect where
    # the re-consented push silently no-op'd while git held the older approved state.
    man = SimpleNamespace(approved_at="2026-07-27", manifest_hash="H2")
    stale = SimpleNamespace(push_state="pushed", pushed_manifest_hash="H1")
    db = _Q({"ChangeManifest": [man], "AgenticRunRepo": [stale]})
    assert W._push_pending(db, _run("code")) is True


def test_push_pending_current_or_legacy_push_clears():
    man = SimpleNamespace(approved_at="2026-07-27", manifest_hash="H2")
    current = SimpleNamespace(push_state="pushed", pushed_manifest_hash="H2")
    legacy = SimpleNamespace(push_state="pushed", pushed_manifest_hash=None)  # pre-0108 row
    assert W._push_pending(_Q({"ChangeManifest": [man], "AgenticRunRepo": [current]}), _run("code")) is False
    assert W._push_pending(_Q({"ChangeManifest": [man], "AgenticRunRepo": [legacy]}), _run("code")) is False


def test_push_pending_exempts_phase_a_and_unapproved():
    man = SimpleNamespace(approved_at="2026-06-11")
    db = _Q({"ChangeManifest": [man], "AgenticRunRepo": []})
    assert W._push_pending(db, _run("xsd")) is False           # Phase A never pushes itself
    db2 = _Q({"ChangeManifest": [SimpleNamespace(approved_at=None)], "AgenticRunRepo": []})
    assert W._push_pending(db2, _run("code")) is False         # not approved → normal GC
    assert W._push_pending(None, _run("code")) is False        # no db → fail-open (collectable)


# ── run_command argv self-heal ─────────────────────────────────────────────────

def _ctx():
    return T.RunContext(run_id="r1", selected_repo_ids=["repo1"])


def test_run_command_parses_stringified_argv(monkeypatch):
    seen = {}

    def fake_run(root, argv, timeout_s=None):
        seen["argv"] = argv
        return SimpleNamespace(stdout="ok", stderr="", exit_code=0, timed_out=False, duration_ms=1)

    monkeypatch.setattr("app.agents.platform_adapter.adapter.run_command", fake_run)
    monkeypatch.setattr(T, "_repo_root", lambda ctx, rid: "/tmp")
    out = T.run_command(_ctx(), "repo1", '["git", "status"]')   # JSON-string argv (the model slip)
    assert seen["argv"] == ["git", "status"]
    assert "exit=0" in out


def test_run_command_bad_argv_is_error():
    text, is_error = T.execute_tool(_ctx(), "run_command", {"repo_id": "repo1", "argv": "not a list"})
    assert is_error is True
    assert "argv" in text


# ── Prod hardening: disk guard + error classification ──────────────────────────

def test_disk_guard_blocks_below_floor(monkeypatch, tmp_path):
    from app.agents import workspace_local as W
    from app.core.config import settings
    monkeypatch.setattr(settings, "agentic_min_disk_free_mb", 10_000_000)  # absurd floor
    import shutil as _sh
    monkeypatch.setattr(_sh, "disk_usage", lambda p: SimpleNamespace(total=0, used=0, free=1024 * 1024))
    try:
        W._assert_disk_space(tmp_path)
        assert False, "expected WorkspaceError"
    except W.WorkspaceError as e:
        assert "insufficient workspace disk" in str(e)


def test_disk_guard_off_when_zero(monkeypatch, tmp_path):
    from app.agents import workspace_local as W
    from app.core.config import settings
    monkeypatch.setattr(settings, "agentic_min_disk_free_mb", 0)
    W._assert_disk_space(tmp_path)   # no raise


def test_error_code_classification():
    from app.agents.agentic_orchestrator import _error_code
    assert _error_code(Exception("429 rate limit exceeded")) == "llm_rate_limit"
    assert _error_code(Exception("clone failed for repo (exit 128)")) == "git_clone_failed"
    assert _error_code(Exception("anthropic overloaded")) in ("llm_unavailable", "llm_rate_limit")
    assert _error_code(Exception("something odd")) == "internal_error"


# ── Review fixes: git-guard deny-remote during the loop + preflight delete revalidation ──

def test_deny_remote_policy_blocks_push_allows_local():
    from app.agents import git_guard as G
    pol = G.deny_remote_policy()
    # Remote writes denied.
    assert G.classify_git(["push", "origin", "feat/x"], pol).allowed is False
    assert G.classify_git(["remote", "set-url", "origin", "http://evil"], pol).allowed is False
    assert G.classify_git(["pull"], pol).allowed is False
    # Local reads/edits allowed (incl. git grep used by the grep tool).
    assert G.classify_git(["grep", "-n", "foo"], pol).allowed is True
    assert G.classify_git(["diff", "HEAD"], pol).allowed is True
    assert G.classify_git(["add", "-A"], pol).allowed is True


def test_enforce_denies_push_under_deny_policy():
    from app.agents import git_guard as G
    tok = G.set_policy(G.deny_remote_policy())
    try:
        raised = False
        try:
            G.enforce(["git", "push", "origin", "main"])
        except G.GitGuardDenied:
            raised = True
        assert raised, "push must be denied under the deny-remote policy"
        G.enforce(["git", "grep", "-n", "x"])  # local: no raise
    finally:
        G.reset_policy(tok)


def test_push_preflight_rejects_restored_deletion():
    from app.agents import manifest as M
    man = {"per_repo": [{"repo_id": "r1", "base_commit_sha": "abc"}],
           "operations": [{"op": "delete", "repo_id": "r1", "path": "Old.java", "content_hash": None}]}
    # File still present in the workspace → approved deletion was restored → reject.
    ok, reasons = M.push_preflight(man, current_base_sha={"r1": "abc"},
                                   read_content=lambda rid, p: "still here")
    assert ok is False and any("restored" in r for r in reasons)
    # Genuinely gone → passes.
    ok2, _ = M.push_preflight(man, current_base_sha={"r1": "abc"},
                              read_content=lambda rid, p: None)
    assert ok2 is True


def test_push_preflight_catches_base_sha_drift():
    from app.agents import manifest as M
    man = {"per_repo": [{"repo_id": "r1", "base_commit_sha": "abc"}], "operations": []}
    # Actual workspace HEAD differs from the approved base → drift detected.
    ok, reasons = M.push_preflight(man, current_base_sha={"r1": "def"}, read_content=lambda rid, p: None)
    assert ok is False and any("base SHA" in r for r in reasons)
