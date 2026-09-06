# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-language tree-sitter node mappings + symbol extraction (Slice 3).

Central registry of: which tree-sitter grammar to load, which AST node types
count as "symbols" for chunking, and how that node type maps to our internal
`symbol_kind` vocabulary.

Adding a new language (Slices 22a–22o) means:
  1. `pip install tree-sitter-<lang>` (+ requirements.txt entry),
  2. add a `(grammar_module, language_fn, kind_map)` entry to `_REGISTRY_SPEC`
     in `_build_registry()` below.
No new file needed per language — deliberately consolidated.

All imports are lazy so the module is safe to import even when tree-sitter
grammar packages are absent (the feature flag `USE_TREE_SITTER_CHUNKER`
gates actual use).
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Phase 3.3 — Noise-symbol filter
# ──────────────────────────────────────────────────────────────────────────────

# A symbol body is "trivial" — and therefore noise we shouldn't index as its
# own chunk — when ALL of these are true:
#   1. It's short (< NOISE_SYMBOL_MAX_CHARS chars after stripping whitespace).
#   2. After removing the declaration line + trailing brace, the body matches
#      one of the trivial-shape patterns below.
#
# Why we still keep the file-level chunk: the symbol's source still lives in
# the file row (`symbol_kind='file'`), so a BM25/grep-style query for the
# method name still hits the file. We just don't waste an embedding + an
# HNSW index entry on each one-line getter.
#
# These thresholds are intentionally conservative — better to keep a
# borderline chunk than aggressively drop content the agents may need.
NOISE_SYMBOL_MAX_CHARS = 80

# Lines that count as "no real logic". Anchored, allow optional `@Override` /
# annotations / modifiers / typed return / Python self-types.
_TRIVIAL_BODY_LINES = (
    re.compile(r"^\s*return\s+[A-Za-z_][\w\.]*\s*;?\s*$"),
    re.compile(r"^\s*return\s+[A-Za-z_][\w\.]*\([^()]*\)\s*;?\s*$"),  # delegate
    re.compile(r"^\s*this\.[A-Za-z_]\w*\s*=\s*[A-Za-z_]\w*\s*;\s*$"),  # setter
    re.compile(r"^\s*self\.[A-Za-z_]\w*\s*=\s*[A-Za-z_]\w*\s*$"),     # py setter
)


def _strip_decl_and_braces(content: str) -> list[str]:
    """Best-effort: return body lines minus the declaration line and the
    closing brace. Works on Java/TS/Python idiomatic shapes. Pure string
    work — no AST traversal."""
    lines = [ln.rstrip() for ln in (content or "").splitlines() if ln.strip()]
    if not lines:
        return []
    # Drop the first line (declaration) and the last line if it's just `}`
    # or a closing colon/pass for Python.
    body = lines[1:] if len(lines) > 1 else []
    if body and body[-1].strip() in ("}", "};"):
        body = body[:-1]
    # Strip Python decorators at the top
    while body and body[0].strip().startswith("@"):
        body = body[1:]
    return body


def is_trivial_symbol(chunk: dict) -> bool:
    """True when this symbol chunk is a one-liner getter/setter/delegate
    that doesn't deserve its own embedding row.

    Pre-conditions enforced by the caller:
      - chunk['symbol_kind'] is method/function/constructor (not 'file').
      - The Phase 3.3 feature flag is on.

    Returns False for anything we can't confidently classify — fail-keep is
    the right default. False positives cost retrieval noise; false negatives
    cost embedding bandwidth (recoverable on the next ingest).
    """
    content = chunk.get("content") or ""
    if len(content.strip()) >= NOISE_SYMBOL_MAX_CHARS:
        return False

    body = _strip_decl_and_braces(content)
    if not body:
        # Empty body — likely an abstract method or `def foo(): pass`. Not
        # useful to embed on its own.
        return True
    if len(body) > 2:
        # More than two statement lines → not a one-liner getter.
        return False

    # Every remaining body line must match one of the trivial shapes.
    for line in body:
        if not any(pat.match(line) for pat in _TRIVIAL_BODY_LINES):
            return False
    return True


