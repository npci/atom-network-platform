# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the Java LSP resolver (Sub-slice 24a).

Mirrors `test_lsp_resolver_python.py` and `_typescript.py`. Layered:
  1. Pure helpers
  2. `resolve_cross_file_calls` orchestration with multilspy stubbed
  3. One `@pytest.mark.lsp` integration that runs live multilspy
     (skipped when multilspy / eclipse-jdt is unavailable)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.rag import lsp_resolver_java as java_lsp
from app.rag.lsp_resolver_java import (
    JavaCallSiteRequest,
    JavaCrossFileCall,
    JavaResolutionReport,
    _collect_call_sites,
    _find_first_occurrence,
    _is_java_file,
    _materialise_repo,
    attach_cross_file_calls,
    is_multilspy_available,
    resolve_cross_file_calls,
)


# ──────────────────────────────────────────────────────────────────────────────
# _is_java_file
# ──────────────────────────────────────────────────────────────────────────────

class TestIsJavaFile:

    def test_java_extension(self):
        assert _is_java_file("Foo.java") is True
        assert _is_java_file("src/main/java/com/network/Foo.java") is True

    def test_uppercase_extension(self):
        assert _is_java_file("Foo.JAVA") is True

    def test_python_rejected(self):
        assert _is_java_file("foo.py") is False

    def test_typescript_rejected(self):
        assert _is_java_file("foo.ts") is False

    def test_no_extension(self):
        assert _is_java_file("Makefile") is False

    def test_empty_path(self):
        assert _is_java_file("") is False


# ──────────────────────────────────────────────────────────────────────────────
# _find_first_occurrence
# ──────────────────────────────────────────────────────────────────────────────

class TestFindFirstOccurrence:

    def test_simple_match(self):
        lines = ["public class Foo {", "    void bar() { helper(); }", "}"]
        out = _find_first_occurrence(lines, "helper", line_start=1, line_end=3)
        line, char = out
        assert line == 1
        assert lines[line][char:char+len("helper")] == "helper"

    def test_word_boundary_avoids_substring(self):
        lines = ["public class Foo {", "    void bar() { helperOne(); }", "}"]
        out = _find_first_occurrence(lines, "helper", line_start=1, line_end=3)
        assert out is None

    def test_no_match(self):
        lines = ["public class Foo {", "    void bar() { return; }", "}"]
        assert _find_first_occurrence(lines, "nope") is None

    def test_empty_needle(self):
        assert _find_first_occurrence(["a", "b"], "") is None


# ──────────────────────────────────────────────────────────────────────────────
# _materialise_repo
# ──────────────────────────────────────────────────────────────────────────────

class TestMaterialiseRepo:

    def test_writes_files(self, tmp_path):
        files = [
            {"path": "src/main/java/com/network/Foo.java",
             "content": "package com.network; public class Foo {}\n"},
            {"path": "src/main/java/com/network/Bar.java",
             "content": "package com.network; public class Bar {}\n"},
        ]
        root = _materialise_repo(files, parent_dir=str(tmp_path))
        try:
            foo = Path(root) / "src/main/java/com/network/Foo.java"
            bar = Path(root) / "src/main/java/com/network/Bar.java"
            assert foo.read_text().startswith("package com.network")
            assert bar.read_text().startswith("package com.network")
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
        files = [{"path": "../escape.java", "content": "evil"}]
        root = _materialise_repo(files, parent_dir=str(tmp_path))
        try:
            siblings = list(tmp_path.iterdir())
            assert all(s.name.startswith("lsp_java_repo_") for s in siblings)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────────────
# _collect_call_sites
# ──────────────────────────────────────────────────────────────────────────────

