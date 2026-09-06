# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the TypeScript / JavaScript LSP resolver (Slice 24).

Mirrors `test_lsp_resolver_python.py` for the TS/JS module. Layered:
  1. Pure helpers
  2. `resolve_cross_file_calls` orchestration with multilspy stubbed
  3. One `@pytest.mark.lsp` integration that runs the live multilspy stack
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.rag import lsp_resolver_typescript as ts_lsp
from app.rag.lsp_resolver_typescript import (
    TsCallSiteRequest,
    TsCrossFileCall,
    TsResolutionReport,
    _collect_call_sites,
    _find_first_occurrence,
    _is_ts_or_js_file,
    _materialise_repo,
    attach_cross_file_calls,
    is_multilspy_available,
    resolve_cross_file_calls,
)


# ──────────────────────────────────────────────────────────────────────────────
# _is_ts_or_js_file
# ──────────────────────────────────────────────────────────────────────────────

class TestIsTsOrJsFile:

    def test_ts_extension(self):
        assert _is_ts_or_js_file("Foo.ts") is True
        assert _is_ts_or_js_file("src/Foo.ts") is True

    def test_tsx_extension(self):
        assert _is_ts_or_js_file("Component.tsx") is True

    def test_js_extension(self):
        assert _is_ts_or_js_file("util.js") is True

    def test_jsx_extension(self):
        assert _is_ts_or_js_file("App.jsx") is True

    def test_uppercase_extension(self):
        assert _is_ts_or_js_file("Foo.TS") is True

    def test_python_rejected(self):
        assert _is_ts_or_js_file("foo.py") is False

    def test_no_extension(self):
        assert _is_ts_or_js_file("Makefile") is False


# ──────────────────────────────────────────────────────────────────────────────
# _find_first_occurrence (shared regex behaviour with Python resolver)
# ──────────────────────────────────────────────────────────────────────────────

class TestFindFirstOccurrence:

    def test_simple_match(self):
        lines = ["function foo() {", "    return helper();", "}"]
        out = _find_first_occurrence(lines, "helper", line_start=1, line_end=3)
        line, char = out
        assert line == 1
        assert lines[line][char:char+len("helper")] == "helper"

    def test_word_boundary_avoids_substring(self):
        lines = ["function foo() {", "    return helperOne();", "}"]
        out = _find_first_occurrence(lines, "helper", line_start=1, line_end=3)
        assert out is None

    def test_no_match(self):
        lines = ["function foo() {", "    return 1;", "}"]
        out = _find_first_occurrence(lines, "nope")
        assert out is None

    def test_empty_needle_returns_none(self):
        assert _find_first_occurrence(["a", "b"], "") is None


# ──────────────────────────────────────────────────────────────────────────────
# _materialise_repo
# ──────────────────────────────────────────────────────────────────────────────

class TestMaterialiseRepo:

    def test_writes_files(self, tmp_path):
        files = [
            {"path": "a.ts", "content": "export const x = 1;\n"},
            {"path": "pkg/b.tsx", "content": "export const y = 2;\n"},
        ]
        root = _materialise_repo(files, parent_dir=str(tmp_path))
        try:
            assert (Path(root) / "a.ts").read_text() == "export const x = 1;\n"
            assert (Path(root) / "pkg" / "b.tsx").read_text() == "export const y = 2;\n"
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_drops_absolute_paths(self, tmp_path):
        files = [{"path": "/etc/passwd", "content": "evil"}]
        root = _materialise_repo(files, parent_dir=str(tmp_path))
        try:
            assert not (Path(root) / "etc" / "passwd").exists()
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_drops_dotdot_traversal(self, tmp_path):
        files = [{"path": "../escape.ts", "content": "evil"}]
        root = _materialise_repo(files, parent_dir=str(tmp_path))
        try:
            # Must not have written outside tmp_path.
            siblings = list(tmp_path.iterdir())
            assert all(s.name.startswith("lsp_ts_repo_") for s in siblings)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────────────
# _collect_call_sites
# ──────────────────────────────────────────────────────────────────────────────

