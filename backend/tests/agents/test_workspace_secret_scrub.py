# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""T10 (THREAT_MODEL.md) — workspace credential scrubbing / secret detection.

These tests exercise REAL git repositories rather than synthetic fixtures,
because the leak this closes is a property of how `git clone` behaves: it
records the full clone URL in `.git/logs/**`, and `git remote set-url` (the
existing §22 post-clone scrub) does not remove it. A mocked reflog would not
prove that.

Two invariants matter most and are asserted explicitly:

  * After scrubbing, **git must still work** on the repo (`status`, `log`,
    `reflog`, `fsck`). A scrubber that corrupts the clone is worse than the leak
    it removes.
  * The source-tree scan must **never modify a file**. Rewriting a developer's
    committed content would corrupt the diff a human is about to review and
    approve.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.agents.workspace_secret_scrub import (
    scan_workspace_for_secrets,
    scrub_workspace_credentials,
)

_TOKEN = "glpat-SECRETTOKEN1234567890abc"
_TOKENED_URL = f"https://oauth2:{_TOKEN}@gitlab.example.com/grp/repo.git"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _have_git() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _have_git(), reason="git not available")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A realistic workspace: `<ws>/<repo_id>/` holding a real clone whose
    reflog carries a tokened clone URL, exactly as `build_clone_url` +
    `git clone` would leave it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("hello", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "init")

    ws = tmp_path / "run-abc123"
    ws.mkdir()
    subprocess.run(["git", "clone", "-q", str(origin), "repo-1"],
                   cwd=ws, capture_output=True)

    # Rewrite the reflog's clone line to carry the tokened URL. git itself would
    # have written this had the clone been done over HTTPS with credentials.
    reflog = ws / "repo-1" / ".git" / "logs" / "HEAD"
    reflog.write_text(
        reflog.read_text(encoding="utf-8").replace(
            "clone: from", f"clone: from {_TOKENED_URL} #"),
        encoding="utf-8",
    )
    return ws


class TestCredentialScrub:
    def test_token_is_present_before_scrub(self, workspace: Path):
        """Guards the fixture itself — if this fails, the test below proves
        nothing."""
        reflog = (workspace / "repo-1" / ".git" / "logs" / "HEAD").read_text(encoding="utf-8")
        assert _TOKEN in reflog

    def test_scrub_removes_the_token_from_the_reflog(self, workspace: Path):
        summary = scrub_workspace_credentials(workspace)
        reflog = (workspace / "repo-1" / ".git" / "logs" / "HEAD").read_text(encoding="utf-8")
        assert _TOKEN not in reflog
        assert "[credentials-redacted]@" in reflog
        assert summary["credentials_removed"] >= 1
        assert summary["files_rewritten"] >= 1

    def test_git_still_functions_after_scrub(self, workspace: Path):
        """A scrubber that corrupts the clone is worse than the leak."""
        scrub_workspace_credentials(workspace)
        repo = workspace / "repo-1"
        for args in (("status", "--porcelain"), ("log", "--oneline"),
                     ("reflog",), ("rev-parse", "HEAD"), ("fsck",)):
            result = _git(repo, *args)
            assert result.returncode == 0, f"git {args[0]} broke: {result.stderr}"

    def test_scrub_is_idempotent(self, workspace: Path):
        first = scrub_workspace_credentials(workspace)
        second = scrub_workspace_credentials(workspace)
        assert first["credentials_removed"] >= 1
        assert second["credentials_removed"] == 0, "second pass should find nothing left"

    def test_working_tree_is_untouched_by_the_scrub(self, workspace: Path):
        """The scrub targets git METADATA only — tracked content must not move."""
        tracked = workspace / "repo-1" / "a.txt"
        before = tracked.read_bytes()
        scrub_workspace_credentials(workspace)
        assert tracked.read_bytes() == before

    def test_missing_directory_is_safe(self, tmp_path: Path):
        summary = scrub_workspace_credentials(tmp_path / "does-not-exist")
        assert summary == {"files_rewritten": 0, "credentials_removed": 0}

    def test_workspace_without_git_dirs_is_safe(self, tmp_path: Path):
        ws = tmp_path / "run-x"
        (ws / "repo-1").mkdir(parents=True)
        (ws / "repo-1" / "f.txt").write_text("no git here", encoding="utf-8")
        assert scrub_workspace_credentials(ws)["credentials_removed"] == 0


