# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 14 sandbox service.

Split into two groups:

  - Unit tests (always run) — pure helpers + mocked-Docker behaviour.
    `docker.from_env()` is monkeypatched, so these run without a daemon.

  - Integration tests (`@pytest.mark.sandbox`) — require a live Docker
    daemon. Auto-skip if `sandbox.is_docker_available()` is False so CI
    without Docker passes cleanly.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services import sandbox


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestValidatePaths:
    def test_accepts_relative_paths(self):
        sandbox._validate_paths({
            "src/Main.java":       "class M{}",
            "pom.xml":             "<project/>",
            "nested/dir/File.java":"class F{}",
        })  # no exception

    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="absolute"):
            sandbox._validate_paths({"/etc/passwd": "bad"})

    def test_rejects_parent_traversal(self):
        with pytest.raises(ValueError, match=r"'\.\.'"):
            sandbox._validate_paths({"../escape.txt": "bad"})

    def test_rejects_traversal_in_middle_of_path(self):
        with pytest.raises(ValueError, match=r"'\.\.'"):
            sandbox._validate_paths({"src/../../../etc/passwd": "bad"})

    def test_rejects_empty_or_non_string_key(self):
        with pytest.raises(ValueError):
            sandbox._validate_paths({"": "x"})


class TestBuildContainerKwargs:
    def test_sets_isolation_defaults(self):
        kw = sandbox._build_container_kwargs(
            image="maven:3", command="mvn -q compile",
            workdir_host="/tmp/foo", memory_limit="512m", cpu_limit=1.0,
            network_disabled=True,
        )
        assert kw["network_disabled"] is True
        assert kw["mem_limit"] == "512m"
        assert kw["nano_cpus"] == 1_000_000_000
        assert kw["working_dir"] == sandbox.WORKSPACE_MOUNT
        assert kw["volumes"] == {"/tmp/foo": {"bind": "/workspace", "mode": "rw"}}
        assert kw["detach"] is True
        assert kw["stdout"] is True
        assert kw["stderr"] is True

    def test_cpu_limit_fractional(self):
        kw = sandbox._build_container_kwargs(
            image="x", command="y", workdir_host="/tmp",
            memory_limit="256m", cpu_limit=0.5, network_disabled=True,
        )
        assert kw["nano_cpus"] == 500_000_000

    def test_network_can_be_enabled(self):
        kw = sandbox._build_container_kwargs(
            image="x", command="y", workdir_host="/tmp",
            memory_limit="256m", cpu_limit=1.0, network_disabled=False,
        )
        assert kw["network_disabled"] is False


class TestMaterialiseFiles:
    def test_writes_nested_files(self, tmp_path: Path):
        files = {
            "pom.xml": "<project/>",
            "src/main/java/Foo.java": "public class Foo {}",
            "src/test/java/FooTest.java": "public class FooTest {}",
        }
        sandbox._materialise_files(files, tmp_path)
        for rel, content in files.items():
            p = tmp_path / rel
            assert p.exists()
            assert p.read_text() == content


# ──────────────────────────────────────────────────────────────────────────────
# is_docker_available — probe
# ──────────────────────────────────────────────────────────────────────────────

