# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Unit tests for the code-repo delete endpoint.

Guards the FK-cascade fix: `remove_repo` must clear every child row that
references `code_repos.id` (none of those FKs declare ON DELETE CASCADE) before
deleting the repo, or Postgres raises an IntegrityError.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.api.code_indexing import remove_repo, _REPO_CHILD_TABLES


def _mock_db(repo):
    db = MagicMock()
    db.get.return_value = repo
    # chunk query chain → no chunks
    db.query.return_value.filter.return_value.filter.return_value.all.return_value = []
    # every DELETE reports one row removed
    db.execute.return_value = MagicMock(rowcount=1)
    return db


def _executed_tables(db):
    """Table name from each `DELETE FROM <t> WHERE repo_id = :rid` execute call."""
    tables = []
    for call in db.execute.call_args_list:
        sql = str(call.args[0]).lower()
        for tbl in _REPO_CHILD_TABLES:
            if f"delete from {tbl} " in sql:
                tables.append(tbl)
    return tables


class TestRemoveRepo:
    def test_clears_every_child_table_then_repo(self):
        repo = MagicMock(label="core-network")
        db = _mock_db(repo)

        result = remove_repo("repo-123", db, _=MagicMock())

        # Every FK/soft child table was targeted...
        assert set(_executed_tables(db)) == set(_REPO_CHILD_TABLES)
        # ...each scoped to this repo id...
        for call in db.execute.call_args_list:
            assert call.args[1] == {"rid": "repo-123"}
        # ...the repo row itself was deleted, and the txn committed.
        db.delete.assert_called_with(repo)
        db.commit.assert_called_once()
        assert result["deleted"] is True

    def test_child_delete_precedes_repo_delete(self):
        """FK order matters: children must be gone before the repo row."""
        repo = MagicMock(label="x")
        db = _mock_db(repo)
        order: list[str] = []
        db.execute.side_effect = lambda *a, **k: order.append("child") or MagicMock(rowcount=0)
        db.delete.side_effect = lambda obj: order.append("repo" if obj is repo else "chunk")

        remove_repo("r", db, _=MagicMock())

        assert order[-1] == "repo"                 # repo deleted last
        assert order.count("child") == len(_REPO_CHILD_TABLES)

    def test_404_when_repo_missing(self):
        db = MagicMock()
        db.get.return_value = None
        with pytest.raises(Exception) as exc:
            remove_repo("missing", db, _=MagicMock())
        assert "404" in str(exc.value) or "not found" in str(exc.value).lower()
        db.commit.assert_not_called()

    def test_leaf_children_ordered_before_parents(self):
        """xsd_java_links + xsd_schema_nodes must precede nothing that FKs them,
        and code_repos is never in the child list."""
        assert _REPO_CHILD_TABLES.index("xsd_java_links") < _REPO_CHILD_TABLES.index("xsd_schema_nodes")
        assert "code_repos" not in _REPO_CHILD_TABLES