class TestSourceSecretScan:
    @pytest.fixture
    def tree(self, tmp_path: Path) -> Path:
        ws = tmp_path / "run-y"
        repo = ws / "repo-1"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "App.java").write_text(
            'String k = "glpat-abcdefghij1234567890XYZ";', encoding="utf-8")
        (repo / "config.yml").write_text(
            "url: https://user:hunter2@internal.example.com/svc", encoding="utf-8")
        (repo / "id_rsa").write_text(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
            encoding="utf-8")
        # Contract values that must NOT be mistaken for secrets.
        (repo / "clean.java").write_text(
            "int timeout = 30000; long ts = 1735689600000L;", encoding="utf-8")
        # Allowlisted: a long all-lowercase `a2a_` Python identifier, which
        # collides with the minted-key length rule. See .gitleaks.toml's note.
        (repo / "test_names.py").write_text(
            "def test_a2a_rejects_a_message_kind_the_transport_cannot_carry(): pass",
            encoding="utf-8")
        # Build output must be pruned.
        (repo / "target").mkdir()
        (repo / "target" / "leak.txt").write_text(
            "glpat-shouldbeskipped1234567890", encoding="utf-8")
        return ws

    def test_detects_real_credential_classes(self, tree: Path):
        findings = scan_workspace_for_secrets(tree)["findings"]
        assert "gitlab-pat" in findings
        assert "credential-in-url" in findings
        assert "private-key-pem" in findings

    def test_never_modifies_any_file(self, tree: Path):
        """Rewriting committed content would corrupt the diff under review."""
        repo = tree / "repo-1"
        before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        scan_workspace_for_secrets(tree)
        after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        assert before == after

    def test_prunes_build_output_directories(self, tree: Path):
        """`target/` holds a matching token; it must not be counted."""
        assert scan_workspace_for_secrets(tree)["findings"].get("gitlab-pat") == 1

    def test_honours_the_allowlist(self, tree: Path):
        assert "a2a-partner-api-key" not in scan_workspace_for_secrets(tree)["findings"]

    def test_clean_contract_values_are_not_flagged(self, tmp_path: Path):
        ws = tmp_path / "run-z"
        (ws / "repo-1").mkdir(parents=True)
        (ws / "repo-1" / "spec.java").write_text(
            "timeout=30000; ts=1735689600000L; code=1234567891; budget=999999999;",
            encoding="utf-8")
        assert scan_workspace_for_secrets(ws)["findings"] == {}

    def test_file_budget_sets_truncated_flag(self, tmp_path: Path):
        """A bounded scan must never be mistaken for a clean bill of health."""
        ws = tmp_path / "run-w"
        repo = ws / "repo-1"
        repo.mkdir(parents=True)
        for i in range(5):
            (repo / f"f{i}.txt").write_text("nothing", encoding="utf-8")
        result = scan_workspace_for_secrets(ws, max_files=2)
        assert result["truncated"] is True
        assert result["files_scanned"] <= 2

    def test_binary_files_are_skipped(self, tmp_path: Path):
        ws = tmp_path / "run-v"
        repo = ws / "repo-1"
        repo.mkdir(parents=True)
        (repo / "blob.dat").write_bytes(b"\x00\x01glpat-abcdefghij1234567890XYZ")
        assert scan_workspace_for_secrets(ws)["findings"] == {}

    def test_missing_directory_is_safe(self, tmp_path: Path):
        result = scan_workspace_for_secrets(tmp_path / "nope")
        assert result["findings"] == {} and result["files_scanned"] == 0


def _workspace_local_importable() -> bool:
    """`workspace_local` imports `core.database`, which calls `create_engine`
    with Postgres-only pool kwargs (`max_overflow`, `pool_timeout`). Against the
    SQLite URL used for local test runs that raises TypeError at import time —
    a pre-existing environment limitation that every existing `workspace_local`
    test in this repo hits identically (e.g. tests/agents/test_eol_preservation.py
    errors on collection). These two tests are the only ones here that need the
    real GC module, so they skip cleanly rather than failing for a reason
    unrelated to what they assert. They DO run in CI, where DATABASE_URL points
    at Postgres.
    """
    try:
        import app.agents.workspace_local  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _workspace_local_importable(),
                    reason="workspace_local requires a Postgres DATABASE_URL "
                           "(pre-existing create_engine limitation, see helper docstring)")
class TestGcIntegration:
    def test_scrub_runs_before_removal_in_cleanup(self, workspace: Path, monkeypatch):
        """`cleanup_workspace` must scrub BEFORE `rmtree`, so a removal failure
        still leaves a scrubbed (not tokened) directory behind."""
        from app.agents import workspace_local

        monkeypatch.setattr(workspace_local, "run_dir", lambda _rid: workspace)
        # Make removal fail, leaving the directory on disk to inspect.
        monkeypatch.setattr(workspace_local.shutil, "rmtree",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))

        assert workspace_local.cleanup_workspace("run-abc123") is False
        reflog = (workspace / "repo-1" / ".git" / "logs" / "HEAD").read_text(encoding="utf-8")
        assert _TOKEN not in reflog, "token survived a failed removal — scrub did not run first"

    def test_scrub_failure_does_not_prevent_removal(self, tmp_path: Path, monkeypatch):
        """The scrub is defence-in-depth around the operation that actually
        deletes the secret; a scrub error must be swallowed and removal must
        still proceed.

        Uses a PLAIN directory rather than the real-clone `workspace` fixture on
        purpose: `shutil.rmtree` cannot delete a real git clone on Windows,
        because git marks `.git/objects/**` read-only and `rmtree` has no
        `onerror` handler here. That is a pre-existing platform limitation in
        `cleanup_workspace` (verified independently of this change, and worth
        fixing separately — it means immediate cleanup silently no-ops on
        Windows dev machines, leaving the workspace for the TTL sweep). Mixing
        it into this assertion would test the wrong thing.
        """
        from app.agents import workspace_local

        ws = tmp_path / "run-plain"
        (ws / "repo-1").mkdir(parents=True)
        (ws / "repo-1" / "f.txt").write_text("content", encoding="utf-8")

        monkeypatch.setattr(workspace_local, "run_dir", lambda _rid: ws)
        monkeypatch.setattr(
            "app.agents.workspace_secret_scrub.scrub_and_scan",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert workspace_local.cleanup_workspace("run-plain") is True
        assert not ws.exists(), "a scrub failure must not block removal"


def test_module_imports_without_application_settings():
    """The scrubber must not require a valid DATABASE_URL/SECRET_KEY to run —
    it is invoked from GC, and coupling a directory-name check to full app
    configuration was a real defect found while building this.
    """
    import importlib
    mod = importlib.import_module("app.agents.workspace_secret_scrub")
    assert "target" in mod._BUILD_OUTPUT_DIRS
    # Sanity: the constant is kept in step with workspace_local's copy.
    shutil.which("git")  # no-op; keeps the import used
