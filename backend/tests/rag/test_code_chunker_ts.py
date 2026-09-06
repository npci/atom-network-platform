# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tree-sitter AST code chunker tests (Slice 3).

Covers:
  - Feature-flag gating: OFF → dispatcher uses fallback, ON → tree-sitter path.
  - Per-language chunk extraction: file-level + symbol-level chunks emitted
    with correct metadata for Java, Python, TypeScript fixtures.
  - Unsupported language → dispatcher falls back.

Tests are pure — no DB, no LLM. Use the real tree-sitter grammars via fixture
files on disk (`tests/rag/fixtures/sample.{java,py,ts}`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.rag import code_chunker_langs, code_chunker_ts


FIXTURES = Path(__file__).resolve().parent / "fixtures"

LANG_FIXTURES = [
    ("sample.java", "java",       {"class"},               {"method", "constructor"}),
    ("sample.py",   "python",     {"class"},               {"function"}),
    ("sample.ts",   "typescript", {"class", "interface"},  {"method", "function"}),
]


# ──────────────────────────────────────────────────────────────────────────────
# Tree-sitter registry
# ──────────────────────────────────────────────────────────────────────────────

def test_registry_has_trio_supported():
    langs = code_chunker_langs.supported_languages()
    assert "java" in langs
    assert "python" in langs
    assert "typescript" in langs


# ──────────────────────────────────────────────────────────────────────────────
# Feature-flag gating
# ──────────────────────────────────────────────────────────────────────────────

def test_flag_off_invokes_fallback(monkeypatch):
    monkeypatch.setattr(settings, "use_tree_sitter_chunker", False)
    called = {"n": 0}

    def fallback():
        called["n"] += 1
        return [{"path": "x.java", "class_name": None, "method_name": None,
                 "content": "stub", "chunk_index": 0}]

    result = code_chunker_ts.chunk_source_file("x.java", "public class X {}", "java", fallback=fallback)
    assert called["n"] == 1
    assert result[0]["content"] == "stub"


def test_flag_on_uses_tree_sitter(monkeypatch):
    monkeypatch.setattr(settings, "use_tree_sitter_chunker", True)
    fixture = (FIXTURES / "sample.java").read_text()

    result = code_chunker_ts.chunk_source_file("sample.java", fixture, "java", fallback=lambda: [])
    assert len(result) > 1, "tree-sitter should emit file + at least one symbol chunk"
    assert result[0]["symbol_kind"] == "file"


def test_unsupported_language_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "use_tree_sitter_chunker", True)
    called = {"n": 0}

    def fallback():
        called["n"] += 1
        return []

    result = code_chunker_ts.chunk_source_file("x.rs", "fn main() {}", "rust", fallback=fallback)
    assert called["n"] == 1
    assert result == []


# ──────────────────────────────────────────────────────────────────────────────
# Per-language chunk extraction
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename,language,class_kinds,func_kinds", LANG_FIXTURES)
def test_chunker_emits_file_plus_symbols(filename, language, class_kinds, func_kinds):
    fixture_path = FIXTURES / filename
    content = fixture_path.read_text()

    chunks = code_chunker_langs.extract_chunks(filename, content, language)

    assert len(chunks) >= 3, f"expected ≥3 chunks (file + ≥1 class-like + ≥1 function-like) for {filename}"

    # Chunk 0 must be the file-level chunk with full content
    file_chunk = chunks[0]
    assert file_chunk["symbol_kind"] == "file"
    assert file_chunk["content"] == content
    assert file_chunk["symbol_name"] == filename
    assert file_chunk["language"] == language
    assert file_chunk["chunk_index"] == 0
    assert file_chunk["line_start"] == 1
    assert file_chunk["line_end"] == content.count("\n") + 1

    # At least one class-like chunk
    kinds_seen = {c["symbol_kind"] for c in chunks}
    assert kinds_seen & class_kinds, f"expected any of {class_kinds} in emitted kinds, got {kinds_seen}"

    # At least one function-like chunk
    assert kinds_seen & func_kinds, f"expected any of {func_kinds} in emitted kinds, got {kinds_seen}"


@pytest.mark.parametrize("filename,language,class_kinds,func_kinds", LANG_FIXTURES)
def test_chunker_populates_symbol_metadata(filename, language, class_kinds, func_kinds):
    """Every symbol chunk (not file) must have the 6 metadata fields populated."""
    fixture_path = FIXTURES / filename
    content = fixture_path.read_text()

    chunks = code_chunker_langs.extract_chunks(filename, content, language)

    symbol_chunks = [c for c in chunks if c["symbol_kind"] != "file"]
    assert symbol_chunks, f"no symbol-level chunks emitted for {filename}"

    for c in symbol_chunks:
        assert c["symbol_kind"], f"missing symbol_kind in {c}"
        assert c["symbol_name"], f"missing symbol_name in {c}"
        assert c["signature"], f"missing signature in {c}"
        assert c["line_start"] >= 1
        assert c["line_end"] >= c["line_start"]
        assert c["language"] == language

        # Chunks 1..n get chunk_index 1..n
        assert c["chunk_index"] >= 1

        # Legacy class_name / method_name compatibility fields
        if c["symbol_kind"] in ("method", "function", "constructor"):
            assert c["method_name"] == c["symbol_name"]


def test_java_specific_symbols_found():
    """Concrete check: the Java fixture has RateLimiter class + constructor +
    2 methods (acquire, remaining). All 4 must be extracted."""
    content = (FIXTURES / "sample.java").read_text()
    chunks = code_chunker_langs.extract_chunks("sample.java", content, "java")

    names = {c["symbol_name"] for c in chunks if c["symbol_kind"] != "file"}
    assert "RateLimiter" in names
    assert "acquire" in names
    assert "remaining" in names


def test_python_specific_symbols_found():
    content = (FIXTURES / "sample.py").read_text()
    chunks = code_chunker_langs.extract_chunks("sample.py", content, "python")

    names = {c["symbol_name"] for c in chunks if c["symbol_kind"] != "file"}
    assert "RateLimiter" in names
    assert "acquire" in names
    assert "log_event" in names


def test_typescript_specific_symbols_found(monkeypatch):
    # `formatName` is a one-line delegate (`return u.name.trim();`), which the
    # Phase 3.3 noise filter drops on purpose — so extraction is asserted with the
    # filter OFF, or this would be testing the filter rather than the grammar.
    monkeypatch.setattr(settings, "skip_noise_symbols", False)
    content = (FIXTURES / "sample.ts").read_text()
    chunks = code_chunker_langs.extract_chunks("sample.ts", content, "typescript")

    names = {c["symbol_name"] for c in chunks if c["symbol_kind"] != "file"}
    assert "User" in names           # interface
    assert "UserService" in names    # class
    assert "formatName" in names     # function


def test_typescript_trivial_delegate_dropped_by_noise_filter(monkeypatch):
    """The other half of the above: with Phase 3.3 on (the default), the one-line
    delegate is filtered out while the real symbols stay."""
    monkeypatch.setattr(settings, "skip_noise_symbols", True)
    content = (FIXTURES / "sample.ts").read_text()
    chunks = code_chunker_langs.extract_chunks("sample.ts", content, "typescript")

    names = {c["symbol_name"] for c in chunks if c["symbol_kind"] != "file"}
    assert "formatName" not in names
    assert {"User", "UserService"} <= names
