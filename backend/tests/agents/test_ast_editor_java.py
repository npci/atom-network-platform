# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 16 — aider-style SEARCH/REPLACE patch editor.

Pure — no LLM, no I/O. `parse_patches` and `apply_patches` are pure
functions; `generate_patches` uses an injected `call_llm_fn` so we pass
stubs instead of hitting the network.
"""
from __future__ import annotations

import pytest

from app.agents import ast_editor_java as editor


# ──────────────────────────────────────────────────────────────────────────────
# parse_patches — happy paths
# ──────────────────────────────────────────────────────────────────────────────

_SIMPLE_BLOCK = """File: src/main/java/Foo.java
<<<<<<< SEARCH
    public int add(int a, int b) {
        return a + b;
    }
=======
    public int add(int a, int b) {
        if (a < 0 || b < 0) throw new IllegalArgumentException();
        return a + b;
    }
>>>>>>> REPLACE
"""


class TestParseSinglePatch:
    def test_single_block_parsed(self):
        patches = editor.parse_patches(_SIMPLE_BLOCK)
        assert len(patches) == 1
        p = patches[0]
        assert p.file_path == "src/main/java/Foo.java"
        assert "return a + b;" in p.search
        assert "IllegalArgumentException" in p.replace

    def test_search_preserves_indentation(self):
        patches = editor.parse_patches(_SIMPLE_BLOCK)
        assert patches[0].search.startswith("    public")

    def test_multiple_blocks_same_file(self):
        text = """File: Foo.java
<<<<<<< SEARCH
a
=======
b
>>>>>>> REPLACE
File: Foo.java
<<<<<<< SEARCH
c
=======
d
>>>>>>> REPLACE
"""
        patches = editor.parse_patches(text)
        assert len(patches) == 2
        assert all(p.file_path == "Foo.java" for p in patches)
        assert patches[0].replace == "b"
        assert patches[1].replace == "d"

    def test_multiple_blocks_different_files(self):
        text = """File: A.java
<<<<<<< SEARCH
x
=======
y
>>>>>>> REPLACE

File: B.java
<<<<<<< SEARCH
z
=======
w
>>>>>>> REPLACE
"""
        patches = editor.parse_patches(text)
        assert [p.file_path for p in patches] == ["A.java", "B.java"]


class TestParseEmptyAndMalformed:
    def test_empty_input_returns_empty(self):
        assert editor.parse_patches("") == []
        assert editor.parse_patches(None or "") == []

    def test_preamble_ignored(self):
        text = "Hey here are some patches:\n\n" + _SIMPLE_BLOCK
        assert len(editor.parse_patches(text)) == 1

    def test_file_header_without_block_discarded(self):
        text = """File: A.java
<<<<<<< SEARCH
search
=======
replace
>>>>>>> REPLACE

File: B.java
This is just prose without a SEARCH block at all.
"""
        patches = editor.parse_patches(text)
        assert len(patches) == 1
        assert patches[0].file_path == "A.java"

    def test_missing_replace_close_drops_block(self):
        text = """File: A.java
<<<<<<< SEARCH
search
=======
replace but no close
"""
        assert editor.parse_patches(text) == []

    def test_missing_search_close_drops_block(self):
        text = """File: A.java
<<<<<<< SEARCH
search but never closes
>>>>>>> REPLACE
"""
        assert editor.parse_patches(text) == []

    def test_empty_search_block_parsed_as_append(self):
        """Empty SEARCH block is valid — it means 'create / append'."""
        text = """File: New.java
