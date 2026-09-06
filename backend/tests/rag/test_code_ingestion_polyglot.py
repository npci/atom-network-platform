# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for polyglot ingestion primitives (Slice 22c).

Covers the pure helpers (`LANGUAGE_EXTENSIONS`, `_detect_language`,
`_extensions_for_languages`) plus the admin endpoint's input validation.
The full `ingest_polyglot_repo` integration test (which would hit GitLab +
DB + embeddings) is out of scope for this slice — that path is exercised
manually via the new `/admin/code-indexing/repos/{id}/index-polyglot`
endpoint.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.rag.code_ingestion import (
    LANGUAGE_EXTENSIONS,
    _detect_language,
    _extensions_for_languages,
)


# ──────────────────────────────────────────────────────────────────────────────
# LANGUAGE_EXTENSIONS table
# ──────────────────────────────────────────────────────────────────────────────

class TestLanguageExtensionsTable:

    def test_has_java(self):
        assert LANGUAGE_EXTENSIONS[".java"] == "java"

    def test_has_python(self):
        assert LANGUAGE_EXTENSIONS[".py"] == "python"

    def test_keys_are_lowercase_with_leading_dot(self):
        for ext in LANGUAGE_EXTENSIONS:
            assert ext.startswith("."), f"missing leading dot: {ext!r}"
            assert ext == ext.lower(), f"non-lowercase ext: {ext!r}"

    def test_values_are_canonical_lowercase(self):
        for lang in LANGUAGE_EXTENSIONS.values():
            assert lang == lang.lower(), f"non-lowercase lang: {lang!r}"


# ──────────────────────────────────────────────────────────────────────────────
# _detect_language
# ──────────────────────────────────────────────────────────────────────────────

class TestDetectLanguage:

    def test_simple_java(self):
        assert _detect_language("Foo.java") == "java"
        assert _detect_language("src/main/java/com/network/Foo.java") == "java"

    def test_simple_python(self):
        assert _detect_language("foo.py") == "python"
        assert _detect_language("backend/app/main.py") == "python"

    def test_uppercase_extension_normalised(self):
        assert _detect_language("Foo.JAVA") == "java"
        assert _detect_language("foo.PY") == "python"

    def test_unknown_extension(self):
        assert _detect_language("foo.rs") is None
        assert _detect_language("README.md") is None

    def test_no_extension(self):
        assert _detect_language("Makefile") is None
        assert _detect_language("LICENSE") is None

    def test_empty_path(self):
        assert _detect_language("") is None
        assert _detect_language(None) is None


# ──────────────────────────────────────────────────────────────────────────────
# _extensions_for_languages
# ──────────────────────────────────────────────────────────────────────────────

class TestExtensionsForLanguages:

    def test_single_language_java(self):
        out = _extensions_for_languages(["java"])
        assert out == [".java"]

    def test_single_language_python(self):
        out = _extensions_for_languages(["python"])
        assert out == [".py"]

    def test_multiple_languages(self):
        out = _extensions_for_languages(["java", "python"])
        assert set(out) == {".java", ".py"}

    def test_unknown_language_dropped_silently(self):
        out = _extensions_for_languages(["java", "rust", "haskell"])
        assert out == [".java"]

    def test_empty_input(self):
        assert _extensions_for_languages([]) == []

    def test_case_insensitive_match(self):
        out = _extensions_for_languages(["JAVA", "Python"])
        assert set(out) == {".java", ".py"}

    def test_blank_strings_skipped(self):
        out = _extensions_for_languages(["", None, "python"])  # type: ignore[list-item]
        assert out == [".py"]


# ──────────────────────────────────────────────────────────────────────────────
# Admin endpoint input validation (`index_repo_polyglot`)
# ──────────────────────────────────────────────────────────────────────────────

class TestPolyglotEndpointValidation:

    def test_unknown_repo_raises_404(self, monkeypatch):
        from app.api import code_indexing as ci

        # Stub db.get → None to simulate missing repo
        class _FakeDb:
            def get(self, model, repo_id):
                return None

        body = ci.PolyglotIndexRequest(languages=["java"])
        with pytest.raises(HTTPException) as exc:
            ci.index_repo_polyglot(
                "nonexistent", body, BackgroundTasks(), _FakeDb(), MagicMock(),
            )
        assert exc.value.status_code == 404

    def test_no_recognised_languages_raises_400(self, monkeypatch):
        from app.api import code_indexing as ci

        # Stub db.get → real repo, but request only unknown languages.
        repo = MagicMock(gitlab_repo="grp/proj", gitlab_branch="main", gitlab_url=None)
        class _FakeDb:
            def get(self, model, repo_id):
                return repo

        body = ci.PolyglotIndexRequest(languages=["rust", "haskell"])
        with pytest.raises(HTTPException) as exc:
            ci.index_repo_polyglot(
                "repo-1", body, BackgroundTasks(), _FakeDb(), MagicMock(),
            )
        assert exc.value.status_code == 400
        assert "rust" in str(exc.value.detail) or "haskell" in str(exc.value.detail)

    def test_partial_unknown_languages_filtered_to_known(self, monkeypatch):
        """Mix of known + unknown → only known reach the scheduled job."""
        from app.api import code_indexing as ci

        repo = MagicMock(gitlab_repo="grp/proj", gitlab_branch="main", gitlab_url=None)

        class _FakeDb:
            def get(self, model, repo_id):
                return repo

        monkeypatch.setattr(ci.job_registry, "create_job", lambda *a, **kw: "job-1")

        bg = BackgroundTasks()
        body = ci.PolyglotIndexRequest(languages=["java", "rust"])
        result = ci.index_repo_polyglot("repo-1", body, bg, _FakeDb(), MagicMock())

        assert result["languages"] == ["java"]
        assert result["job_id"] == "job-1"
        assert bg.tasks[0].kwargs["languages"] == ["java"]

    def test_happy_path_passes_full_language_list(self, monkeypatch):
        from app.api import code_indexing as ci

        repo = MagicMock(gitlab_repo="grp/proj", gitlab_branch="main", gitlab_url=None)

        class _FakeDb:
            def get(self, model, repo_id):
                return repo

        monkeypatch.setattr(ci.job_registry, "create_job", lambda *a, **kw: "job-1")

        bg = BackgroundTasks()
        body = ci.PolyglotIndexRequest(languages=["JAVA", "Python"])  # mixed case
        result = ci.index_repo_polyglot("repo-1", body, bg, _FakeDb(), MagicMock())

        # Both should pass through, lowercased.
        assert set(result["languages"]) == {"java", "python"}
        assert set(bg.tasks[0].kwargs["languages"]) == {"java", "python"}
        assert bg.tasks[0].kwargs["mode"] == "polyglot"
