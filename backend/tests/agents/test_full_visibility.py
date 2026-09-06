# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Full-visibility change-set: EVERYTHING the agent changed is always shown.

The change-set used to be HEAD-relative, so any work sitting in a LOCAL commit —
a failed push's leftover ``agentic:`` commit, or an agent-made ``git commit`` (which
the git-guard allows) — silently vanished from the changes view, the frozen
manifest, and therefore the eventual push. The change-set is now anchored to the
RECORDED clone-time base (``.base_sha`` marker, HEAD fallback for legacy clones):
uncommitted, locally-committed, and pushed work all stay visible.
"""
import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.agents import agentic_orchestrator as O
from app.agents import workspace_local as W


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, check=False)


def _mk_repo(tmp_path, ws="ws1", rid="repoA"):
    rd = tmp_path / ws / rid
    rd.mkdir(parents=True)
    _git(rd, "init", "-q")
    _git(rd, "config", "user.email", "t@t"); _git(rd, "config", "user.name", "t")
    return rd


def _commit_all(rd, msg="base"):
    _git(rd, "add", "-A"); _git(rd, "commit", "-q", "-m", msg)
    return _git(rd, "rev-parse", "HEAD").stdout.strip()


def _run_stub(ws="ws1", rids=("repoA",)):
    return SimpleNamespace(id=ws, workspace_run_id=None, selected_repo_ids=list(rids))


# ── recorded_base resolution ───────────────────────────────────────────────────

def test_recorded_base_marker_then_head_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "a.txt").write_text("a\n")
    base = _commit_all(rd)
    assert W.recorded_base("ws1", "repoA") == base          # no marker → HEAD
    (rd / ".base_sha").write_text(base)
    (rd / "a.txt").write_text("a2\n")
    head2 = _commit_all(rd, "agentic: moved head")
    assert W.recorded_base("ws1", "repoA") == base          # marker wins over moved HEAD
    (rd / ".base_sha").write_text("not-a-sha")
    assert W.recorded_base("ws1", "repoA") == head2         # corrupt marker → HEAD fallback


def test_recorded_base_unresolvable_falls_back_to_head(tmp_path, monkeypatch):
    """A well-formed sha this repo no longer HAS (force-pushed base, workspace copied
    between environments) must not be handed to ``git diff``: the diff fails and every
    caller reads that as 'no tracked changes', silently shrinking the change-set the
    human approves — the exact bug the anchor exists to prevent."""
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "tracked.java").write_text("old\n")
    head = _commit_all(rd)
    (rd / "tracked.java").write_text("MODIFIED\n")
    (rd / "untracked.java").write_text("NEW\n")

    (rd / ".base_sha").write_text("0" * 39 + "1")           # 40 hex, not a commit here
    assert W.recorded_base("ws1", "repoA") == head          # unresolvable → HEAD fallback
    got = dict((p, op) for op, p in W.changed_files("ws1", "repoA"))
    assert got == {"tracked.java": "modify", "untracked.java": "add"}


# ── THE guarantee: committed + uncommitted + untracked all visible ─────────────

def _three_state_repo(tmp_path):
    rd = _mk_repo(tmp_path)
    (rd / "committed.java").write_text("old\n")
    (rd / "worktree.java").write_text("old\n")
    base = _commit_all(rd)
    (rd / ".base_sha").write_text(base)                     # simulate clone-time marker
    (rd / "committed.java").write_text("NEW-committed\n")
    _commit_all(rd, "agentic: leftover push commit")        # locally COMMITTED change
    (rd / "worktree.java").write_text("NEW-worktree\n")     # UNCOMMITTED change
    (rd / "untracked.java").write_text("NEW-untracked\n")   # UNTRACKED new file
    return rd


def test_changed_files_sees_all_three_states(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    _three_state_repo(tmp_path)
    got = dict((p, op) for op, p in W.changed_files("ws1", "repoA"))
    assert got == {"committed.java": "modify", "worktree.java": "modify",
                   "untracked.java": "add"}
    assert ".base_sha" not in got and ".lease" not in got


def test_capture_diffs_shows_all_three_states(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    _three_state_repo(tmp_path)
    by = {f["path"]: f for f in O._capture_diffs(None, _run_stub())["repoA"]["files"]}
    assert set(by) == {"committed.java", "worktree.java", "untracked.java"}
    assert "+NEW-committed" in by["committed.java"]["patch"]   # the previously-lost one
    assert "+NEW-worktree" in by["worktree.java"]["patch"]
    assert by["untracked.java"]["op"] == "add"
    assert all(f["add"] == 1 and f["del"] in (0, 1) for f in by.values())


def test_legacy_clone_without_marker_is_head_relative(tmp_path, monkeypatch):
    # Lock the fallback: a pre-marker workspace keeps the old HEAD-relative behavior
    # (no silent semantic change for in-flight legacy runs).
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "a.java").write_text("old\n")
    _commit_all(rd)
    (rd / "a.java").write_text("committed\n")
    _commit_all(rd, "agentic: local commit")                 # no marker on disk
    (rd / "b.java").write_text("worktree\n")
    got = dict((p, op) for op, p in W.changed_files("ws1", "repoA"))
    assert got == {"b.java": "add"}                          # committed edit invisible (legacy)


# ── clone(): marker written, resume re-anchors to it ───────────────────────────

def test_clone_records_base_and_resume_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path / "workspaces")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@t"); _git(origin, "config", "user.name", "t")
    (origin / "a.java").write_text("a\n")
    origin_head = _commit_all(origin)

    sha = W.clone("ws1", "repoA", str(origin), "main")
    rd = W.repo_dir("ws1", "repoA")
    assert sha == origin_head
    assert (rd / ".base_sha").read_text().strip() == origin_head

    # crash-resume AFTER the flow made a local commit: clone() must return the
    # recorded base, not the moved HEAD — the change-set stays anchored.
    _git(rd, "config", "user.email", "t@t"); _git(rd, "config", "user.name", "t")
    (rd / "a.java").write_text("changed\n")
    _commit_all(rd, "agentic: mid-run commit")
    assert W.clone("ws1", "repoA", str(origin), "main") == origin_head
    assert [p for _op, p in W.changed_files("ws1", "repoA")] == ["a.java"]
