# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pure-logic tests for repo scoping (§5). DB-backed behaviour (validate_selection,
indexed_sha, is_stale, chunk_scope_filter) is covered by the S3 integration smoke."""
import pytest

from app.agents.repo_scope import (
    RepoSelectionError,
    is_repo_selected,
    assert_repo_selected,
)

SELECTED = ["repo-a", "repo-b"]


def test_is_repo_selected():
    assert is_repo_selected("repo-a", SELECTED) is True
    assert is_repo_selected("repo-z", SELECTED) is False
    assert is_repo_selected(None, SELECTED) is False
    assert is_repo_selected("repo-a", []) is False


def test_assert_repo_selected_passes_for_member():
    assert_repo_selected("repo-b", SELECTED)  # no raise


def test_assert_repo_selected_rejects_outsider():
    with pytest.raises(RepoSelectionError):
        assert_repo_selected("repo-z", SELECTED)
    with pytest.raises(RepoSelectionError):
        assert_repo_selected(None, SELECTED)  # the dropped "primary repo" fallback


def test_repo_selection_error_is_value_error():
    # S13 maps this to HTTP 400; keep it a ValueError subclass.
    assert issubclass(RepoSelectionError, ValueError)
