# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""UAT trigger helpers — script resolution ladder, base_url SSRF guard, and
the staleness window.

The load-bearing contracts: the configured PHASE_B_TEST_SCRIPT default must
work in ANY runner mode for ANY authenticated operator (elevation guards
CHOOSING a script, not running the default), base_url must go through the
platform's ssrf_guard (loopback/metadata refused; the operator allowlist is
the escape hatch), and the re-trigger staleness window must track the
configurable script timeout so a long-but-legal run cannot age out of it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.phase_b import (
    UatTestTriggerRequest, _base_url_or_400, _stale_after_s, _uat_script_or_400,
)
from app.core.config import settings
from app.models.user import UserRole

ADMIN = SimpleNamespace(id="u-admin", role=UserRole.ADMIN)
PM = SimpleNamespace(id="u-pm", role=UserRole.PRODUCT_MANAGER)


# ── _uat_script_or_400: request path vs configured default ───────────────────

def test_configured_default_runs_for_any_role_in_any_mode(tmp_path, monkeypatch):
    script = tmp_path / "uat.sh"
    script.write_text("#!/bin/bash\necho ok\n")
    monkeypatch.setattr(settings, "phase_b_test_script", str(script), raising=False)
    monkeypatch.setattr(settings, "phase_b_runner_mode", "ssh", raising=False)
    out = _uat_script_or_400(UatTestTriggerRequest(), PM)
    assert out == str(script)


def test_no_script_anywhere_is_a_loud_400(monkeypatch):
    monkeypatch.setattr(settings, "phase_b_test_script", "", raising=False)
    with pytest.raises(HTTPException) as exc:
        _uat_script_or_400(UatTestTriggerRequest(), ADMIN)
    assert exc.value.status_code == 400
    assert "PHASE_B_TEST_SCRIPT" in exc.value.detail


def test_missing_configured_script_fails_loud_without_leaking_the_path(tmp_path, monkeypatch):
    gone = str(tmp_path / "nope.sh")
    monkeypatch.setattr(settings, "phase_b_test_script", gone, raising=False)
    with pytest.raises(HTTPException) as exc:
        _uat_script_or_400(UatTestTriggerRequest(), PM)
    assert exc.value.status_code == 400
    assert gone not in exc.value.detail          # path stays server-side (logs)


def test_request_supplied_script_requires_elevated_role(monkeypatch):
    monkeypatch.setattr(settings, "phase_b_runner_mode", "local", raising=False)
    with pytest.raises(HTTPException) as exc:
        _uat_script_or_400(UatTestTriggerRequest(script_path="x.sh"), PM)
    assert exc.value.status_code == 403


def test_request_supplied_script_requires_local_mode(monkeypatch):
    monkeypatch.setattr(settings, "phase_b_runner_mode", "ssh", raising=False)
    with pytest.raises(HTTPException) as exc:
        _uat_script_or_400(UatTestTriggerRequest(script_path="x.sh"), ADMIN)
    assert exc.value.status_code == 400
    assert "local" in exc.value.detail


def test_request_supplied_script_resolves_against_the_allowlist_root(tmp_path, monkeypatch):
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "run.sh").write_text("#!/bin/bash\n")
    monkeypatch.setattr(settings, "phase_b_runner_mode", "local", raising=False)
    monkeypatch.setattr(settings, "phase_b_script_root", str(root), raising=False)
    out = _uat_script_or_400(UatTestTriggerRequest(script_path="run.sh"), ADMIN)
    assert out == str((root / "run.sh").resolve())


# ── _base_url_or_400: ssrf_guard is in the path ──────────────────────────────

def _enforce(monkeypatch):
    monkeypatch.setattr(settings, "ssrf_guard_mode", "enforce", raising=False)
    monkeypatch.setattr(settings, "ssrf_allowed_internal_hosts", "", raising=False)
    monkeypatch.setattr(settings, "ssrf_allow_private_networks", False, raising=False)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/",            # loopback
    "http://169.254.169.254/latest/",    # cloud metadata
    "http://0x7f000001/",                # numeric loopback spelling
])
def test_internal_targets_are_refused(monkeypatch, url):
    _enforce(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _base_url_or_400(url)
    assert exc.value.status_code == 400
    assert "refused" in exc.value.detail


def test_allowlisted_internal_host_passes(monkeypatch):
    _enforce(monkeypatch)
    monkeypatch.setattr(settings, "ssrf_allowed_internal_hosts",
                        "uat.example.internal", raising=False)
    assert _base_url_or_400("https://uat.example.internal/api") \
        == "https://uat.example.internal/api"


def test_public_literal_ip_passes(monkeypatch):
    _enforce(monkeypatch)
    assert _base_url_or_400("https://93.184.216.34/") == "https://93.184.216.34/"


def test_empty_base_url_is_none(monkeypatch):
    _enforce(monkeypatch)
    assert _base_url_or_400(None) is None
    assert _base_url_or_400("   ") is None


@pytest.mark.parametrize("url", ["ftp://x/", "not-a-url", "http://", "http://a b/"])
def test_malformed_urls_are_rejected_before_the_guard(monkeypatch, url):
    _enforce(monkeypatch)
    with pytest.raises(HTTPException):
        _base_url_or_400(url)


# ── staleness window tracks the configurable timeout ─────────────────────────

def test_stale_window_exceeds_the_script_timeout(monkeypatch):
    monkeypatch.setattr(settings, "phase_b_script_timeout_seconds", 4 * 3600, raising=False)
    assert _stale_after_s() == 4 * 3600 + 1800
    monkeypatch.setattr(settings, "phase_b_script_timeout_seconds", 0, raising=False)
    assert _stale_after_s() == 60 + 1800
