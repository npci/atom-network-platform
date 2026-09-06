# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the shared inline-markdown tokenizer in `app.services.docx_builder`.

Both .docx assemblers (the services builder and the docgen one) render body text
through `_tokenize_inline_markdown`, so a miss here shows up as literal markdown
characters in every generated BRD / TSD / Product Note.

Regression under test: `_INLINE_RE` consumes an emphasis span in a single match,
so a code span nested inside bold was never re-scanned and its backticks reached
the page — ``**a `b` c**`` rendered as the visible text ``a `b` c``.

Pure test: the tokenizer is stdlib-only, so this imports without the app graph.
"""
from __future__ import annotations

from app.services.docx_builder import (
    _strip_inline_markdown,
    _tokenize_inline_markdown,
)

# The five values the module contract documents. Consumers switch on these, so
# the fix must not widen the vocabulary.
KINDS = {"plain", "bold", "italic", "bold_italic", "code"}


def _kinds(text: str) -> set[str]:
    return {k for _, k in _tokenize_inline_markdown(text)}


def test_plain_bold_italic_and_code_unchanged():
    assert _tokenize_inline_markdown("plain only") == [("plain only", "plain")]
    assert _tokenize_inline_markdown("**bold**") == [("bold", "bold")]
    assert _tokenize_inline_markdown("*it*") == [("it", "italic")]
    assert _tokenize_inline_markdown("`c`") == [("c", "code")]
    assert _tokenize_inline_markdown("***bi***") == [("bi", "bold_italic")]


def test_code_nested_in_bold_becomes_a_code_run():
    assert _tokenize_inline_markdown("**a `b` c**") == [
        ("a ", "bold"),
        ("b", "code"),
        (" c", "bold"),
    ]


def test_code_nested_in_italic_and_bold_italic():
    assert _tokenize_inline_markdown("*x `y` z*") == [
        ("x ", "italic"),
        ("y", "code"),
        (" z", "italic"),
    ]
    assert _tokenize_inline_markdown("***x `y` z***") == [
        ("x ", "bold_italic"),
        ("y", "code"),
        (" z", "bold_italic"),
    ]


def test_multiple_code_spans_inside_one_emphasis_span():
    assert _tokenize_inline_markdown("**a `b` c `d` e**") == [
        ("a ", "bold"),
        ("b", "code"),
        (" c ", "bold"),
        ("d", "code"),
        (" e", "bold"),
    ]


def test_emphasis_wrapping_only_a_code_span_collapses_to_code():
    assert _tokenize_inline_markdown("**`only`**") == [("only", "code")]


def test_no_backticks_or_asterisks_survive_into_any_segment():
    sample = (
        "**De-overload `clarification_response`** — promote the "
        "`message_kind`-multiplexed payloads to *real* task types"
    )
    for segment, _ in _tokenize_inline_markdown(sample):
        assert "`" not in segment
        assert "*" not in segment


def test_vocabulary_is_not_widened():
    sample = "**a `b` c** and *d `e` f* and ***g `h` i*** and `j` and plain"
    assert _kinds(sample) <= KINDS


def test_strip_drops_nested_markers_too():
    assert (
        _strip_inline_markdown("**De-overload `clarification_response`**")
        == "De-overload clarification_response"
    )


def test_empty_and_none_safe():
    assert _tokenize_inline_markdown("") == []
    assert _strip_inline_markdown("") == ""
