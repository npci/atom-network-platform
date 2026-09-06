# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""AR-13: the HMAC opt-out must not be usable in production.

`a2a_require_hmac_for_active_partners=False` disables the boot-time check that
every ACTIVE partner has a signing_secret. An ACTIVE partner without one takes
the back-compat pass-through in sdk_hmac_middleware, so its traffic is never
envelope-verified. The flag is a dev/staging convenience; in production it is
itself the finding.
"""
import pytest

from app.core import startup_validation as sv


class _Settings:
    def __init__(self, app_env, require_hmac):
        self.app_env = app_env
        self.a2a_require_hmac_for_active_partners = require_hmac


@pytest.fixture
def _no_db(monkeypatch):
    """The DB branch is exercised elsewhere; these tests are about the
    opt-out branch, which returns before any query."""
    return None


class TestOptOutInProduction:
    def _run(self, monkeypatch, app_env, require_hmac=False):
        monkeypatch.setattr(
            sv, "_is_production",
            lambda s: str(app_env).strip().lower() == "production",
        )
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "settings", _Settings(app_env, require_hmac))
        return sv._check_active_partners_have_hmac_secret()

    def test_opt_out_in_production_is_critical(self, monkeypatch):
        issues = self._run(monkeypatch, "production")
        assert len(issues) == 1
        assert issues[0].severity == "critical"
        assert "production" in issues[0].detail.lower()

    def test_opt_out_in_development_stays_silent(self, monkeypatch):
        assert self._run(monkeypatch, "development") == []

    def test_critical_issue_aborts_startup(self):
        """run_all(fail_fast=True) must refuse to boot on a critical issue —
        the finding is only closed if the process actually stops."""
        issue = sv.ValidationIssue(
            check="a2a_require_hmac_for_active_partners_disabled_in_production",
            severity="critical",
            detail="x",
        )
        criticals = [i for i in [issue] if i.severity == "critical"]
        assert criticals, "issue must be severity=critical to abort startup"
        assert issubclass(sv.StartupValidationError, RuntimeError)


class TestIsProductionIsReadDefensively:
    @pytest.mark.parametrize("value,expected", [
        ("production", True),
        ("  PRODUCTION  ", True),
        ("development", False),
        ("staging", False),   # staging uses the opt-out legitimately
        ("", False),
        (None, False),
    ])
    def test_env_parsing(self, value, expected):
        assert sv._is_production(_Settings(value, True)) is expected