class TestCollectCallSites:

    def test_extracts_per_call(self):
        chunks = [{
            "id": "c1", "language": "typescript", "symbol_kind": "function",
            "path": "main.ts", "line_start": 1, "line_end": 3,
            "calls": ["helper", "other"],
        }]
        content = {
            "main.ts": "function main() {\n    return helper() + other();\n}\n",
        }
        out = _collect_call_sites(chunks, content)
        names = {r.callee_symbol for r in out}
        assert names == {"helper", "other"}

    def test_javascript_chunks_handled(self):
        chunks = [{
            "id": "c1", "language": "javascript", "symbol_kind": "function",
            "path": "util.js", "line_start": 1, "line_end": 3,
            "calls": ["doThing"],
        }]
        content = {"util.js": "function util() {\n    return doThing();\n}\n"}
        out = _collect_call_sites(chunks, content)
        assert len(out) == 1
        assert out[0].language == "javascript"

    def test_python_chunks_skipped(self):
        chunks = [{
            "id": "c1", "language": "python", "symbol_kind": "function",
            "path": "foo.py", "calls": ["helper"],
        }]
        out = _collect_call_sites(chunks, {})
        assert out == []

    def test_chunks_without_calls_skipped(self):
        chunks = [{
            "id": "c1", "language": "typescript", "symbol_kind": "function",
            "path": "main.ts", "calls": []},
        ]
        out = _collect_call_sites(chunks, {"main.ts": ""})
        assert out == []

    def test_calls_not_found_in_body_skipped(self):
        chunks = [{
            "id": "c1", "language": "typescript", "symbol_kind": "function",
            "path": "main.ts", "line_start": 1, "line_end": 2,
            "calls": ["nope"],
        }]
        content = {"main.ts": "function main() {\n    return 1;\n}\n"}
        out = _collect_call_sites(chunks, content)
        assert out == []


# ──────────────────────────────────────────────────────────────────────────────
# attach_cross_file_calls
# ──────────────────────────────────────────────────────────────────────────────

class TestAttachCrossFileCalls:

    def test_attaches_to_matching_chunk(self):
        chunks = [{"id": "c1"}, {"id": "c2"}]
        by_caller = {
            "c1": [TsCrossFileCall(
                caller_chunk_id="c1", callee_symbol="x",
                callee_path="other.ts", line=10,
            )],
        }
        attached = attach_cross_file_calls(chunks, by_caller)
        assert attached == 1
        assert chunks[0]["cross_file_calls"][0]["callee_symbol"] == "x"
        assert chunks[0]["cross_file_calls"][0]["language"] == "typescript"

    def test_merges_with_existing_entries(self):
        """If a chunk already has cross_file_calls (e.g. from Python LSP run
        on a multi-language file — defensive though not currently realistic),
        TS resolutions append rather than overwrite."""
        chunks = [{"id": "c1", "cross_file_calls": [
            {"callee_symbol": "py_func", "callee_path": "x.py",
             "line": 1, "language": "python"},
        ]}]
        by_caller = {
            "c1": [TsCrossFileCall(
                caller_chunk_id="c1", callee_symbol="ts_func",
                callee_path="x.ts", line=2,
            )],
        }
        attach_cross_file_calls(chunks, by_caller)
        cf = chunks[0]["cross_file_calls"]
        assert len(cf) == 2
        symbols = {e["callee_symbol"] for e in cf}
        assert symbols == {"py_func", "ts_func"}

    def test_unknown_caller_id_skipped(self):
        chunks = [{"id": "c1"}]
        attached = attach_cross_file_calls(chunks, {"unknown": [
            TsCrossFileCall(caller_chunk_id="unknown",
                            callee_symbol="x", callee_path="o.ts"),
        ]})
        assert attached == 0


