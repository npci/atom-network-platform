# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SBOM finding 11 — CVE-2025-45768 (PyJWT, CVSS 6.3).

The CVE claims PyJWT does not enforce a minimum key length for HMAC signing.
It is disputed and has NO FIXED VERSION, so no upgrade can clear it — the only
honest way to close the finding is to prove this application enforces key
strength itself. `startup_validation.validate_jwt_key_strength` is that proof,
and docs/sbom/vex.json cites it by name. These tests are what make the citation
trustworthy: if the validator is ever weakened or deleted, the VEX statement
becomes a false assurance we formally signed, and that must fail loudly here.

THE MOST IMPORTANT TEST IN THIS FILE is
`test_entropy_findings_are_not_blocking`. The requirement driving this change
was explicitly "the existing functionality should not be affected", and this is
an AUTHENTICATION path — a new fail-closed check would refuse to boot any
environment holding a weak secret, converting a documentation gap into an
outage. So the new entropy/placeholder checks are severity "high" (logged
loudly, non-blocking) while length in non-dev remains the pre-existing hard
block in config.py. That distinction is the safety property, so it is asserted
directly rather than left to code review.
"""
from __future__ import annotations

import pytest

from app.core import startup_validation as sv


class _FakeSettings:
    """Minimal stand-in for the real Settings object.

    The validator reads exactly two attributes, so a stub keeps these tests
    independent of the ~2000-line real Settings class and of whatever .env
    happens to exist on the machine running them.
    """

    def __init__(self, secret_key: str, app_env: str = "production"):
        self.secret_key = secret_key
        self.app_env = app_env


@pytest.fixture
def patch_settings(monkeypatch):
    """Swap the settings object the validator imports.

    `validate_jwt_key_strength` does `from app.core.config import settings`
    INSIDE the function body, so the patch must target the config module
    attribute rather than a name already bound in startup_validation.
    """
    def _apply(secret_key: str, app_env: str = "production"):
        import app.core.config as config_mod
        monkeypatch.setattr(config_mod, "settings",
                            _FakeSettings(secret_key, app_env), raising=False)
    return _apply


# A realistic strong key: 64 URL-safe random characters.
_STRONG = "kJ8pQ2mN7xR4vT6yB9wZ3cF5gH1jL0aS8dE7fG6hK5nM4pQ3rT2vX1yZ0bC9dF8g"


# ── The strong-key baseline ───────────────────────────────────────────────────

def test_strong_key_produces_no_issues(patch_settings):
    """The happy path must be silent, or the signal is worthless."""
    patch_settings(_STRONG)
    assert sv.validate_jwt_key_strength() == []


@pytest.mark.parametrize("env", ["production", "uat", "staging"])
def test_strong_key_clean_in_every_non_dev_env(patch_settings, env):
    patch_settings(_STRONG, app_env=env)
    assert sv.validate_jwt_key_strength() == []


# ── The safety property: nothing new blocks boot ──────────────────────────────

def test_entropy_findings_are_not_blocking(patch_settings):
    """A low-entropy key must NOT be "critical".

    `run_all(fail_fast=True)` raises only on "critical", so this is precisely
    the assertion that a weak-but-long secret keeps the platform booting while
    shouting about it. Making this critical is a deliberate, scheduled
    follow-up once every environment is confirmed clean — NOT something to ship
    alongside the introduction of the check, because rotating secret_key signs
    out every logged-in user.
    """
    patch_settings("a" * 40)          # 40 chars, 1 distinct character
    issues = sv.validate_jwt_key_strength()
    assert issues, "a 1-distinct-character key must be reported"
    assert all(i.severity != "critical" for i in issues), (
        "entropy findings must not be critical — run_all(fail_fast=True) would "
        "refuse to boot, which is an outage on an auth path, not a fix"
    )
    assert any(i.severity == "high" for i in issues)


def test_placeholder_findings_are_not_blocking(patch_settings):
    patch_settings("changeme-" + "x" * 40)
    issues = sv.validate_jwt_key_strength()
    assert issues
    assert all(i.severity != "critical" for i in issues)


def test_run_all_does_not_raise_on_a_weak_but_long_key(monkeypatch, patch_settings):
    """End-to-end proof of the no-outage guarantee.

    Every other validator is stubbed out so this asserts one thing only: a weak
    JWT key does not, by itself, abort startup.
    """
    patch_settings("a" * 40)
    for name in (
        "_check_active_partners_have_hmac_secret",
        "validate_hostility_tier_config",
        "validate_precert_engine_tls",
        "validate_encryption_keys",
        "validate_hmac_fail_open",
        "validate_http_defaults",
    ):
        monkeypatch.setattr(sv, name, lambda: [], raising=True)

    issues = sv.run_all(fail_fast=True)          # must NOT raise
    assert any(i.check.startswith("jwt_secret_key") for i in issues)


# ── Low entropy ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    "a" * 40,                        # 1 distinct char
    "ab" * 20,                       # 2 distinct chars
    "abc" * 14,                      # 3 distinct chars
    "1234567" * 6,                   # 7 distinct chars — just under the floor
])
def test_low_entropy_keys_are_flagged(patch_settings, key):
    """Length is not entropy. Each of these passes a >=32-char gate and is
    still trivially guessable — the exact gap this validator exists to cover."""
    patch_settings(key)
    checks = {i.check for i in sv.validate_jwt_key_strength()}
    assert "jwt_secret_key_entropy" in checks, f"{key[:12]}... should be flagged"


def test_eight_distinct_characters_is_accepted(patch_settings):
    """Boundary: the threshold is `< 8`, so exactly 8 distinct chars passes.

    Pinned so a future tweak to the threshold is a visible decision rather than
    an accident.
    """
    patch_settings("12345678" * 5)   # 40 chars, exactly 8 distinct
    checks = {i.check for i in sv.validate_jwt_key_strength()}
    assert "jwt_secret_key_entropy" not in checks


# ── Placeholders ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "changeme", "change-me", "secret", "password", "please-change",
    "your-secret", "replace", "example", "dev-secret", "test-secret",
    "insecure", "placeholder", "todo",
])
def test_placeholder_text_is_flagged(patch_settings, bad):
    """Anything derived from published boilerplate must be treated as public.

    Padded to a passing length so the placeholder check is what fires, not the
    length check.
    """
    patch_settings(bad + "Zq7Xr2Np9Kf4Wt6Yb3Cv8Md1Gh5Ls0Aj" * 2)
    checks = {i.check for i in sv.validate_jwt_key_strength()}
    assert "jwt_secret_key_placeholder" in checks, f"{bad!r} should be flagged"


def test_placeholder_match_is_case_insensitive(patch_settings):
    patch_settings("ChangeMe" + "Zq7Xr2Np9Kf4Wt6Yb3Cv8Md1Gh5Ls0Aj" * 2)
    checks = {i.check for i in sv.validate_jwt_key_strength()}
    assert "jwt_secret_key_placeholder" in checks


def test_strong_key_is_not_falsely_flagged_as_placeholder(patch_settings):
    """Guards against a substring that is too aggressive to be useful."""
    patch_settings(_STRONG)
    checks = {i.check for i in sv.validate_jwt_key_strength()}
    assert "jwt_secret_key_placeholder" not in checks


# ── Length, and the DB-override gap this validator closes ────────────────────

def test_short_key_in_production_is_critical(patch_settings):
    """Reaching here with a short key means it bypassed config.py's validator.

    config.py checks at Settings CONSTRUCTION (.env only), but `app.main` calls
    `load_db_overrides()` BEFORE `run_all()` — so an Admin-UI-set value can land
    a secret the pydantic validator never saw. That is a genuine hole and is
    correctly critical: it was always meant to be blocked, and this is not a
    new restriction on any environment that was previously booting legitimately.
    """
    patch_settings("short")
    issues = sv.validate_jwt_key_strength()
    assert any(i.check == "jwt_secret_key_length" and i.severity == "critical"
               for i in issues)


def test_short_key_reports_only_length_not_entropy(patch_settings):
    """Fail on one clear cause. Piling an entropy complaint on top of a length
    complaint for the same value just makes the remediation list noisy."""
    patch_settings("abc")
    checks = {i.check for i in sv.validate_jwt_key_strength()}
    assert checks == {"jwt_secret_key_length"}


def test_thirty_two_characters_is_the_boundary(patch_settings):
    """31 fails, 32 passes — mirrors config.py's threshold exactly rather than
    introducing a second, subtly different number."""
    patch_settings("Zq7Xr2Np9Kf4Wt6Yb3Cv8Md1Gh5Ls0A")          # 31
    assert any(i.check == "jwt_secret_key_length"
               for i in sv.validate_jwt_key_strength())

    patch_settings("Zq7Xr2Np9Kf4Wt6Yb3Cv8Md1Gh5Ls0Aj")         # 32
    assert not any(i.check == "jwt_secret_key_length"
                   for i in sv.validate_jwt_key_strength())


# ── Development stays permissive ──────────────────────────────────────────────

def test_development_short_key_warns_but_never_blocks(patch_settings):
    """Local development must not be made painful by this change.

    config.py deliberately skips its hard block in development; this mirrors
    that, but still warns so a weak key does not travel from a laptop into a
    shared .env unnoticed.
    """
    patch_settings("short", app_env="development")
    issues = sv.validate_jwt_key_strength()
    assert issues
    assert all(i.severity == "warning" for i in issues)


def test_development_ignores_entropy_and_placeholders(patch_settings):
    """Dev returns early: a 'changeme' secret locally is normal and reporting
    it every boot would train people to ignore startup output."""
    patch_settings("changeme" * 5, app_env="development")
    assert sv.validate_jwt_key_strength() == []


def test_development_strong_key_is_silent(patch_settings):
    patch_settings(_STRONG, app_env="development")
    assert sv.validate_jwt_key_strength() == []


# ── The VEX statement's integrity ─────────────────────────────────────────────

def test_validator_is_registered_in_run_all():
    """docs/sbom/vex.json cites this validator as the control that makes
    CVE-2025-45768 not_affected. A validator that exists but is never called
    would make that statement false, and a scanner cannot detect the
    difference — only this test can.
    """
    import inspect
    source = inspect.getsource(sv.run_all)
    assert "validate_jwt_key_strength()" in source, (
        "validate_jwt_key_strength must be wired into run_all() — the VEX "
        "statement for CVE-2025-45768 in docs/sbom/vex.json depends on it "
        "actually running"
    )
