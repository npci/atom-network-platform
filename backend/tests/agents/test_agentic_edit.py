# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The 4-level edit ladder (§8) — pure, exhaustively tested before the tools use it."""
import pytest

from app.agents.agentic_edit import apply_edit, EditError

SRC = "class A {\n    int x = 1;\n    int y = 2;\n}\n"


def test_level1_exact_unique():
    out, lvl = apply_edit(SRC, "int x = 1;", "int x = 42;")
    assert lvl == 1 and "int x = 42;" in out and "int y = 2;" in out


def test_level1_ambiguous_is_refused():
    src = "a = 1\na = 1\n"
    with pytest.raises(EditError):
        apply_edit(src, "a = 1", "a = 2")


def test_level2_trailing_whitespace_tolerated():
    # First line carries trailing whitespace in the file; the embedded newline
    # makes exact (level 1) fail, so trailing-strip (level 2) matches.
    src = "foo = bar \nbaz = qux\n"          # trailing space after 'bar'
    out, lvl = apply_edit(src, "foo = bar\nbaz = qux", "X\nY")
    assert lvl == 2 and out == "X\nY\n"


def test_level3_indentation_insensitive():
    # old is dedented relative to the indented file block → level 3.
    src = "    int x = 1;\n    int y = 2;\n"
    out, lvl = apply_edit(src, "int x = 1;\nint y = 2;", "int x = 10;\nint y = 20;")
    assert lvl == 3 and "int x = 10;" in out and "int y = 20;" in out


def test_level4_collapse_internal_whitespace():
    src = "if (a   ==   b) {\n}\n"
    out, lvl = apply_edit(src, "if (a == b) {", "if (a != b) {")
    assert lvl == 4 and "if (a != b) {" in out


def test_ambiguous_at_normalized_level_is_refused():
    # Two indentation-insensitive matches → refuse (never pick the first).
    src = "    x = 1\n        x = 1\n"
    with pytest.raises(EditError):
        apply_edit(src, "x = 1", "x = 2")


def test_not_found():
    with pytest.raises(EditError):
        apply_edit(SRC, "nonexistent line", "whatever")


def test_empty_and_identical_refused():
    with pytest.raises(EditError):
        apply_edit(SRC, "", "x")
    with pytest.raises(EditError):
        apply_edit(SRC, "int x = 1;", "int x = 1;")


def test_newline_preserved_when_new_omits_it():
    # A normalized (line-block) replacement whose new_string omits the trailing
    # newline must not pull the following line up.
    src = "    line one\n    line two\n"
    out, lvl = apply_edit(src, "line one\nline two", "ONE\nTWO")  # dedented → level 3
    assert lvl == 3 and out == "ONE\nTWO\n"
