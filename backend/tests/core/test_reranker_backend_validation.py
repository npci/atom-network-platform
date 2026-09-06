# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SBOM findings 2 / 12-14 / 17-18 — the reranker must not degrade SILENTLY.

torch and sentence-transformers were removed from the backend image on
2026-08-28 (six findings, including CVE-2026-68770 at CVSS 9.8) and the model
moved to the `reranker` sidecar. The capability was preserved via the remote
backend — but the reranker FAILS OPEN by design, which creates a specific
hazard worth testing:

    if reranker_backend is left at "local", the in-process path tries to import
    a sentence-transformers that is no longer installed, catches the failure,
    and returns un-reranked RRF order. Search keeps working. Nothing errors.
    The +5-15pp recall@10 just quietly disappears.

`validate_reranker_backend` converts that silent quality loss into a visible
startup signal. These tests pin both halves of its contract: it must SPEAK UP
when the configuration would degrade, and it must STAY QUIET otherwise — a
validator that cries wolf on healthy configs gets ignored, which would defeat
the purpose entirely.
"""
from __future__ import annotations

import pytest

from app.core import startup_validation as sv


class _FakeSettings:
    def __init__(self, use_reranker=False, backend="remote", url="", env="production"):
        self.use_reranker = use_reranker
        self.reranker_backend = backend
        self.reranker_url = url
        self.app_env = env


@pytest.fixture
def patch_settings(monkeypatch):
    def _apply(**kwargs):
        import app.core.config as config_mod
        monkeypatch.setattr(config_mod, "settings", _FakeSettings(**kwargs), raising=False)
    return _apply


_GOOD_URL = "http://reranker:8200/rerank"


# ── Silence when the feature is off ──────────────────────────────────────────

@pytest.mark.parametrize("backend", ["local", "remote", "nonsense"])
def test_no_issues_when_reranker_disabled(patch_settings, backend):
    """use_reranker defaults to False. An unused feature being unconfigured is
    not a problem, and warning about it on every boot would train operators to
    ignore startup output — which is the one thing this validator cannot afford.
    """
    patch_settings(use_reranker=False, backend=backend, url="")
    assert sv.validate_reranker_backend() == []


# ── The dangerous combination ────────────────────────────────────────────────

def test_local_backend_with_reranker_on_is_flagged(patch_settings):
    """THE case this validator exists for.

    "local" is what the setting used to default to, so an environment that
    pinned it explicitly, or a stale .env, lands here. Before the split this
    was correct; after it, it means every rerank call fails open.
    """
    patch_settings(use_reranker=True, backend="local")
    issues = sv.validate_reranker_backend()
    checks = {i.check for i in issues}
    assert "reranker_backend_local_without_model_libs" in checks


def test_local_backend_issue_is_high_not_critical(patch_settings):
    """Must NOT block boot.

    `run_all(fail_fast=True)` raises only on "critical". Refusing to start
    because an OPTIONAL search-quality enhancement is misconfigured would be a
    worse outcome than the degradation it reports.
    """
    patch_settings(use_reranker=True, backend="local")
    issues = sv.validate_reranker_backend()
    assert issues
    assert all(i.severity != "critical" for i in issues)
    assert any(i.severity == "high" for i in issues)


def test_local_backend_message_tells_the_operator_what_to_do(patch_settings):
    """A warning that does not say how to fix the problem gets ignored. The
    remediation is two settings and one compose command, so the message states
    all three rather than making someone go read the code.
    """
    patch_settings(use_reranker=True, backend="local")
    detail = " ".join(i.detail for i in sv.validate_reranker_backend())
    assert "RERANKER_BACKEND=remote" in detail
    assert "RERANKER_URL" in detail
    assert "--profile reranker" in detail


# ── Remote backend, missing URL ──────────────────────────────────────────────

def test_remote_backend_without_url_is_flagged(patch_settings):
    """`_rerank_remote` logs one warning and falls back on EVERY call when the
    URL is empty — high-volume noise that is easy to miss in aggregate. Better
    to say it once, clearly, at startup.
    """
    patch_settings(use_reranker=True, backend="remote", url="")
    checks = {i.check for i in sv.validate_reranker_backend()}
    assert "reranker_url_unset" in checks


def test_remote_backend_with_whitespace_only_url_is_flagged(patch_settings):
    """`_rerank_remote` does `.strip()` before testing truthiness, so a
    whitespace-only value behaves exactly like an empty one. The validator must
    agree with the code path it is describing.
    """
    patch_settings(use_reranker=True, backend="remote", url="   ")
    checks = {i.check for i in sv.validate_reranker_backend()}
    assert "reranker_url_unset" in checks


def test_remote_backend_url_issue_is_not_blocking(patch_settings):
    patch_settings(use_reranker=True, backend="remote", url="")
    issues = sv.validate_reranker_backend()
    assert issues
    assert all(i.severity != "critical" for i in issues)


# ── The correct configuration is silent ──────────────────────────────────────

def test_properly_configured_remote_backend_is_silent(patch_settings):
    """The whole point: a healthy config produces NO output. Otherwise the
    validator is noise and operators stop reading it.
    """
    patch_settings(use_reranker=True, backend="remote", url=_GOOD_URL)
    assert sv.validate_reranker_backend() == []


def test_backend_value_is_case_and_whitespace_insensitive(patch_settings):
    """`rerank()` normalises with `.strip().lower()`, so the validator must too
    — otherwise "REMOTE" would be reported as broken while working fine.
    """
    patch_settings(use_reranker=True, backend="  REMOTE  ", url=_GOOD_URL)
    assert sv.validate_reranker_backend() == []


# ── Unknown values ───────────────────────────────────────────────────────────

def test_unknown_backend_is_reported(patch_settings):
    """`rerank()` falls back to "local" on an unrecognised value, which now
    means "fails open". Worth saying, but a typo is a lesser problem than a
    plausible-looking wrong setting, so it is only a warning.
    """
    patch_settings(use_reranker=True, backend="grpc", url=_GOOD_URL)
    issues = sv.validate_reranker_backend()
    checks = {i.check for i in issues}
    assert "reranker_backend_unknown" in checks
    assert all(i.severity != "critical" for i in issues)


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_validator_is_registered_in_run_all():
    """A validator that is never called protects nothing, and no scanner or
    type checker can detect the omission."""
    import inspect
    assert "validate_reranker_backend()" in inspect.getsource(sv.run_all)


def test_run_all_does_not_raise_on_misconfigured_reranker(monkeypatch, patch_settings):
    """End-to-end: the degraded-reranker state must never abort startup."""
    patch_settings(use_reranker=True, backend="local")
    for name in (
        "_check_active_partners_have_hmac_secret",
        "validate_hostility_tier_config",
        "validate_precert_engine_tls",
        "validate_jwt_key_strength",
        "validate_encryption_keys",
        "validate_hmac_fail_open",
        "validate_http_defaults",
    ):
        monkeypatch.setattr(sv, name, lambda: [], raising=True)

    issues = sv.run_all(fail_fast=True)          # must NOT raise
    assert any(i.check.startswith("reranker_") for i in issues)


# ── The default must stay "remote" ───────────────────────────────────────────

def test_config_default_backend_is_remote():
    """Guards the topology decision itself.

    If this default reverts to "local", every deployment that does not
    explicitly override it silently loses reranking — because the model
    libraries are not in this image any more. That is precisely the regression
    the SBOM change must not cause, so it is asserted against the real Settings
    class rather than trusted to a comment.
    """
    from app.core.config import Settings
    assert Settings.model_fields["reranker_backend"].default == "remote", (
        "reranker_backend must default to 'remote': torch/sentence-transformers "
        "are no longer installed in the backend image, so 'local' fails open"
    )