class TestCollectCallSites:

    def test_extracts_per_call_for_method(self):
        chunks = [{
            "id": "c1", "language": "java", "symbol_kind": "method",
            "path": "Foo.java", "line_start": 1, "line_end": 3,
            "calls": ["helper", "other"],
        }]
        content = {
            "Foo.java": "public class Foo {\n    void f() { helper(); other(); }\n}\n",
        }
        out = _collect_call_sites(chunks, content)
        names = {r.callee_symbol for r in out}
        assert names == {"helper", "other"}

    def test_constructor_kind_handled(self):
        chunks = [{
            "id": "c1", "language": "java", "symbol_kind": "constructor",
            "path": "Foo.java", "line_start": 1, "line_end": 3,
            "calls": ["initThing"],
        }]
        content = {
            "Foo.java": "public class Foo {\n    Foo() { initThing(); }\n}\n",
        }
        out = _collect_call_sites(chunks, content)
        assert len(out) == 1
        assert out[0].callee_symbol == "initThing"

    def test_python_chunks_skipped(self):
        chunks = [{
            "id": "c1", "language": "python", "symbol_kind": "method",
            "path": "foo.py", "calls": ["helper"],
        }]
        out = _collect_call_sites(chunks, {})
        assert out == []

    def test_class_chunks_skipped(self):
        """Class-level Java chunks have no `calls` of their own — only
        their method/constructor children produce call sites."""
        chunks = [{
            "id": "c1", "language": "java", "symbol_kind": "class",
            "path": "Foo.java", "calls": [],
        }]
        out = _collect_call_sites(chunks, {})
        assert out == []

    def test_calls_not_found_in_body_skipped(self):
        chunks = [{
            "id": "c1", "language": "java", "symbol_kind": "method",
            "path": "Foo.java", "line_start": 1, "line_end": 2,
            "calls": ["nope"],
        }]
        content = {"Foo.java": "public class Foo {\n    void f() {}\n}\n"}
        assert _collect_call_sites(chunks, content) == []


# ──────────────────────────────────────────────────────────────────────────────
# attach_cross_file_calls
# ──────────────────────────────────────────────────────────────────────────────

class TestAttachCrossFileCalls:

    def test_attaches_with_java_language(self):
        chunks = [{"id": "c1"}]
        attached = attach_cross_file_calls(chunks, {
            "c1": [JavaCrossFileCall(
                caller_chunk_id="c1", callee_symbol="x",
                callee_path="other/Bar.java", line=10,
            )],
        })
        assert attached == 1
        assert chunks[0]["cross_file_calls"][0]["language"] == "java"
        assert chunks[0]["cross_file_calls"][0]["callee_path"] == "other/Bar.java"

    def test_merges_with_existing(self):
        chunks = [{"id": "c1", "cross_file_calls": [
            {"callee_symbol": "py", "callee_path": "x.py", "language": "python"},
        ]}]
        attach_cross_file_calls(chunks, {
            "c1": [JavaCrossFileCall(
                caller_chunk_id="c1", callee_symbol="ja",
                callee_path="X.java", line=5,
            )],
        })
        cf = chunks[0]["cross_file_calls"]
        assert len(cf) == 2
        assert {e["language"] for e in cf} == {"python", "java"}

    def test_unknown_caller_skipped(self):
        chunks = [{"id": "c1"}]
        attached = attach_cross_file_calls(chunks, {
            "unknown": [JavaCrossFileCall(
                caller_chunk_id="unknown", callee_symbol="x",
                callee_path="o.java",
            )],
        })
        assert attached == 0


