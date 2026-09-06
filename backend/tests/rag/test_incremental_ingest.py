# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 26 — event-sourced incremental ingest.

Layered:
  1. `_compute_file_hash` (pure)
  2. `diff_against_state` (pure — the heart of the slice)
  3. Endpoint validation (404 / 400) using the existing patterns from
     Slice 22c's polyglot endpoint tests
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.rag.code_ingestion import _compute_file_hash, diff_against_state


# ──────────────────────────────────────────────────────────────────────────────
# _compute_file_hash
# ──────────────────────────────────────────────────────────────────────────────

class TestComputeFileHash:

    def test_deterministic_for_same_content(self):
        assert _compute_file_hash("hello") == _compute_file_hash("hello")

    def test_different_content_different_hash(self):
        assert _compute_file_hash("hello") != _compute_file_hash("hello!")

    def test_empty_string_has_known_sha256(self):
        # SHA256 of b"" is a well-known constant
        assert _compute_file_hash("") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_none_treated_as_empty(self):
        assert _compute_file_hash(None) == _compute_file_hash("")  # type: ignore[arg-type]

    def test_bytes_input_supported(self):
        assert _compute_file_hash(b"hello") == _compute_file_hash("hello")

    def test_hash_is_64_hex_chars(self):
        h = _compute_file_hash("anything")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ──────────────────────────────────────────────────────────────────────────────
# diff_against_state
# ──────────────────────────────────────────────────────────────────────────────

class TestDiffAgainstState:

    def _f(self, path: str, content: str = "") -> dict:
        return {"path": path, "content": content, "language": "python"}

    def test_first_run_all_files_are_added(self):
        files = [self._f("a.py", "x = 1"), self._f("b.py", "y = 2")]
        diff = diff_against_state(files, prior_state={})
        assert len(diff["added"]) == 2
        assert diff["modified"] == []
        assert diff["unchanged"] == []
        assert diff["deleted"] == []

    def test_unchanged_files_when_hashes_match(self):
        files = [self._f("a.py", "x = 1")]
        prior = {"a.py": _compute_file_hash("x = 1")}
        diff = diff_against_state(files, prior)
        assert len(diff["unchanged"]) == 1
        assert diff["modified"] == []
        assert diff["added"] == []
        assert diff["deleted"] == []

    def test_modified_when_hash_differs(self):
        files = [self._f("a.py", "x = 999")]
        prior = {"a.py": _compute_file_hash("x = 1")}
        diff = diff_against_state(files, prior)
        assert len(diff["modified"]) == 1
        assert diff["modified"][0]["path"] == "a.py"
        assert diff["unchanged"] == []
        assert diff["added"] == []

    def test_deleted_when_in_prior_but_not_in_files(self):
        files = [self._f("a.py", "x")]
        prior = {
            "a.py": _compute_file_hash("x"),
            "b.py": _compute_file_hash("y"),
        }
        diff = diff_against_state(files, prior)
        assert diff["deleted"] == ["b.py"]

    def test_mixed_classification(self):
        files = [
            self._f("a.py", "unchanged"),    # unchanged
            self._f("b.py", "new content"),  # modified
            self._f("c.py", "brand new"),    # added
        ]
        prior = {
            "a.py": _compute_file_hash("unchanged"),
            "b.py": _compute_file_hash("old content"),
            "d.py": _compute_file_hash("gone"),  # deleted
        }
        diff = diff_against_state(files, prior)
        assert {f["path"] for f in diff["unchanged"]} == {"a.py"}
        assert {f["path"] for f in diff["modified"]} == {"b.py"}
        assert {f["path"] for f in diff["added"]} == {"c.py"}
        assert diff["deleted"] == ["d.py"]

    def test_each_classified_file_has_content_hash_attached(self):
        """Caller persists `_content_hash` after a successful per-file ingest.
        Pure helper sets it for added + modified + unchanged so the caller
        can flush state without recomputing."""
        files = [self._f("a.py", "x = 1")]
        diff = diff_against_state(files, prior_state={})
        assert diff["added"][0]["_content_hash"] == _compute_file_hash("x = 1")

    def test_files_without_path_skipped(self):
        files = [{"path": "", "content": "x"}, {"content": "y"}, self._f("c.py", "z")]
        diff = diff_against_state(files, prior_state={})
        assert len(diff["added"]) == 1
        assert diff["added"][0]["path"] == "c.py"

    def test_idempotent_when_no_changes(self):
        """Re-running with identical files = empty buckets except unchanged."""
        files = [self._f("a.py", "x"), self._f("b.py", "y")]
        # First run: build state from current files
        prior = {f["path"]: _compute_file_hash(f["content"]) for f in files}
        diff = diff_against_state(files, prior)
        assert len(diff["unchanged"]) == 2
        assert diff["modified"] == []
        assert diff["added"] == []
        assert diff["deleted"] == []

    def test_deleted_list_sorted(self):
        files: list[dict] = []
        prior = {
            "z.py": _compute_file_hash("a"),
            "a.py": _compute_file_hash("b"),
            "m.py": _compute_file_hash("c"),
        }
        diff = diff_against_state(files, prior)
        assert diff["deleted"] == ["a.py", "m.py", "z.py"]

    def test_unchanged_carries_content_hash_too(self):
        """The hash field is set on every classified file regardless of
        bucket — keeps the caller's persistence loop simple."""
        files = [self._f("a.py", "x")]
        prior = {"a.py": _compute_file_hash("x")}
        diff = diff_against_state(files, prior)
        assert diff["unchanged"][0]["_content_hash"] == _compute_file_hash("x")


