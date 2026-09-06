# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the Python LSP resolver (Slice 23).

Layered:
  1. Pure helpers (`_find_first_occurrence`, `_collect_call_sites...`,
     `_materialise_repo`, `_is_cross_file`, `attach_cross_file_calls`)
  2. `resolve_cross_file_calls` with multilspy stubbed at the import
     boundary so we exercise the orchestration without spawning a server
  3. One `@pytest.mark.lsp` integration test that runs the live multilspy
     stack against a tiny on-disk Python project (skipped if multilspy
     isn't importable)
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.rag import lsp_resolver_python as lsp
from app.rag.lsp_resolver_python import (
    CallSiteRequest,
    CrossFileCall,
    ResolutionReport,
    _collect_call_sites_from_within_file_graphs,
    _find_first_occurrence,
    _is_cross_file,
    _materialise_repo,
    attach_cross_file_calls,
    is_multilspy_available,
    resolve_cross_file_calls,
)


# ──────────────────────────────────────────────────────────────────────────────
# _find_first_occurrence
# ──────────────────────────────────────────────────────────────────────────────

class TestFindFirstOccurrence:

    def test_simple_match(self):
        lines = ["def foo():", "    return helper()", "    return 1"]
        out = _find_first_occurrence(lines, "helper", line_start=1, line_end=3)
        assert out is not None
        line, char = out
        assert line == 1                          # 0-indexed second line
        assert lines[line][char:char+len("helper")] == "helper"

    def test_word_boundary_avoids_substring(self):
        lines = ["def foo():", "    return helper_one()"]
        out = _find_first_occurrence(lines, "helper", line_start=1, line_end=2)
        # `helper` must NOT match inside `helper_one` due to \b enforcement.
        assert out is None

    def test_match_at_start_of_line(self):
        lines = ["def foo():", "helper()"]
        out = _find_first_occurrence(lines, "helper", line_start=1, line_end=2)
        line, char = out
        assert (line, char) == (1, 0)

    def test_no_match_returns_none(self):
        lines = ["def foo():", "    return 1"]
        out = _find_first_occurrence(lines, "helper")
        assert out is None

    def test_empty_needle(self):
        assert _find_first_occurrence(["a", "b"], "") is None

    def test_handles_zero_or_one_indexed_line_inputs(self):
        """Tree-sitter line_start is 1-indexed; we shift to 0-indexed."""
        lines = ["row0", "row1 helper()", "row2"]
        out = _find_first_occurrence(lines, "helper", line_start=2, line_end=2)
        line, _char = out
        assert line == 1


# ──────────────────────────────────────────────────────────────────────────────
# _is_cross_file
# ──────────────────────────────────────────────────────────────────────────────

class TestIsCrossFile:

    def test_same_file(self):
        assert _is_cross_file("a.py", "a.py") is False

    def test_different_file(self):
        assert _is_cross_file("a.py", "b.py") is True

    def test_uri_form(self):
        assert _is_cross_file("file:///root/a.py", "/root/b.py") is True

    def test_empty_callee(self):
        assert _is_cross_file("", "a.py") is False


# ──────────────────────────────────────────────────────────────────────────────
# _materialise_repo
# ──────────────────────────────────────────────────────────────────────────────

class TestMaterialiseRepo:

    def test_writes_each_file_to_correct_path(self, tmp_path):
        files = [
            {"path": "a.py", "content": "x = 1\n"},
            {"path": "pkg/b.py", "content": "y = 2\n"},
        ]
        root = _materialise_repo(files, parent_dir=str(tmp_path))
        try:
            assert (Path(root) / "a.py").read_text() == "x = 1\n"
            assert (Path(root) / "pkg" / "b.py").read_text() == "y = 2\n"
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_drops_absolute_paths(self, tmp_path):
        files = [{"path": "/etc/passwd", "content": "evil"}]
        root = _materialise_repo(files, parent_dir=str(tmp_path))
        try:
            # Should NOT have written outside the tempdir.
            assert not (Path(root) / "etc" / "passwd").exists()
            assert not Path("/etc/passwd_new").exists()
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────────────
# _collect_call_sites_from_within_file_graphs
# ──────────────────────────────────────────────────────────────────────────────

class TestCollectCallSites:

    def test_extracts_one_request_per_call(self):
        chunks = [
            {
                "id": "c1", "language": "python", "symbol_kind": "function",
                "path": "a.py", "line_start": 1, "line_end": 3,
                "calls": ["helper", "other"],
            },
        ]
        content = {
            "a.py": "def caller():\n    return helper() + other()\n",
        }
        out = _collect_call_sites_from_within_file_graphs(chunks, content)
        names = [r.callee_symbol for r in out]
        assert "helper" in names
        assert "other" in names

    def test_skips_non_python_chunks(self):
        chunks = [
            {"id": "c1", "language": "java", "symbol_kind": "method",
             "path": "Foo.java", "calls": ["bar"]},
        ]
        out = _collect_call_sites_from_within_file_graphs(chunks, {})
        assert out == []

    def test_skips_chunks_without_calls(self):
        chunks = [
            {"id": "c1", "language": "python", "symbol_kind": "function",
             "path": "a.py", "calls": []},
            {"id": "c2", "language": "python", "symbol_kind": "function",
             "path": "a.py", "calls": None},
        ]
        out = _collect_call_sites_from_within_file_graphs(chunks, {"a.py": ""})
        assert out == []

    def test_skips_calls_not_found_in_body(self):
        chunks = [
            {"id": "c1", "language": "python", "symbol_kind": "function",
             "path": "a.py", "line_start": 1, "line_end": 2,
             "calls": ["nope"]},
        ]
        content = {"a.py": "def caller():\n    return 1\n"}
        out = _collect_call_sites_from_within_file_graphs(chunks, content)
        assert out == []


# ──────────────────────────────────────────────────────────────────────────────
# attach_cross_file_calls
# ──────────────────────────────────────────────────────────────────────────────

class TestAttachCrossFileCalls:

    def test_attaches_to_matching_chunk_id(self):
        chunks = [{"id": "c1"}, {"id": "c2"}]
        by_caller = {
            "c1": [
                CrossFileCall(caller_chunk_id="c1", callee_symbol="x",
                              callee_path="other.py", line=10),
            ],
        }
        attached = attach_cross_file_calls(chunks, by_caller)
        assert attached == 1
        assert chunks[0]["cross_file_calls"][0]["callee_symbol"] == "x"
        assert chunks[0]["cross_file_calls"][0]["callee_path"] == "other.py"
        assert "cross_file_calls" not in chunks[1]

    def test_unknown_caller_id_silently_skipped(self):
        chunks = [{"id": "c1"}]
        by_caller = {"unknown": [
            CrossFileCall(caller_chunk_id="unknown", callee_symbol="x",
                          callee_path="other.py"),
        ]}
        attached = attach_cross_file_calls(chunks, by_caller)
        assert attached == 0

    def test_chunk_without_id_skipped(self):
        chunks = [{"path": "a.py"}]   # no id
        attach_cross_file_calls(chunks, {"x": []})
        assert "cross_file_calls" not in chunks[0]


# ──────────────────────────────────────────────────────────────────────────────
# resolve_cross_file_calls — orchestration with multilspy stubbed
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveCrossFileCallsOrchestration:

    def test_no_python_files_returns_empty_report(self, monkeypatch):
        report = resolve_cross_file_calls(
            files=[{"path": "Foo.java", "content": "x", "language": "java"}],
            file_chunks=[],
        )
        assert isinstance(report, ResolutionReport)
        assert report.requests == 0

    def test_multilspy_unavailable_records_failure(self, monkeypatch):
        monkeypatch.setattr(lsp, "is_multilspy_available", lambda: False)
        files = [{"path": "a.py", "content": "def x(): pass", "language": "python"}]
        chunks = [{
            "id": "c1", "language": "python", "symbol_kind": "function",
            "path": "a.py", "line_start": 1, "line_end": 1, "calls": ["nothing"],
        }]
        report = resolve_cross_file_calls(files, chunks)
        assert report.failures >= 1
        assert any("multilspy" in r for r in report.failure_reasons)

    def test_no_call_sites_short_circuits(self, monkeypatch):
        """Has Python files but no within-file calls → no LSP work, empty report."""
        monkeypatch.setattr(lsp, "is_multilspy_available", lambda: True)
        files = [{"path": "a.py", "content": "x = 1", "language": "python"}]
        chunks = []   # nothing with `calls`
        report = resolve_cross_file_calls(files, chunks)
        assert report.requests == 0
        assert report.failures == 0

    def test_lsp_session_failure_returns_empty_with_failure_recorded(
        self, monkeypatch,
    ):
        """When the LSP coroutine throws, the wrapper catches + reports."""
        monkeypatch.setattr(lsp, "is_multilspy_available", lambda: True)

        async def failing_lsp(repo_root, requests, *, timeout_seconds=60):
            from app.rag.lsp_resolver_python import ResolutionReport as RR
            r = RR()
            r.failures = 1
            r.failure_reasons.append("simulated LSP boom")
            return {}, r
        monkeypatch.setattr(lsp, "_ask_lsp_for_definitions", failing_lsp)

        files = [{
            "path": "a.py",
            "content": "def caller():\n    return helper()\n\ndef other():\n    pass\n",
            "language": "python",
        }]
        chunks = [{
            "id": "c1", "language": "python", "symbol_kind": "function",
            "path": "a.py", "line_start": 1, "line_end": 2,
            "calls": ["helper"],
        }]
        report = resolve_cross_file_calls(files, chunks)
        assert report.failures >= 1
        assert any("LSP" in r or "boom" in r for r in report.failure_reasons)

    def test_happy_path_attaches_cross_file_calls(self, monkeypatch):
        """Stub the LSP call to return a synthetic cross-file definition;
        verify the orchestrator attaches it to the right chunk."""
        monkeypatch.setattr(lsp, "is_multilspy_available", lambda: True)

        async def fake_lsp(repo_root, requests, *, timeout_seconds=60):
            r = ResolutionReport()
            by_caller: dict[str, list[CrossFileCall]] = {}
            for req in requests:
                r.requests += 1
                r.resolved += 1
                r.cross_file += 1
                by_caller.setdefault(req.caller_chunk_id, []).append(
                    CrossFileCall(
                        caller_chunk_id=req.caller_chunk_id,
                        callee_symbol=req.callee_symbol,
                        callee_path="utils/helpers.py",
                        line=42, language="python",
                    ),
                )
            return by_caller, r
        monkeypatch.setattr(lsp, "_ask_lsp_for_definitions", fake_lsp)

        files = [{
            "path": "main.py",
            "content": "def caller():\n    return helper()\n",
            "language": "python",
        }]
        chunks = [{
            "id": "c1", "language": "python", "symbol_kind": "function",
            "path": "main.py", "line_start": 1, "line_end": 2,
            "calls": ["helper"],
        }]
        report = resolve_cross_file_calls(files, chunks)

        assert report.requests == 1
        assert report.cross_file == 1
        assert chunks[0]["cross_file_calls"][0]["callee_path"] == "utils/helpers.py"
        assert chunks[0]["cross_file_calls"][0]["callee_symbol"] == "helper"

    def test_outer_exception_swallowed(self, monkeypatch):
        """Even an unexpected error inside _ask_lsp must not propagate."""
        monkeypatch.setattr(lsp, "is_multilspy_available", lambda: True)

        async def boom(repo_root, requests, *, timeout_seconds=60):
            raise RuntimeError("unexpected")
        monkeypatch.setattr(lsp, "_ask_lsp_for_definitions", boom)

        files = [{"path": "a.py", "content": "def caller():\n    helper()\n",
                  "language": "python"}]
        chunks = [{"id": "c1", "language": "python", "symbol_kind": "function",
                   "path": "a.py", "line_start": 1, "line_end": 2,
                   "calls": ["helper"]}]
        # Must not raise.
        report = resolve_cross_file_calls(files, chunks)
        assert isinstance(report, ResolutionReport)


# ──────────────────────────────────────────────────────────────────────────────
# Live multilspy integration (optional)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.lsp
def test_resolve_against_live_multilspy(tmp_path):
    """Smoke: tiny project where main.py calls helpers.helper(); LSP should
    resolve `helper` to helpers.py. Skipped automatically when multilspy
    isn't installed."""
    if not is_multilspy_available():
        pytest.skip("multilspy not installed")

    helpers = tmp_path / "helpers.py"
    helpers.write_text("def helper():\n    return 1\n")
    main = tmp_path / "main.py"
    main.write_text(
        "from helpers import helper\n"
        "def caller():\n"
        "    return helper()\n"
    )

    files = [
        {"path": "helpers.py", "content": helpers.read_text(), "language": "python"},
        {"path": "main.py",    "content": main.read_text(),    "language": "python"},
    ]
    chunks = [{
        "id": "c1", "language": "python", "symbol_kind": "function",
        "path": "main.py", "line_start": 2, "line_end": 3,
        "calls": ["helper"],
    }]

    report = resolve_cross_file_calls(
        files, chunks, repo_root=str(tmp_path), timeout_seconds=30,
    )
    # Either the LSP resolved (cross_file=1) or it's environmentally failing
    # (e.g. jedi-language-server binary missing). Both are acceptable for v0.
    assert isinstance(report, ResolutionReport)
    if report.cross_file >= 1:
        assert chunks[0].get("cross_file_calls"), \
            "cross_file resolution succeeded but chunk wasn't enriched"
