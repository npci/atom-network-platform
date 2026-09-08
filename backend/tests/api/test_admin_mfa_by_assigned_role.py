# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""T7 — the admin MFA challenge must key off ASSIGNED roles, not the active one.

The bypass this pins shut:

    user is assigned {product_manager, admin}, active role product_manager
      -> POST /auth/login          password only, no OTP  (active role != admin)
      -> POST /auth/switch-role    active role becomes admin
      -> full admin authority on every route that checks the role INLINE
         (governance.py, jobs.py, eval.py, emergency_issues.py, phase_b.py)
         rather than via `require_admin`, none of which inspect `amr`.

`require_admin` alone did not close this: it asserts amr == "pwd+mfa", but it is
not what those routes call. Challenging at login for anyone *assigned* admin is
what makes the assertion true for every path.
"""
import pytest

pytest.importorskip("fastapi")

from app.models.user import UserRole                     # noqa: E402


class _Assignment:
    def __init__(self, role):
        self.role = role


class _User:
    """Mirrors the real model's `roles` property (active role always included)."""

    def __init__(self, active, assigned=()):
        self.role = active
        self.role_assignments = [_Assignment(r) for r in assigned]

    @property
    def roles(self):
        s = {ra.role for ra in self.role_assignments}
        s.add(self.role)
        return sorted(s, key=lambda r: r.value)


def admin_mfa_required(user, *, setting=True):
    """The predicate as it now reads in api/auth.login."""
    return UserRole.ADMIN in user.roles and setting


def test_admin_assigned_but_not_active_is_challenged():
    """The actual bypass: PM-active, admin-assigned. Previously False."""
    u = _User(UserRole.PRODUCT_MANAGER, assigned=[UserRole.ADMIN])
    assert admin_mfa_required(u) is True


def test_admin_active_still_challenged():
    """Strictly wider than the old test — nothing previously challenged stops."""
    u = _User(UserRole.ADMIN)
    assert admin_mfa_required(u) is True


def test_non_admin_user_is_not_challenged():
    """Guard against the predicate going vacuous by simply returning True."""
    u = _User(UserRole.PRODUCT_MANAGER, assigned=[UserRole.TECH_LEAD])
    assert admin_mfa_required(u) is False


def test_setting_can_still_disable_it():
    """`admin_mfa_required=False` remains the documented break-glass override."""
    u = _User(UserRole.PRODUCT_MANAGER, assigned=[UserRole.ADMIN])
    assert admin_mfa_required(u, setting=False) is False


def test_the_old_predicate_would_have_missed_it():
    """Documents the regression directly: the previous active-role-only test
    returns False for exactly the account that can reach admin."""
    u = _User(UserRole.PRODUCT_MANAGER, assigned=[UserRole.ADMIN])
    old = (u.role == UserRole.ADMIN)
    assert old is False and admin_mfa_required(u) is True


def test_login_uses_the_assigned_role_form():
    """Source-level pin: the fix lives inside `login`, which needs a DB and a
    request to call, so assert on the expression actually shipped. If someone
    reverts to `user.role == UserRole.ADMIN`, this fails."""
    import inspect

    from app.api import auth

    src = inspect.getsource(auth.login)
    assert "UserRole.ADMIN in user.roles" in src, (
        "login() no longer derives the admin MFA challenge from ASSIGNED roles"
    )
    assert "user.role == UserRole.ADMIN and getattr(settings" not in src, (
        "login() has reverted to the active-role-only MFA test (T7 bypass)"
    )