<<<<<<< SEARCH
=======
public class New {}
>>>>>>> REPLACE
"""
        patches = editor.parse_patches(text)
        assert len(patches) == 1
        assert patches[0].search == ""
        assert patches[0].replace == "public class New {}"


# ──────────────────────────────────────────────────────────────────────────────
# apply_patches — happy paths
# ──────────────────────────────────────────────────────────────────────────────

class TestApplySinglePatch:
    def test_exact_match_replaced(self):
        files = {"A.java": "class A { int x = 1; }"}
        patches = [editor.Patch(file_path="A.java", search="int x = 1;", replace="int x = 42;")]
        result = editor.apply_patches(files, patches)
        assert result.applied_count == 1
        assert result.failures == []
        assert result.updated_files["A.java"] == "class A { int x = 42; }"

    def test_unrelated_files_unchanged(self):
        files = {
            "A.java": "class A { int x = 1; }",
            "B.java": "class B {}",
        }
        patches = [editor.Patch("A.java", "int x = 1;", "int x = 42;")]
        result = editor.apply_patches(files, patches)
        assert result.updated_files["B.java"] == "class B {}"

    def test_multiple_patches_applied_independently(self):
        files = {"A.java": "x\n\ny\n\nz"}
        patches = [
            editor.Patch("A.java", "x", "X"),
            editor.Patch("A.java", "z", "Z"),
        ]
        result = editor.apply_patches(files, patches)
        assert result.applied_count == 2
        assert result.updated_files["A.java"] == "X\n\ny\n\nZ"

    def test_does_not_mutate_caller_files(self):
        files = {"A.java": "class A {}"}
        original_copy = dict(files)
        patches = [editor.Patch("A.java", "class A {}", "class A { int x; }")]
        editor.apply_patches(files, patches)
        assert files == original_copy  # unchanged

    def test_empty_search_appends_to_existing_file(self):
        files = {"A.java": "class A {}\n"}
        patches = [editor.Patch("A.java", "", "// appended comment\n")]
        result = editor.apply_patches(files, patches)
        assert result.applied_count == 1
        assert result.failures == []
        assert result.updated_files["A.java"] == "class A {}\n// appended comment\n"

    def test_empty_search_creates_new_file(self):
        files = {"A.java": "class A {}"}
        patches = [editor.Patch("New.java", "", "public class New {}")]
        result = editor.apply_patches(files, patches)
        assert result.applied_count == 1
        assert result.updated_files["New.java"] == "public class New {}"


class TestApplyFailures:
    def test_file_not_found_failure(self):
        files = {"A.java": "class A {}"}
        patches = [editor.Patch("Missing.java", "x", "y")]
        result = editor.apply_patches(files, patches)
        assert result.applied_count == 0
        assert len(result.failures) == 1
        assert result.failures[0].reason == "file_not_found"
        assert result.failures[0].file_path == "Missing.java"

    def test_pattern_not_found_failure(self):
        files = {"A.java": "class A { int x = 1; }"}
        patches = [editor.Patch("A.java", "int nonexistent = 99;", "int y = 2;")]
        result = editor.apply_patches(files, patches)
        assert result.applied_count == 0
        assert len(result.failures) == 1
        assert result.failures[0].reason == "pattern_not_found"

    def test_ambiguous_match_failure(self):
        """Search text that appears twice → patch refused."""
        files = {"A.java": "duplicate\nduplicate\n"}
        patches = [editor.Patch("A.java", "duplicate", "UNIQUE")]
        result = editor.apply_patches(files, patches)
        assert result.applied_count == 0
        assert result.failures[0].reason == "ambiguous_match"
        # Original unchanged
        assert result.updated_files["A.java"] == "duplicate\nduplicate\n"

    def test_one_failure_does_not_block_others(self):
        files = {"A.java": "class A {}", "B.java": "class B { int y; }"}
        patches = [
            editor.Patch("Missing.java", "x", "y"),       # fails: file_not_found
            editor.Patch("B.java", "int y;", "int z;"),   # succeeds
        ]
        result = editor.apply_patches(files, patches)
        assert result.applied_count == 1
        assert len(result.failures) == 1
        assert result.updated_files["B.java"] == "class B { int z; }"

    def test_invalid_path_captured(self):
        files = {"A.java": "x"}
        patches = [editor.Patch("", "x", "y")]
        result = editor.apply_patches(files, patches)
        assert len(result.failures) == 1
        assert result.failures[0].reason == "invalid_path"


# ──────────────────────────────────────────────────────────────────────────────
# Integration — parse + apply combined
# ──────────────────────────────────────────────────────────────────────────────

class TestParseApplyIntegration:
    def test_full_roundtrip_against_realistic_llm_output(self):
        llm_output = """Here are the patches:

