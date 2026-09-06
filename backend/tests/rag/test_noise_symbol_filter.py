# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 3.3 — noise-symbol filter tests.

The filter lives in `app.rag.code_chunker_langs.is_trivial_symbol`. These
tests exercise the pure classifier — no tree-sitter needed.
"""
from __future__ import annotations

import pytest

from app.rag.code_chunker_langs import (
    NOISE_SYMBOL_MAX_CHARS,
    is_trivial_symbol,
)


def _java_method(content: str) -> dict:
    return {
        "symbol_kind": "method",
        "content": content,
        "symbol_name": "x",
    }


# ── Trivial cases — should be filtered ───────────────────────────────────────

def test_one_line_getter_filtered():
    chunk = _java_method(
        "public String getName() {\n"
        "    return name;\n"
        "}"
    )
    assert is_trivial_symbol(chunk) is True


def test_one_line_setter_filtered():
    chunk = _java_method(
        "public void setName(String name) {\n"
        "    this.name = name;\n"
        "}"
    )
    assert is_trivial_symbol(chunk) is True


def test_simple_delegate_filtered():
    chunk = _java_method(
        "public String getInner() {\n"
        "    return wrapped.getInner();\n"
        "}"
    )
    assert is_trivial_symbol(chunk) is True


def test_empty_body_filtered():
    chunk = _java_method(
        "public void initialize() {\n"
        "}"
    )
    assert is_trivial_symbol(chunk) is True


def test_python_self_setter_filtered():
    chunk = {
        "symbol_kind": "function",
        "content": "def set_name(self, name):\n    self.name = name\n",
    }
    assert is_trivial_symbol(chunk) is True


# ── Non-trivial cases — must NOT be filtered ─────────────────────────────────

def test_method_with_logic_kept():
    chunk = _java_method(
        "public boolean validate(String token) {\n"
        "    if (token == null) return false;\n"
        "    byte[] sig = hmac(token, SECRET);\n"
        "    return MessageDigest.isEqual(sig, decode(token));\n"
        "}"
    )
    assert is_trivial_symbol(chunk) is False


def test_long_method_kept_even_if_simple_lines():
    # A method whose body is long enough that it can't be a one-liner getter.
    body = "    return x;\n" * 20
    chunk = _java_method(f"public String f() {{\n{body}}}")
    # Char-length above threshold → not trivial regardless of shape.
    assert len(chunk["content"]) > NOISE_SYMBOL_MAX_CHARS
    assert is_trivial_symbol(chunk) is False


def test_getter_with_side_effect_kept():
    chunk = _java_method(
        "public String getName() {\n"
        "    log.debug(\"audit\");\n"
        "    return name;\n"
        "}"
    )
    # The log call counts as a non-trivial body line. 3 body lines now,
    # which also fails the "≤2 lines" gate.
    assert is_trivial_symbol(chunk) is False


def test_complex_setter_kept():
    chunk = _java_method(
        "public void setName(String name) {\n"
        "    if (name == null) throw new IllegalArgumentException();\n"
        "    this.name = name;\n"
        "}"
    )
    assert is_trivial_symbol(chunk) is False
