# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Authorization contract for the Change-Analysis flow (PM access + collaborative gating).

Covers the role model that drives who can drive the analysis: planning roles (admin,
tech-lead, PM, product-owner) collaborate on an analysis run regardless of who started
it, while xsd/code runs stay author-or-admin (maker-checker)."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.agentic import _authz_analysis, _authz_read, _authz_resume
from app.models.user import UserRole

_PLANNING = (UserRole.ADMIN, UserRole.TECH_LEAD, UserRole.PRODUCT_MANAGER, UserRole.PRODUCT_OWNER)
_NON_PLANNING = (UserRole.INFOSEC_REVIEWER, UserRole.RISK_REVIEWER)


def _user(role, uid="u1"):
    return SimpleNamespace(role=role, id=uid)


def _run(kind="analysis", created_by="owner1"):
    return SimpleNamespace(kind=kind, created_by=created_by)


def test_authz_analysis_allows_all_planning_roles():
    for role in _PLANNING:
        _authz_analysis(_user(role))          # must NOT raise


def test_authz_analysis_rejects_non_planning_roles():
    for role in _NON_PLANNING:
        with pytest.raises(HTTPException):
            _authz_analysis(_user(role))


def test_pm_can_read_analysis_run_they_did_not_author():
    # The analysis is collaborative (PM=functional, tech-lead=technical), so any planning
    # role may read it even if someone else started it.
    run = _run(kind="analysis", created_by="someone_else")
    _authz_read(run, _user(UserRole.PRODUCT_MANAGER, uid="pm"))
    _authz_read(run, _user(UserRole.TECH_LEAD, uid="tl"))


def test_code_run_stays_author_or_admin():
    run = _run(kind="code", created_by="owner1")
    _authz_read(run, _user(UserRole.TECH_LEAD, uid="owner1"))   # author → ok
    _authz_read(run, _user(UserRole.ADMIN, uid="admin"))         # admin → ok
    with pytest.raises(HTTPException):                            # PM non-author → denied
        _authz_read(run, _user(UserRole.PRODUCT_MANAGER, uid="pm"))


def test_pm_can_retry_analysis_run_they_did_not_author():
    # The dead-end fix: a PM whose analysis crashed can retry it themselves (collaborative),
    # without needing an admin/tech-lead to un-stick it.
    run = _run(kind="analysis", created_by="someone_else")
    _authz_resume(run, _user(UserRole.PRODUCT_MANAGER, uid="pm"))   # must NOT raise
    _authz_resume(run, _user(UserRole.PRODUCT_OWNER, uid="po"))


def test_resume_rejects_non_planning_roles_on_analysis():
    run = _run(kind="analysis", created_by="someone_else")
    for role in _NON_PLANNING:
        with pytest.raises(HTTPException):
            _authz_resume(run, _user(role))


def test_resume_code_run_stays_admin_or_techlead_author():
    # Non-analysis runs keep the stricter gate: admin/tech-lead role AND author-or-admin.
    run = _run(kind="code", created_by="owner1")
    _authz_resume(run, _user(UserRole.TECH_LEAD, uid="owner1"))     # author tech-lead → ok
    _authz_resume(run, _user(UserRole.ADMIN, uid="admin"))           # admin → ok
    with pytest.raises(HTTPException):                               # PM → denied (wrong role)
        _authz_resume(run, _user(UserRole.PRODUCT_MANAGER, uid="pm"))
    with pytest.raises(HTTPException):                               # tech-lead non-author → denied
        _authz_resume(run, _user(UserRole.TECH_LEAD, uid="other"))