def _noise_filter_enabled() -> bool:
    """Settings-aware check. Importing settings lazily so this module loads
    even when pydantic-settings isn't installed (tests, lightweight tools)."""
    try:
        from app.core.config import settings
        return bool(getattr(settings, "skip_noise_symbols", True))
    except Exception:
        return True


# ──────────────────────────────────────────────────────────────────────────────
# Lazy registry
# ──────────────────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, dict[str, Any]] | None = None


def _build_registry() -> dict[str, dict[str, Any]]:
    """Lazily construct the language registry.

    Imports happen here (not at module top-level) so environments without
    tree-sitter grammar packages installed don't crash at import time.
    """
    try:
        from tree_sitter import Language
        import tree_sitter_java
        import tree_sitter_python
        import tree_sitter_typescript as tst
    except ImportError as e:
        logger.warning("tree-sitter grammar packages not available: %s", e)
        return {}

    # Node types we recognise as "symbols" to emit as standalone chunks.
    # Map AST node_type → internal symbol_kind vocabulary.
    java_kinds = {
        "class_declaration":       "class",
        "interface_declaration":   "interface",
        "enum_declaration":        "enum",
        "record_declaration":      "record",
        "method_declaration":      "method",
        "constructor_declaration": "constructor",
    }
    python_kinds = {
        "class_definition":            "class",
        "function_definition":         "function",
        "decorated_definition":        "function",  # decorated fn or class
    }
    typescript_kinds = {
        "class_declaration":     "class",
        "interface_declaration": "interface",
        "enum_declaration":      "enum",
        "function_declaration":  "function",
        "method_definition":     "method",
        "abstract_class_declaration": "class",
        "abstract_method_signature":  "method",
    }

    return {
        "java":       {"language": Language(tree_sitter_java.language()),     "symbol_kinds": java_kinds},
        "python":     {"language": Language(tree_sitter_python.language()),   "symbol_kinds": python_kinds},
        "typescript": {"language": Language(tst.language_typescript()),       "symbol_kinds": typescript_kinds},
        "javascript": {"language": Language(tst.language_tsx()),              "symbol_kinds": typescript_kinds},
    }


def _get_registry() -> dict[str, dict[str, Any]]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def supported_languages() -> list[str]:
    """Public: list of languages currently wired up. Empty if tree-sitter missing."""
    return list(_get_registry().keys())


# ──────────────────────────────────────────────────────────────────────────────
# Chunk extraction
# ──────────────────────────────────────────────────────────────────────────────