# ──────────────────────────────────────────────────────────────────────────────
# resolve_cross_file_calls — orchestration with multilspy stubbed
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveOrchestration:

    def test_no_ts_js_files_returns_empty_report(self):
        report = resolve_cross_file_calls(
            files=[{"path": "Foo.java", "content": "x", "language": "java"}],
            file_chunks=[],
        )
        assert isinstance(report, TsResolutionReport)
        assert report.requests == 0

    def test_multilspy_unavailable_records_failure(self, monkeypatch):
        monkeypatch.setattr(ts_lsp, "is_multilspy_available", lambda: False)
        files = [{"path": "main.ts", "content": "function f(){}", "language": "typescript"}]
        chunks = [{
            "id": "c1", "language": "typescript", "symbol_kind": "function",
            "path": "main.ts", "line_start": 1, "line_end": 1, "calls": ["x"],
        }]
        report = resolve_cross_file_calls(files, chunks)
        assert report.failures >= 1
        assert any("multilspy" in r for r in report.failure_reasons)

    def test_no_call_sites_short_circuits(self, monkeypatch):
        monkeypatch.setattr(ts_lsp, "is_multilspy_available", lambda: True)
        files = [{"path": "main.ts", "content": "const x = 1;", "language": "typescript"}]
        chunks = []
        report = resolve_cross_file_calls(files, chunks)
        assert report.requests == 0
        assert report.failures == 0

    def test_happy_path_attaches_cross_file_calls(self, monkeypatch):
        monkeypatch.setattr(ts_lsp, "is_multilspy_available", lambda: True)

        async def fake_lsp(repo_root, requests, *, timeout_seconds=60):
            r = TsResolutionReport()
            by_caller: dict[str, list[TsCrossFileCall]] = {}
            for req in requests:
                r.requests += 1
                r.resolved += 1
                r.cross_file += 1
                by_caller.setdefault(req.caller_chunk_id, []).append(
                    TsCrossFileCall(
                        caller_chunk_id=req.caller_chunk_id,
                        callee_symbol=req.callee_symbol,
                        callee_path="utils/helpers.ts",
                        line=42, language=req.language,
                    ),
                )
            return by_caller, r
        monkeypatch.setattr(ts_lsp, "_ask_lsp_for_definitions", fake_lsp)

        files = [{
            "path": "main.ts",
            "content": "function caller() {\n    return helper();\n}\n",
            "language": "typescript",
        }]
        chunks = [{
            "id": "c1", "language": "typescript", "symbol_kind": "function",
            "path": "main.ts", "line_start": 1, "line_end": 3,
            "calls": ["helper"],
        }]
        report = resolve_cross_file_calls(files, chunks)

        assert report.requests == 1
        assert report.cross_file == 1
        assert chunks[0]["cross_file_calls"][0]["callee_path"] == "utils/helpers.ts"
        assert chunks[0]["cross_file_calls"][0]["callee_symbol"] == "helper"
        assert chunks[0]["cross_file_calls"][0]["language"] == "typescript"

    def test_javascript_language_preserved_through_resolution(self, monkeypatch):
        """A `.js` file's resolutions should keep `language: javascript`,
        not get coerced to `typescript`."""
        monkeypatch.setattr(ts_lsp, "is_multilspy_available", lambda: True)

        async def fake_lsp(repo_root, requests, *, timeout_seconds=60):
            r = TsResolutionReport()
            by_caller: dict[str, list[TsCrossFileCall]] = {}
            for req in requests:
                r.cross_file += 1
                by_caller.setdefault(req.caller_chunk_id, []).append(
                    TsCrossFileCall(
                        caller_chunk_id=req.caller_chunk_id,
                        callee_symbol=req.callee_symbol,
                        callee_path="util.js",
                        line=1, language=req.language,
                    ),
                )
            return by_caller, r
        monkeypatch.setattr(ts_lsp, "_ask_lsp_for_definitions", fake_lsp)

        files = [{
            "path": "app.js",
            "content": "function f() { return helper(); }\n",
            "language": "javascript",
        }]
        chunks = [{
            "id": "c1", "language": "javascript", "symbol_kind": "function",
            "path": "app.js", "line_start": 1, "line_end": 1,
            "calls": ["helper"],
        }]
        resolve_cross_file_calls(files, chunks)
        assert chunks[0]["cross_file_calls"][0]["language"] == "javascript"

    def test_outer_exception_swallowed(self, monkeypatch):
        monkeypatch.setattr(ts_lsp, "is_multilspy_available", lambda: True)

        async def boom(repo_root, requests, *, timeout_seconds=60):
            raise RuntimeError("unexpected")
        monkeypatch.setattr(ts_lsp, "_ask_lsp_for_definitions", boom)

        files = [{"path": "a.ts", "content": "function f(){ helper(); }\n",
                  "language": "typescript"}]
        chunks = [{"id": "c1", "language": "typescript", "symbol_kind": "function",
                   "path": "a.ts", "line_start": 1, "line_end": 1,
                   "calls": ["helper"]}]
        report = resolve_cross_file_calls(files, chunks)
        assert isinstance(report, TsResolutionReport)


# ──────────────────────────────────────────────────────────────────────────────
# Live multilspy integration (optional)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.lsp
def test_resolve_against_live_multilspy(tmp_path):
    """Smoke: tiny TS project where main.ts calls helpers.helper()."""
    if not is_multilspy_available():
        pytest.skip("multilspy not installed")

    helpers = tmp_path / "helpers.ts"
    helpers.write_text("export function helper(): number {\n    return 1;\n}\n")
    main = tmp_path / "main.ts"
    main.write_text(
        "import { helper } from './helpers';\n"
        "export function caller(): number {\n"
        "    return helper();\n"
        "}\n"
    )

    files = [
        {"path": "helpers.ts", "content": helpers.read_text(), "language": "typescript"},
        {"path": "main.ts",    "content": main.read_text(),    "language": "typescript"},
    ]
    chunks = [{
        "id": "c1", "language": "typescript", "symbol_kind": "function",
        "path": "main.ts", "line_start": 2, "line_end": 4,
        "calls": ["helper"],
    }]

    report = resolve_cross_file_calls(
        files, chunks, repo_root=str(tmp_path), timeout_seconds=30,
    )
    assert isinstance(report, TsResolutionReport)
    if report.cross_file >= 1:
        assert chunks[0].get("cross_file_calls"), \
            "cross_file resolution succeeded but chunk wasn't enriched"
