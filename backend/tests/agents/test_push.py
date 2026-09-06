# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Forge-agnostic push (§12/§22) verified against a LOCAL bare git remote — no
GitLab needed. Confirms the one guarded remote write actually lands the branch
(with add/modify/delete) and that the git-guard still blocks anything else."""
import subprocess
from pathlib import Path

import pytest

from app.core.config import settings
from app.agents import agentic_orchestrator as O
from app.agents import workspace_local, git_guard
from app.agents.platform_adapter import adapter


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def bare_remote_and_clone(tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "-q", str(bare))
    seed = tmp_path / "seed"
    _git("clone", "-q", str(bare), str(seed))
    (seed / "f.txt").write_text("hi")
    _git("add", "-A", cwd=seed)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path / "ws"))
    rd = workspace_local.repo_dir("run-1", "repo-1")
    rd.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", "--branch", "main", str(bare), str(rd))
    base = subprocess.run(["git", "-C", str(rd), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return bare, rd, base


def test_generic_push_lands_branch_with_add_and_delete(bare_remote_and_clone):
    bare, rd, base = bare_remote_and_clone
    (rd / "A.java").write_text("class A{}")      # agent add
    (rd / "f.txt").unlink()                        # agent delete
    (rd / ".lease").write_text("run-1")           # internal marker — must NOT be committed

    # paths = the approved manifest change-set (NOT a blanket `git add -A`).
    O._git_push_branch("run-1", "repo-1", "feature/xsd-refund", base, str(bare), str(bare),
                       ["A.java", "f.txt"])

    branches = subprocess.run(["git", "-C", str(bare), "branch", "--list", "feature/xsd-refund"],
                              capture_output=True, text=True).stdout
    files = subprocess.run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "feature/xsd-refund"],
                           capture_output=True, text=True).stdout
    assert "feature/xsd-refund" in branches
    assert "A.java" in files and "f.txt" not in files   # add landed, delete applied
    assert ".lease" not in files                        # internal file NOT swept in (no `git add -A`)
    assert git_guard.active_policy() is None             # policy reset after the one push


def test_guard_blocks_other_pushes_during_a_run(bare_remote_and_clone):
    bare, rd, base = bare_remote_and_clone
    tok = git_guard.set_policy(git_guard.GitGuardPolicy(run_branch="feature/xsd-refund", base_sha=base))
    try:
        with pytest.raises(git_guard.GitGuardDenied):
            adapter.run_command(rd, ["git", "push", "origin", "main"])
        with pytest.raises(git_guard.GitGuardDenied):
            adapter.run_command(rd, ["git", "push", "--force", "origin", "feature/xsd-refund"])
    finally:
        git_guard.reset_policy(tok)
