# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 22d — polyglot retrieval in the code-change agent.

Verifies that `_build_system_prompt` (via `retrieve`) requests all four
code-source categories — Java, Python, TypeScript, JavaScript — not just
Java. Stubs `retrieve` so we can inspect the categories argument without
hitting the DB.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agents import code_change as cc
from app.core.config import settings
from app.models.document_chunk import CODE_SOURCE_CATEGORIES, DocCategory


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

class TestCodeSourceCategories:

    def test_includes_java(self):
        assert DocCategory.JAVA_SOURCE in CODE_SOURCE_CATEGORIES

    def test_includes_python(self):
        assert DocCategory.PYTHON_SOURCE in CODE_SOURCE_CATEGORIES

    def test_includes_typescript(self):
        assert DocCategory.TYPESCRIPT_SOURCE in CODE_SOURCE_CATEGORIES

    def test_includes_javascript(self):
        assert DocCategory.JAVASCRIPT_SOURCE in CODE_SOURCE_CATEGORIES

    def test_no_duplicates(self):
        assert len(CODE_SOURCE_CATEGORIES) == len(set(CODE_SOURCE_CATEGORIES))

    def test_python_source_string_value(self):
        # The string must match what `ingest_polyglot_repo` writes for Python
        # chunks — otherwise retrieval misses them.
        assert DocCategory.PYTHON_SOURCE == "python_source"

    def test_typescript_source_string_value(self):
        assert DocCategory.TYPESCRIPT_SOURCE == "typescript_source"

    def test_javascript_source_string_value(self):
        assert DocCategory.JAVASCRIPT_SOURCE == "javascript_source"


# ──────────────────────────────────────────────────────────────────────────────
# code_change._build_system_prompt — categories argument fan-out
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildSystemPromptPolyglotRetrieval:

    def _stub_other_deps(self, monkeypatch):
        """Stub helpers that aren't under test so we can focus on retrieve()."""
        monkeypatch.setattr(cc, "build_context", lambda chunks, **kw: "(ctx)")
        # _get_file_tree now takes an optional phase_b_run_id and returns a 4-tuple
        # (tree_text, path_to_repo, repo_label_to_id, repo_summaries).
        monkeypatch.setattr(cc, "_get_file_tree",
                            lambda db, phase_b_run_id=None: ("filetree_stub", {}, {}, []))
        # Disable impact-block side path (Slice 20c) so it doesn't make a
        # second retrieve call from `analyze_impact` (which doesn't, but
        # cheap insurance).
        monkeypatch.setattr(settings, "use_impact_analyzer", False)

    def test_retrieve_called_with_all_four_code_categories(self, monkeypatch):
        self._stub_other_deps(monkeypatch)

        captured: list[list] = []
        def fake_retrieve(q, db, *, top_k=12, categories=None, min_score=0.0):
            captured.append(list(categories) if categories else [])
            return []
        monkeypatch.setattr(cc, "retrieve", fake_retrieve)

        cc._build_system_prompt(
            MagicMock(), "change-id",
            tech_spec="Refactor NetworkSwitchService",
            brd="BRD body",
        )

        # The agent runs retrieve once per query in its `queries` list
        # (currently 2). Every call must include all 4 code categories.
        assert len(captured) >= 1
        for call_categories in captured:
            cats = set(call_categories)
            assert DocCategory.JAVA_SOURCE in cats
            assert DocCategory.PYTHON_SOURCE in cats
            assert DocCategory.TYPESCRIPT_SOURCE in cats
            assert DocCategory.JAVASCRIPT_SOURCE in cats

    def test_categories_argument_is_a_list(self, monkeypatch):
        """retrieve() expects a list (not a tuple) — historically required by
        the SQL bind-parameter expansion in hybrid_search."""
        self._stub_other_deps(monkeypatch)

        captured: list = []
        def fake_retrieve(q, db, *, top_k=12, categories=None, min_score=0.0):
            captured.append(categories)
            return []
        monkeypatch.setattr(cc, "retrieve", fake_retrieve)

        cc._build_system_prompt(MagicMock(), "id", "ts", "brd")

        for c in captured:
            assert isinstance(c, list)
            assert len(c) == 4

    def test_no_regression_when_no_polyglot_data_indexed(self, monkeypatch):
        """If only Java chunks are indexed, retrieve still works and the
        agent gets exactly what it would have gotten before this slice."""
        self._stub_other_deps(monkeypatch)

        # Simulate: only JAVA_SOURCE chunks come back regardless of which
        # categories are requested (DB has no python/ts/js rows yet).
        java_chunk = {
            "id": "j1", "source_file": "Foo.java",
            "doc_category": DocCategory.JAVA_SOURCE,
            "content": "class Foo {}", "chunk_index": 0,
            "score": 0.9, "parent_symbol_id": None,
        }
        monkeypatch.setattr(
            cc, "retrieve",
            lambda q, db, *, top_k=12, categories=None, min_score=0.0: [java_chunk],
        )

        prompt, _, _, _, doc_ctx = cc._build_system_prompt(MagicMock(), "id", "tech spec", "brd")
        # No errors, prompt renders cleanly. (Body content stub means we
        # mostly verify the call chain didn't blow up.)
        assert "## Existing Codebase — Directory Tree" in prompt