class TestIsDockerAvailable:
    def test_returns_false_when_import_fails(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *args, **kwargs):
            if name == "docker":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocking_import)
        assert sandbox.is_docker_available() is False

    def test_returns_false_when_daemon_unreachable(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.ping.side_effect = RuntimeError("no daemon")
        mock_docker = MagicMock()
        mock_docker.from_env.return_value = mock_client
        import sys
        monkeypatch.setitem(sys.modules, "docker", mock_docker)
        assert sandbox.is_docker_available() is False

    def test_returns_true_when_ping_succeeds(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_docker = MagicMock()
        mock_docker.from_env.return_value = mock_client
        import sys
        monkeypatch.setitem(sys.modules, "docker", mock_docker)
        assert sandbox.is_docker_available() is True


# ──────────────────────────────────────────────────────────────────────────────
# run_in_sandbox — mocked Docker
# ──────────────────────────────────────────────────────────────────────────────

def _install_mock_docker(monkeypatch, *, container_mock: MagicMock, from_env_raises: Exception | None = None):
    """Install a fake `docker` module into sys.modules with a mocked
    `containers.run` returning `container_mock`. Also mocks ImageNotFound
    and APIError exception classes to avoid ImportError inside sandbox.py."""
    import sys

    class _ImageNotFound(Exception):
        pass

    class _APIError(Exception):
        pass

    mock_errors = MagicMock()
    mock_errors.ImageNotFound = _ImageNotFound
    mock_errors.APIError = _APIError

    mock_client = MagicMock()
    mock_client.containers.run.return_value = container_mock

    mock_docker = MagicMock()
    if from_env_raises is not None:
        mock_docker.from_env.side_effect = from_env_raises
    else:
        mock_docker.from_env.return_value = mock_client

    monkeypatch.setitem(sys.modules, "docker", mock_docker)
    monkeypatch.setitem(sys.modules, "docker.errors", mock_errors)
    return _ImageNotFound, _APIError


class TestRunInSandbox:
    def test_happy_path_returns_exit_code_and_streams(self, monkeypatch):
        container = MagicMock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.side_effect = [b"BUILD SUCCESS\n", b""]  # stdout, stderr
        container.attrs = {"State": {"OOMKilled": False}}
        _install_mock_docker(monkeypatch, container_mock=container)

        result = sandbox.run_in_sandbox(
            {"pom.xml": "<project/>"}, "mvn -q compile", image="maven:3",
        )
        assert result.exit_code == 0
        assert "BUILD SUCCESS" in result.stdout
        assert result.killed_for_oom is False
        assert result.killed_for_timeout is False
        assert result.image == "maven:3"
        container.remove.assert_called_once_with(force=True)

    def test_non_zero_exit_preserved(self, monkeypatch):
        container = MagicMock()
        container.wait.return_value = {"StatusCode": 1}
        container.logs.side_effect = [b"", b"compile error"]
        container.attrs = {"State": {"OOMKilled": False}}
        _install_mock_docker(monkeypatch, container_mock=container)

        result = sandbox.run_in_sandbox(
            {"pom.xml": "<bad/>"}, "mvn -q compile",
        )
        assert result.exit_code == 1
        assert "compile error" in result.stderr
        container.remove.assert_called_once_with(force=True)

    def test_timeout_marked_and_killed(self, monkeypatch):
        container = MagicMock()
        container.wait.side_effect = RuntimeError("read timeout")
        container.logs.return_value = b"partial output"
        container.attrs = {"State": {}}
        _install_mock_docker(monkeypatch, container_mock=container)

        result = sandbox.run_in_sandbox({"x.txt": "x"}, "sleep 9999", timeout=1)
        assert result.killed_for_timeout is True
        assert result.exit_code == -1
        assert "timeout" in result.stderr.lower()
        container.kill.assert_called_once()
        container.remove.assert_called_once_with(force=True)

    def test_oom_killed_detected(self, monkeypatch):
        container = MagicMock()
        container.wait.return_value = {"StatusCode": 137}
        container.logs.side_effect = [b"", b"killed"]
        # OOMKilled must appear after reload()
        container.reload = MagicMock()
        container.attrs = {"State": {"OOMKilled": True}}
        _install_mock_docker(monkeypatch, container_mock=container)

        result = sandbox.run_in_sandbox({"x.txt": "x"}, "python -c 'x=bytearray(10**9)'")
        assert result.killed_for_oom is True
        assert result.exit_code == 137

    def test_image_not_found_mapped_to_result(self, monkeypatch):
        mock_client = MagicMock()
        _INotFound, _ = _install_mock_docker(monkeypatch, container_mock=MagicMock())
        # Override: containers.run raises ImageNotFound
        import sys
        sys.modules["docker"].from_env.return_value.containers.run.side_effect = _INotFound()

        result = sandbox.run_in_sandbox({"x.txt": "x"}, "echo hi", image="missing:latest")
        assert result.exit_code == -1
        assert "image not found" in result.stderr.lower()
        assert result.image == "missing:latest"

    def test_from_env_failure_mapped_to_result(self, monkeypatch):
        _install_mock_docker(monkeypatch, container_mock=MagicMock(),
                             from_env_raises=PermissionError("docker socket unreadable"))
        result = sandbox.run_in_sandbox({"x.txt": "x"}, "echo hi")
        assert result.exit_code == -1
        assert "docker client init failed" in result.stderr

    def test_unsafe_path_raises_before_docker(self, monkeypatch):
        _install_mock_docker(monkeypatch, container_mock=MagicMock())
        with pytest.raises(ValueError, match="absolute"):
            sandbox.run_in_sandbox({"/etc/passwd": "x"}, "ls")

    def test_default_settings_applied(self, monkeypatch):
        """When caller omits image/timeout/memory/cpus, settings defaults flow
        through to _build_container_kwargs."""
        from app.core.config import settings
        monkeypatch.setattr(settings, "sandbox_image_java", "myimg:9")
        monkeypatch.setattr(settings, "sandbox_memory_limit", "256m")
        monkeypatch.setattr(settings, "sandbox_cpu_limit", 0.5)

        container = MagicMock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.side_effect = [b"ok", b""]
        container.attrs = {"State": {"OOMKilled": False}}
        _install_mock_docker(monkeypatch, container_mock=container)

        result = sandbox.run_in_sandbox({"x.txt": "x"}, "echo hi")
        # Assert the run call saw our settings defaults
        import sys
        run_kwargs = sys.modules["docker"].from_env.return_value.containers.run.call_args.kwargs
        assert run_kwargs["image"] == "myimg:9"
        assert run_kwargs["mem_limit"] == "256m"
        assert run_kwargs["nano_cpus"] == 500_000_000
        assert result.exit_code == 0


# ──────────────────────────────────────────────────────────────────────────────
# Integration — real Docker daemon required
# ──────────────────────────────────────────────────────────────────────────────

_skip_if_no_docker = pytest.mark.skipif(
    not sandbox.is_docker_available(),
    reason="Docker daemon not reachable; skipping live sandbox tests.",
)


@pytest.mark.sandbox
@_skip_if_no_docker
def test_integration_hello_world_alpine_runs():
    """Basic happy path: alpine echoes a message and exits 0."""
    result = sandbox.run_in_sandbox(
        repo_files={"marker.txt": "placeholder"},
        command=["sh", "-c", "echo hello sandbox"],
        image="alpine:3.20",
        timeout=30,
    )
    assert result.exit_code == 0
    assert "hello sandbox" in result.stdout
    assert result.killed_for_oom is False
    assert result.killed_for_timeout is False


@pytest.mark.sandbox
@_skip_if_no_docker
def test_integration_network_is_disabled():
    """With network_disabled=True, outbound connections must fail."""
    result = sandbox.run_in_sandbox(
        repo_files={"marker.txt": "x"},
        command=["sh", "-c",
                 "apk add --no-cache curl 2>/dev/null; "
                 "curl -s --max-time 3 https://example.com || echo NETWORK_BLOCKED"],
        image="alpine:3.20",
        timeout=45,
        network_disabled=True,
    )
    # Either apk add fails (no network to fetch), or curl fails. Either way,
    # we should NOT see a 200-OK-style success; the NETWORK_BLOCKED fallback
    # text must appear.
    assert "NETWORK_BLOCKED" in result.stdout or result.exit_code != 0


@pytest.mark.sandbox
@_skip_if_no_docker
def test_integration_oom_kill_flagged():
    """Allocating more than mem_limit must be killed with OOMKilled=True."""
    # Try to allocate ~200MB under a 32MB cap. The exact allocator varies
    # by image; using python for portability.
    result = sandbox.run_in_sandbox(
        repo_files={"marker.txt": "x"},
        command=["python", "-c",
                 "buf = bytearray(200 * 1024 * 1024); "
                 "print('allocated', len(buf))"],
        image="python:3.12-alpine",
        timeout=30,
        memory="32m",
    )
    # Either killed_for_oom is True, OR we got a non-zero exit code (some
    # kernels exit 137 without surfacing OOMKilled to the container state).
    assert result.killed_for_oom or result.exit_code != 0
