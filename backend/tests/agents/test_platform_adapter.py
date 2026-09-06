# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""PlatformAdapter execution + allowlist (§18.2). Runs git in-process; no DB."""
import pytest

from app.agents.platform_adapter import (
    PlatformAdapter,
    CommandNotAllowed,
    _argv0_key,
    ALLOWED_ARGV0,
)


def test_argv0_key_normalises_path_and_windows_suffixes():
    assert _argv0_key("/usr/bin/git") == "git"
    assert _argv0_key("mvn.cmd") == "mvn"
    assert _argv0_key("git.exe") == "git"
    assert _argv0_key("./mvnw") == "mvnw"


def test_allowlist_blocks_dangerous_commands(tmp_path):
    pa = PlatformAdapter()
    for bad in (["rm", "-rf", "/"], ["bash", "-c", "echo hi"], ["curl", "http://x"], []):
        with pytest.raises(CommandNotAllowed):
            pa.run_command(tmp_path, bad)


def test_allowlist_contains_build_vcs_and_readonly_tools():
    assert {"git", "mvn", "mvnw", "javac", "java"} <= ALLOWED_ARGV0
    # READ-ONLY inspection is allowed (diagnose generated/gitignored files).
    assert {"grep", "cat", "head", "tail", "ls", "wc", "sort"} <= ALLOWED_ARGV0
    # Mutating / executing / network commands stay OUT.
    assert not ({"bash", "rm", "curl", "find", "sed", "awk", "xargs", "mv", "cp"} & ALLOWED_ARGV0)


def test_path_bearing_argv0_is_rejected_even_with_allowed_basename(tmp_path):
    # The containment bypass: a path whose basename is "git" must NOT run.
    pa = PlatformAdapter()
    for evil in (["/tmp/evil/git", "--version"], ["./git", "status"], [r"sub\dir\mvn", "-v"]):
        with pytest.raises(CommandNotAllowed):
            pa.run_command(tmp_path, evil)


def test_git_runs_and_reports_exit_code(tmp_path):
    pa = PlatformAdapter()
    res = pa.run_command(tmp_path, ["git", "--version"])
    assert res.ok and res.exit_code == 0
    assert "git version" in res.stdout.lower()
    assert res.duration_ms >= 0 and res.timed_out is False


def test_nonzero_exit_is_captured_not_raised(tmp_path):
    pa = PlatformAdapter()
    # `git rev-parse` outside a repo exits non-zero — surfaced, not raised.
    res = pa.run_command(tmp_path, ["git", "rev-parse", "HEAD"])
    assert res.exit_code != 0 and res.ok is False


def test_clean_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "supersecret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = PlatformAdapter()._clean_env()
    assert "GITLAB_TOKEN" not in env and "PATH" in env
