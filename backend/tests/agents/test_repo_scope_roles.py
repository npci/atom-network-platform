# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pack-declared repository topology validation."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agents import repo_scope
from app.agents.repo_scope import RepoSelectionError, validate_selection
from app.core.domain.contract import RepoRole
from app.models.code_repo import CodeRepo


def _repo(repo_id: str, label: str, role: str | None):
    return SimpleNamespace(
        id=repo_id,
        label=label,
        role=role,
        last_indexed_at=datetime.now(timezone.utc),
        chunks_count=1,
    )


class _DB:
    def __init__(self, *repos):
        self.repos = {repo.id: repo for repo in repos}

    def get(self, model, key):
        assert model is CodeRepo
        return self.repos.get(key)


def _pack(*roles):
    return SimpleNamespace(repo_roles=lambda: list(roles))


def test_no_declaration_preserves_existing_behavior(monkeypatch):
    repo = _repo("r1", "Unclassified Repo", None)
    monkeypatch.setattr(repo_scope, "get_active_pack", lambda: object())

    assert validate_selection(_DB(repo), ["r1", "r1"]) == [repo]
    with pytest.raises(RepoSelectionError) as exc:
        validate_selection(_DB(repo), ["missing"])
    assert str(exc.value) == "Unknown repo_id: missing"


def test_missing_required_role_raises(monkeypatch):
    app = _repo("app-1", "Payments App", "app")
    monkeypatch.setattr(repo_scope, "get_active_pack", lambda: _pack(
        RepoRole(key="core", required=True), RepoRole(key="app", required=True)))

    with pytest.raises(RepoSelectionError) as exc:
        validate_selection(_DB(app), [app.id])
    message = str(exc.value)
    assert "Payments App" in message and "core" in message
    assert "expected role keys: core, app" in message


def test_non_multiple_role_rejects_multiple_repos(monkeypatch):
    first = _repo("app-1", "Payments App A", "app")
    second = _repo("app-2", "Payments App B", "app")
    monkeypatch.setattr(repo_scope, "get_active_pack", lambda: _pack(
        RepoRole(key="app", required=True, multiple=False)))

    with pytest.raises(RepoSelectionError) as exc:
        validate_selection(_DB(first, second), [first.id, second.id])
    message = str(exc.value)
    assert "Payments App A" in message and "Payments App B" in message
    assert "expected role keys: app" in message


def test_repo_without_role_is_rejected_when_topology_declared(monkeypatch):
    repo = _repo("r1", "Unclassified Repo", None)
    monkeypatch.setattr(repo_scope, "get_active_pack", lambda: _pack(RepoRole(key="app")))

    with pytest.raises(RepoSelectionError) as exc:
        validate_selection(_DB(repo), [repo.id])
    message = str(exc.value)
    assert "Unclassified Repo" in message and "None" in message
    assert "expected role keys: app" in message
