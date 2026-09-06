# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for user-fixable infra/config error classification (§18)."""
from app.agents.infra_errors import classify_infra_error


def test_real_gitlab_auth_failure_is_classified():
    # the exact error from the wedged run b6b170a6
    err = ("git push failed after 3 attempts: fatal: could not read Username "
           "for 'https://gitlab.com': No such device or address")
    issue = classify_infra_error(err)
    assert issue is not None and issue.code == "GIT_REMOTE_AUTH"
    assert issue.retryable is True
    # the hint is actionable and does NOT reference a non-existent UI button
    assert "Retry" in issue.fix
    assert "download" not in issue.fix.lower()


def test_unreachable_remote_is_classified():
    assert classify_infra_error("fatal: could not resolve host: gitlab.internal").code == "GIT_REMOTE_UNREACHABLE"


def test_disk_full_is_classified():
    assert classify_infra_error("OSError: [Errno 28] No space left on device").code == "DISK_FULL"


def test_missing_toolchain_is_classified():
    assert classify_infra_error("/bin/sh: 1: mvn: not found").code == "TOOLCHAIN_MISSING"


def test_genuine_code_error_is_not_a_config_issue():
    # a real bug/compile failure is NOT user-fixable-by-config → None (don't mislabel it)
    assert classify_infra_error("NullPointerException at com.example.Foo.bar(Foo.java:42)") is None
    assert classify_infra_error("COMPILATION ERROR: cannot find symbol") is None


def test_empty_is_none():
    assert classify_infra_error("") is None
    assert classify_infra_error(None) is None