# ──────────────────────────────────────────────────────────────────────────────
# resolve_cross_file_calls — orchestration with multilspy stubbed
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveOrchestration:

    def test_no_java_files_returns_empty_report(self):
        report = resolve_cross_file_calls(
            files=[{"path": "main.py", "content": "x", "language": "python"}],
            file_chunks=[],
        )
        assert isinstance(report, JavaResolutionReport)
        assert report.requests == 0

    def test_multilspy_unavailable_records_failure(self, monkeypatch):
        monkeypatch.setattr(java_lsp, "is_multilspy_available", lambda: False)
        files = [{"path": "Foo.java", "content": "class Foo {}", "language": "java"}]
        chunks = [{
            "id": "c1", "language": "java", "symbol_kind": "method",
            "path": "Foo.java", "line_start": 1, "line_end": 1, "calls": ["x"],
        }]
        report = resolve_cross_file_calls(files, chunks)
        assert report.failures >= 1
        assert any("multilspy" in r for r in report.failure_reasons)

    def test_no_call_sites_short_circuits(self, monkeypatch):
        monkeypatch.setattr(java_lsp, "is_multilspy_available", lambda: True)
        files = [{"path": "Foo.java", "content": "class Foo {}", "language": "java"}]
        chunks = []
        report = resolve_cross_file_calls(files, chunks)
        assert report.requests == 0
        assert report.failures == 0

    def test_happy_path_attaches_cross_file_call(self, monkeypatch):
        monkeypatch.setattr(java_lsp, "is_multilspy_available", lambda: True)

        async def fake_lsp(repo_root, requests, *, timeout_seconds=90):
            r = JavaResolutionReport()
            by_caller: dict[str, list[JavaCrossFileCall]] = {}
            for req in requests:
                r.requests += 1
                r.resolved += 1
                r.cross_file += 1
                by_caller.setdefault(req.caller_chunk_id, []).append(
                    JavaCrossFileCall(
                        caller_chunk_id=req.caller_chunk_id,
                        callee_symbol=req.callee_symbol,
                        callee_path="util/Helpers.java",
                        line=42,
                    ),
                )
            return by_caller, r
        monkeypatch.setattr(java_lsp, "_ask_lsp_for_definitions", fake_lsp)

        files = [{
            "path": "Main.java",
            "content": "public class Main {\n    void run() { helper(); }\n}\n",
            "language": "java",
        }]
        chunks = [{
            "id": "c1", "language": "java", "symbol_kind": "method",
            "path": "Main.java", "line_start": 1, "line_end": 3,
            "calls": ["helper"],
        }]
        report = resolve_cross_file_calls(files, chunks)
        assert report.cross_file == 1
        assert chunks[0]["cross_file_calls"][0]["callee_path"] == "util/Helpers.java"
        assert chunks[0]["cross_file_calls"][0]["language"] == "java"

    def test_jdt_internal_paths_treated_as_same_file_not_cross(self, monkeypatch):
        """eclipse-jdt may return URIs like `jdt://contents/...` for JDK
        classes — those must not become cross-file edges."""
        monkeypatch.setattr(java_lsp, "is_multilspy_available", lambda: True)

        async def fake_lsp(repo_root, requests, *, timeout_seconds=90):
            r = JavaResolutionReport()
            for req in requests:
                r.requests += 1
                r.resolved += 1
                # Bucket as same_file (not cross_file) per resolver design
                r.same_file += 1
            return {}, r   # by_caller empty → no enrichment
        monkeypatch.setattr(java_lsp, "_ask_lsp_for_definitions", fake_lsp)

        files = [{"path": "Main.java",
                  "content": "public class Main { void f() { System.out.println(); } }",
                  "language": "java"}]
        chunks = [{"id": "c1", "language": "java", "symbol_kind": "method",
                   "path": "Main.java", "line_start": 1, "line_end": 1,
                   "calls": ["println"]}]
        report = resolve_cross_file_calls(files, chunks)
        assert report.cross_file == 0
        assert "cross_file_calls" not in chunks[0]

    def test_outer_exception_swallowed(self, monkeypatch):
        monkeypatch.setattr(java_lsp, "is_multilspy_available", lambda: True)

        async def boom(repo_root, requests, *, timeout_seconds=90):
            raise RuntimeError("simulated jdt crash")
        monkeypatch.setattr(java_lsp, "_ask_lsp_for_definitions", boom)

        files = [{"path": "Main.java", "content": "class M { void f(){ helper(); } }",
                  "language": "java"}]
        chunks = [{"id": "c1", "language": "java", "symbol_kind": "method",
                   "path": "Main.java", "line_start": 1, "line_end": 1,
                   "calls": ["helper"]}]
        report = resolve_cross_file_calls(files, chunks)
        assert isinstance(report, JavaResolutionReport)


# ──────────────────────────────────────────────────────────────────────────────
# Live multilspy integration (optional — eclipse-jdt is heavy)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.lsp
def test_resolve_against_live_multilspy(tmp_path):
    """Smoke: tiny Java project where Main.java calls Helpers.helper().
    Skipped automatically when multilspy / eclipse-jdt is unavailable.

    NOTE: eclipse-jdt downloads on first use (~80MB) and warm-up is slow.
    This test is `@pytest.mark.lsp` — only run when explicitly requested."""
    if not is_multilspy_available():
        pytest.skip("multilspy not installed")

    helpers = tmp_path / "Helpers.java"
    helpers.write_text(
        "public class Helpers {\n"
        "    public static int helper() { return 1; }\n"
        "}\n"
    )
    main = tmp_path / "Main.java"
    main.write_text(
        "public class Main {\n"
        "    public int caller() {\n"
        "        return Helpers.helper();\n"
        "    }\n"
        "}\n"
    )

    files = [
        {"path": "Helpers.java", "content": helpers.read_text(), "language": "java"},
        {"path": "Main.java",    "content": main.read_text(),    "language": "java"},
    ]
    chunks = [{
        "id": "c1", "language": "java", "symbol_kind": "method",
        "path": "Main.java", "line_start": 2, "line_end": 4,
        "calls": ["helper"],
    }]

    report = resolve_cross_file_calls(
        files, chunks, repo_root=str(tmp_path), timeout_seconds=120,
    )
    assert isinstance(report, JavaResolutionReport)
    if report.cross_file >= 1:
        assert chunks[0].get("cross_file_calls"), \
            "cross_file resolution succeeded but chunk wasn't enriched"
