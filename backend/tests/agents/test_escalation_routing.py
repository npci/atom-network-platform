# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Escalation routing — team normalization + inbox role-gating (Slice 1).

Pure / fake-DB unit tests: no real DB or LLM. Mirrors the style of
tests/eval/test_eval_policy_audit.py (env stubs + FakeDb).
"""
from __future__ import annotations

import os
import types

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
from tests._optional_stubs import stub_jwt, stub_pgvector

stub_jwt()
stub_pgvector()

from app.agents.feasibility_resolver import _normalize_team
from app.api.escalations import _teams_for
from app.models.user import UserRole


class TestNormalizeTeam:
    @pytest.mark.parametrize("raw,expected", [
        ("risk", "risk"),
        ("infosec", "infosec"),
        ("tech", "tech"),
        ("Risk ", "risk"),
        ("SECURITY", "infosec"),
        ("architecture", "tech"),
        # Legacy resolver targets map onto the three-team model.
        ("compliance", "risk"),
        ("legal", "risk"),
        ("svp", "tech"),
    ])
    def test_maps_to_one_of_three(self, raw, expected):
        assert _normalize_team(raw) == expected

    def test_unknown_and_empty_default_to_tech(self):
        assert _normalize_team("gibberish") == "tech"
        assert _normalize_team("") == "tech"
        assert _normalize_team(None) == "tech"


class TestInboxTeamGating:
    def _user(self, role):
        return types.SimpleNamespace(role=role)

    def test_reviewers_see_only_their_team(self):
        assert _teams_for(self._user(UserRole.RISK_REVIEWER)) == {"risk"}
        assert _teams_for(self._user(UserRole.INFOSEC_REVIEWER)) == {"infosec"}
        assert _teams_for(self._user(UserRole.TECH_LEAD)) == {"tech"}

    def test_pm_po_admin_oversee_all(self):
        for role in (UserRole.PRODUCT_MANAGER, UserRole.PRODUCT_OWNER, UserRole.ADMIN):
            assert _teams_for(self._user(role)) == {"risk", "infosec", "tech"}