# ──────────────────────────────────────────────────────────────────────────────
# Admin endpoint validation
# ──────────────────────────────────────────────────────────────────────────────

class TestIncrementalEndpointValidation:

    def test_unknown_repo_404(self, monkeypatch):
        from app.api import code_indexing as ci

        class _FakeDb:
            def get(self, model, repo_id):
                return None

        body = ci.PolyglotIndexRequest(languages=["python"])
        with pytest.raises(HTTPException) as exc:
            ci.index_repo_polyglot_incremental(
                "missing", body, BackgroundTasks(), _FakeDb(), MagicMock(),
            )
        assert exc.value.status_code == 404

    def test_no_recognised_languages_400(self, monkeypatch):
        from app.api import code_indexing as ci
        repo = MagicMock(gitlab_repo="grp/proj", gitlab_branch="main", gitlab_url=None)
        class _FakeDb:
            def get(self, model, repo_id):
                return repo

        body = ci.PolyglotIndexRequest(languages=["rust"])
        with pytest.raises(HTTPException) as exc:
            ci.index_repo_polyglot_incremental(
                "repo-1", body, BackgroundTasks(), _FakeDb(), MagicMock(),
            )
        assert exc.value.status_code == 400

    def test_happy_path_delegates_to_incremental(self, monkeypatch):
        from app.api import code_indexing as ci
        repo = MagicMock(gitlab_repo="grp/proj", gitlab_branch="main", gitlab_url=None)

        class _FakeDb:
            def get(self, model, repo_id):
                return repo

        monkeypatch.setattr(ci.job_registry, "create_job", lambda *a, **kw: "job-1")

        bg = BackgroundTasks()
        body = ci.PolyglotIndexRequest(languages=["python"])
        result = ci.index_repo_polyglot_incremental(
            "repo-1", body, bg, _FakeDb(), MagicMock(),
        )

        assert result["languages"] == ["python"]
        assert result["job_id"] == "job-1"
        assert bg.tasks[0].kwargs["languages"] == ["python"]
        # The mode string is what routes the job to the incremental ingest path.
        assert bg.tasks[0].kwargs["mode"] == "polyglot-incremental"