File: src/retry/RateLimiter.java
<<<<<<< SEARCH
    public boolean acquire() {
        this.count += 1;
        return this.count <= this.limit;
    }
=======
    public boolean acquire() {
        this.count += 1;
        if (this.count > this.limit * 0.9) {
            metrics.warnHighUsage();
        }
        return this.count <= this.limit;
    }
>>>>>>> REPLACE
"""
        files = {
            "src/retry/RateLimiter.java": (
                "public class RateLimiter {\n"
                "    private int limit;\n"
                "    private int count;\n"
                "\n"
                "    public boolean acquire() {\n"
                "        this.count += 1;\n"
                "        return this.count <= this.limit;\n"
                "    }\n"
                "}\n"
            ),
        }
        patches = editor.parse_patches(llm_output)
        assert len(patches) == 1
        result = editor.apply_patches(files, patches)
        assert result.applied_count == 1
        assert result.failures == []
        assert "metrics.warnHighUsage" in result.updated_files["src/retry/RateLimiter.java"]


# ──────────────────────────────────────────────────────────────────────────────
# build_system_prompt
# ──────────────────────────────────────────────────────────────────────────────

class TestSystemPrompt:
    def test_mentions_format_delimiters(self):
        s = editor.build_system_prompt()
        assert "File:" in s
        assert "<<<<<<< SEARCH" in s
        assert "=======" in s
        assert ">>>>>>> REPLACE" in s

    def test_explains_empty_search_semantics(self):
        s = editor.build_system_prompt()
        assert "EMPTY SEARCH" in s or "empty SEARCH" in s.lower()

    def test_forbids_markdown_fences(self):
        s = editor.build_system_prompt()
        assert "NO markdown" in s or "no markdown" in s.lower()


# ──────────────────────────────────────────────────────────────────────────────
# generate_patches — injected LLM
# ──────────────────────────────────────────────────────────────────────────────

class TestGeneratePatches:
    @pytest.mark.asyncio
    async def test_returns_parsed_patches_on_success(self):
        async def fake_llm(system, messages):
            return _SIMPLE_BLOCK

        patches, raw = await editor.generate_patches(
            files={"src/main/java/Foo.java": "public class Foo {}"},
            task="Add input validation to add().",
            call_llm_fn=fake_llm,
        )
        assert len(patches) == 1
        assert patches[0].file_path == "src/main/java/Foo.java"
        assert raw == _SIMPLE_BLOCK

    @pytest.mark.asyncio
    async def test_empty_files_short_circuits(self):
        called = {"n": 0}

        async def fake_llm(system, messages):
            called["n"] += 1
            return _SIMPLE_BLOCK

        patches, raw = await editor.generate_patches(
            files={}, task="any task", call_llm_fn=fake_llm,
        )
        assert patches == []
        assert raw == ""
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_empty_task_short_circuits(self):
        called = {"n": 0}

        async def fake_llm(system, messages):
            called["n"] += 1
            return _SIMPLE_BLOCK

        patches, raw = await editor.generate_patches(
            files={"x.java": "x"}, task="   ", call_llm_fn=fake_llm,
        )
        assert patches == []
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_llm_exception_returns_empty(self):
        async def fake_llm(system, messages):
            raise RuntimeError("LLM down")

        patches, raw = await editor.generate_patches(
            files={"x.java": "x"}, task="do thing", call_llm_fn=fake_llm,
        )
        assert patches == []
        assert raw == ""

    @pytest.mark.asyncio
    async def test_non_string_response_returns_empty(self):
        async def fake_llm(system, messages):
            return 42   # ill-typed

        patches, raw = await editor.generate_patches(
            files={"x.java": "x"}, task="do thing", call_llm_fn=fake_llm,
        )
        assert patches == []
        assert raw == ""

    @pytest.mark.asyncio
    async def test_malformed_response_returns_empty_patches(self):
        async def fake_llm(system, messages):
            return "I think we should change line 5 to be nicer."

        patches, raw = await editor.generate_patches(
            files={"x.java": "x"}, task="do thing", call_llm_fn=fake_llm,
        )
        assert patches == []
        assert raw == "I think we should change line 5 to be nicer."