def extract_chunks(path: str, content: str, language: str) -> list[dict]:
    """Produce tree-sitter-derived chunks for one source file.

    Returns a list of chunk dicts. The first is always a file-level chunk;
    subsequent entries are symbol-level (classes, methods, functions, etc.).

    Schema:
      - path:         forwarded from input
      - class_name:   legacy field — populated for class/interface/enum chunks and
                      set to the enclosing class for method chunks (compat with code_ingestion)
      - method_name:  legacy field — populated for method/function/constructor chunks
      - content:      exact source bytes for the node (or full file for file chunk)
      - chunk_index:  0-based within this file
      - symbol_kind:  "file" | "class" | "interface" | "enum" | "record" |
                      "method" | "constructor" | "function"
      - symbol_name:  identifier text (or filename for file chunk)
      - signature:    first line of the node (approximation)
      - line_start:   1-indexed
      - line_end:     1-indexed
      - language:     forwarded from input

    Returns [] when language is unsupported OR tree-sitter packages are absent.
    """
    registry = _get_registry()
    if language not in registry:
        return []

    try:
        from tree_sitter import Parser
    except ImportError:
        return []

    spec = registry[language]
    content_bytes = content.encode("utf-8")

    parser = Parser(spec["language"])
    tree = parser.parse(content_bytes)
    root = tree.root_node

    chunks: list[dict] = []

    # Chunk 0: the full file.
    filename = path.rsplit("/", 1)[-1]
    chunks.append({
        "path":         path,
        "class_name":   None,
        "method_name":  None,
        "content":      content,
        "chunk_index":  0,
        "symbol_kind":  "file",
        "symbol_name":  filename,
        "signature":    None,
        "line_start":   1,
        "line_end":     content.count("\n") + 1,
        "language":     language,
    })

    # Chunks 1..n: symbols found via in-order traversal.
    symbol_kinds = spec["symbol_kinds"]
    noise_filter_on = _noise_filter_enabled()
    skipped_noise = 0
    idx = 1
    for node in _walk(root):
        if node.type not in symbol_kinds:
            continue
        symbol_kind = symbol_kinds[node.type]
        name = _extract_name(node, content_bytes)
        if name == "<unknown>":
            # Skip anonymous / unnamed nodes — not useful for retrieval
            continue
        candidate = {
            "path":         path,
            "class_name":   _class_name_for(node, content_bytes, symbol_kind, name),
            "method_name":  name if symbol_kind in ("method", "function", "constructor") else None,
            "content":      content_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace"),
            "chunk_index":  idx,
            "symbol_kind":  symbol_kind,
            "symbol_name":  name,
            "signature":    _extract_signature(node, content_bytes),
            "line_start":   node.start_point[0] + 1,
            "line_end":     node.end_point[0] + 1,
            "language":     language,
        }

        # Phase 3.3 — skip one-line getters / setters / trivial delegates.
        # Only applied to callable symbols; classes/interfaces stay regardless
        # of size. The file-level chunk preserves locatability for skipped
        # symbols so BM25 / grep-style queries still find them.
        if (
            noise_filter_on
            and symbol_kind in ("method", "function", "constructor")
            and is_trivial_symbol(candidate)
        ):
            skipped_noise += 1
            continue

        chunks.append(candidate)
        idx += 1

    if skipped_noise:
        logger.debug(
            "Noise filter: skipped %d trivial symbol(s) in %s (file-level chunk preserves locatability)",
            skipped_noise, path,
        )
    return chunks


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _walk(node):
    """Pre-order traversal yielding every descendant."""
    yield node
    for child in node.children:
        yield from _walk(child)


def _extract_name(node, content_bytes: bytes) -> str:
    """Return the identifier of this node via tree-sitter's named `name` field.

    Falls back to scanning immediate children for any `identifier` /
    `type_identifier` / `property_identifier` when the named field is absent
    (e.g. Python `decorated_definition` wraps the real def).
    """
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return content_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")

    # Fallback: look one level down for a wrapped definition (Python decorators)
    for child in node.children:
        if child.type in ("function_definition", "class_definition", "method_definition"):
            inner = child.child_by_field_name("name")
            if inner is not None:
                return content_bytes[inner.start_byte:inner.end_byte].decode("utf-8", errors="replace")

    return "<unknown>"


def _extract_signature(node, content_bytes: bytes) -> str:
    """First line of the node's source text (approximation of the declaration).

    For `public boolean acquire() { ... }` this returns `public boolean acquire() {`.
    Good enough for retrieval + display; exact signature parsing is per-language
    and not needed at this slice.
    """
    first_newline = content_bytes.find(b"\n", node.start_byte, node.end_byte)
    end = first_newline if first_newline != -1 else node.end_byte
    return content_bytes[node.start_byte:end].decode("utf-8", errors="replace").strip()


def _class_name_for(node, content_bytes: bytes, symbol_kind: str, name: str) -> str | None:
    """Legacy `class_name` field used by code_ingestion's metadata.

    - class / interface / enum / record: the node's own name.
    - method / function / constructor: the nearest enclosing class-ish ancestor.
    """
    if symbol_kind in ("class", "interface", "enum", "record"):
        return name
    parent = node.parent
    while parent is not None:
        if parent.type in (
            "class_declaration", "interface_declaration", "enum_declaration",
            "record_declaration", "class_definition", "abstract_class_declaration",
        ):
            return _extract_name(parent, content_bytes)
        parent = parent.parent
    return None
