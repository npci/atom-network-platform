# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 23 wiring inside `ingest_polyglot_repo`.

Verifies that:
  - flag OFF → LSP resolver is NOT invoked
  - flag ON  → LSP resolver IS invoked, and only for python files
  - LSP failure does NOT abort ingestion (fail-open)

We don't run the real ingest (it hits GitLab + DB + embeddings); we
import the function and patch its dependencies. This focuses the test
on the new wiring surface only.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.rag import code_ingestion as ci


# ──────────────────────────────────────────────────────────────────────────────
# Shared stubs
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_ingest_deps(monkeypatch):
    """Stub fetcher + embedding + DB so `ingest_polyglot_repo` can run inline.

    The stubs intentionally produce a tiny in-memory dataset:
      - 1 Python file with 1 method-chunk that has within-file `calls`
      - chunker yields a single chunk per file (no class/method recursion)
      - embeddings return zero-vector lists
    """
    files = [{
        "path": "main.py",
        "content": "def caller():\n    return helper()\n\ndef helper():\n    return 1\n",
        "language": "python",
    }]

    monkeypatch.setattr(ci, "_fetch_files_by_extensions",
                        lambda repo, branch, exts: files)

    # Chunker stub: emit one method-chunk that simulates Slice 22a's output
    # so the LSP can find within-file calls in it.
    def fake_chunk_source_file(path, content, language, *, fallback=None):
        return [{
            "id": "test-c1",   # accepted by attach_cross_file_calls fallback to chunk_id
            "path": path,
            "class_name": None,
            "method_name": "caller",
            "content": "def caller(): return helper()",
            "chunk_index": 0,
            "language": language,
            "symbol_kind": "function",
            "symbol_name": "caller",
            "line_start": 1,
            "line_end": 2,
            "calls": ["helper"],
        }]
    monkeypatch.setattr(ci.code_chunker_ts, "chunk_source_file", fake_chunk_source_file)

    # Patch BOTH the name bound in code_ingestion and the function at its
    # source. Patching only `ci.embed_texts` leaves the cached-embedding path
    # reaching `app.rag.embeddings.embed_texts` directly, which then makes a
    # REAL HTTP call. That resolves inside the compose network and fails
    # anywhere else — the first CI run of this suite died here with
    # "Name or service not known", while the same test passed locally.
    # A unit test must not depend on a reachable embedding server.
    _fake_embed = lambda texts: [[0.0] * 768 for _ in texts]  # noqa: E731
    monkeypatch.setattr(ci, "embed_texts", _fake_embed)
    monkeypatch.setattr("app.rag.embeddings.embed_texts", _fake_embed)

    # Disable downstream side-effects we don't care about.
    monkeypatch.setattr(settings, "use_symbol_graph_extractor", False)
    monkeypatch.setattr(settings, "use_code_multiview_embedding", False)

    # Stub DB so ingest_polyglot_repo doesn't need a real one.
    class _FakeDb:
        def __init__(self):
            self.added: list = []
        def query(self, *a, **kw):
            class _Q:
                def filter(self, *a, **kw): return self
                def all(self): return []
            return _Q()
        def flush(self): pass
        def commit(self): pass
        def add(self, x): self.added.append(x)

    return {"db": _FakeDb(), "files": files}


# ──────────────────────────────────────────────────────────────────────────────
# Flag-off behaviour
# ──────────────────────────────────────────────────────────────────────────────

class TestFlagOff:

    def test_lsp_resolver_not_invoked_when_flag_off(self, monkeypatch, mock_ingest_deps):
        monkeypatch.setattr(settings, "use_python_lsp", False)

        called = {"n": 0}
        def fake_resolve(*a, **kw):
            called["n"] += 1
            return MagicMock()
        # Lazy-import target — patch via the module path used in code_ingestion.
        monkeypatch.setattr(
            "app.rag.lsp_resolver_python.resolve_cross_file_calls",
            fake_resolve,
        )

        ci.ingest_polyglot_repo(
            db=mock_ingest_deps["db"],
            repo_id="r1", repo="grp/proj", branch="main",
            languages=["python"],
        )
        assert called["n"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# Flag-on happy path
# ──────────────────────────────────────────────────────────────────────────────

class TestFlagOnHappyPath:

    def test_lsp_resolver_invoked_with_python_files(
        self, monkeypatch, mock_ingest_deps,
    ):
        monkeypatch.setattr(settings, "use_python_lsp", True)

        captured: dict = {}
        def fake_resolve(files, chunks, *, timeout_seconds=60, repo_root=None):
            captured["files"] = files
            captured["chunks"] = chunks
            from app.rag.lsp_resolver_python import ResolutionReport
            return ResolutionReport(requests=1, resolved=1, cross_file=1)
        monkeypatch.setattr(
            "app.rag.lsp_resolver_python.resolve_cross_file_calls",
            fake_resolve,
        )

        ci.ingest_polyglot_repo(
            db=mock_ingest_deps["db"],
            repo_id="r1", repo="grp/proj", branch="main",
            languages=["python"],
        )

        # Confirms the resolver received only python files + the chunk list.
        assert all(
            (f.get("language") or "").lower() == "python" for f in captured["files"]
        )
        assert any(c.get("language") == "python" for c in captured["chunks"])

    def test_no_python_files_means_no_lsp_call(self, monkeypatch, mock_ingest_deps):
        """When `languages=['java']`, the python-only LSP resolver shouldn't run."""
        monkeypatch.setattr(settings, "use_python_lsp", True)

        # Override fetcher to return only Java files
        def fake_fetch(repo, branch, exts):
            return [{"path": "Foo.java", "content": "class Foo {}", "language": "java"}]
        monkeypatch.setattr(ci, "_fetch_files_by_extensions", fake_fetch)

        called = {"n": 0}
        def fake_resolve(*a, **kw):
            called["n"] += 1
            return MagicMock()
        monkeypatch.setattr(
            "app.rag.lsp_resolver_python.resolve_cross_file_calls",
            fake_resolve,
        )

        # Make the chunker accept "java" too
        monkeypatch.setattr(
            ci.code_chunker_ts, "chunk_source_file",
            lambda path, content, lang, *, fallback=None: [{
                "id": "j1", "path": path, "class_name": "Foo",
                "method_name": None, "content": content,
                "chunk_index": 0, "language": "java",
                "symbol_kind": "class", "symbol_name": "Foo",
            }],
        )

        ci.ingest_polyglot_repo(
            db=mock_ingest_deps["db"],
            repo_id="r1", repo="grp/proj", branch="main",
            languages=["java"],
        )
        assert called["n"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# Fail-open behaviour
# ──────────────────────────────────────────────────────────────────────────────

class TestFailOpen:

    def test_resolver_exception_does_not_abort_ingestion(
        self, monkeypatch, mock_ingest_deps,
    ):
        monkeypatch.setattr(settings, "use_python_lsp", True)

        def boom(*a, **kw):
            raise RuntimeError("LSP exploded")
        monkeypatch.setattr(
            "app.rag.lsp_resolver_python.resolve_cross_file_calls", boom,
        )

        # Must complete without raising — chunks still stored.
        result = ci.ingest_polyglot_repo(
            db=mock_ingest_deps["db"],
            repo_id="r1", repo="grp/proj", branch="main",
            languages=["python"],
        )
        assert result["files_fetched"] >= 1
        assert result["chunks_stored"] >= 1
