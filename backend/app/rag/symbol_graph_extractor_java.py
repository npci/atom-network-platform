# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Java symbol-graph extractor (Slice 17).

Parses a Java source file using the tree-sitter Java grammar (already
installed in Slice 3) and returns a structured view of:

  - file-level `imports` list
  - per-class `inherits` (single superclass name or None)
  - per-class `implements` list
  - per-method `calls` list (bare method names invoked inside the body)
  - per-method `called_by` list (WITHIN THE SAME FILE only — cross-file
    resolution requires a global symbol index; follow-up slice)

Not a true LSP. The names captured are unqualified (no package prefixes,
no type resolution). That's enough for Slice 17's scope — building the
symbol graph for RAG retrieval boosting. A future slice can swap in real
LSP-powered resolution if measurement demands it.

Pure — no I/O, no DB. Lazy-imports the tree-sitter Java grammar so this
module stays importable in environments without the package.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Data shapes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class JavaMethod:
    name: str
    calls:     list[str] = field(default_factory=list)   # unique, in-order of first appearance
    called_by: list[str] = field(default_factory=list)   # filled in by a second pass


@dataclass
class JavaClass:
    name: str
    kind: str                                            # "class" | "interface" | "enum" | "record"
    inherits: str | None = None
    implements: list[str] = field(default_factory=list)
    methods: list[JavaMethod] = field(default_factory=list)


@dataclass
class JavaSymbolGraph:
    imports: list[str] = field(default_factory=list)
    classes: list[JavaClass] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Parser access
# ──────────────────────────────────────────────────────────────────────────────

_PARSER = None


def _get_parser():
    """Lazy-init tree-sitter Java parser. Returns None if grammar unavailable."""
    global _PARSER
    if _PARSER is not None:
        return _PARSER
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_java
        _PARSER = Parser(Language(tree_sitter_java.language()))
    except Exception as e:
        logger.warning("symbol_graph_extractor_java: tree-sitter unavailable: %s", e)
        _PARSER = False  # sentinel so we don't retry
    return _PARSER if _PARSER is not False else None


def _reset_parser_for_tests():
    """Test hook — clear the cached parser (fresh lazy init on next call)."""
    global _PARSER
    _PARSER = None


# ──────────────────────────────────────────────────────────────────────────────
# AST helpers
# ──────────────────────────────────────────────────────────────────────────────

def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _name_field(node, src: bytes) -> str:
    n = node.child_by_field_name("name")
    return _text(n, src) if n is not None else ""


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


# ──────────────────────────────────────────────────────────────────────────────
# Import extraction
# ──────────────────────────────────────────────────────────────────────────────

def _extract_imports(root, src: bytes) -> list[str]:
    """Return a deduplicated, order-preserving list of imported names."""
    seen: set[str] = set()
    out: list[str] = []
    for node in root.children:
        if node.type != "import_declaration":
            continue
        # The body after `import` is either scoped_identifier or an asterisk import.
        # Fetch the full content and strip syntax. Easiest: walk to find the first
        # scoped_identifier or identifier child.
        target: str | None = None
        for child in node.children:
            if child.type in ("scoped_identifier", "identifier"):
                target = _text(child, src)
                break
        # Handle `import static pkg.Cls.method`
        if target is None:
            for child in node.children:
                if child.type == "scoped_identifier":
                    target = _text(child, src)
                    break
        if target and target not in seen:
            seen.add(target)
            out.append(target)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Class extraction
# ──────────────────────────────────────────────────────────────────────────────

_CLASS_LIKE_TYPES = {
    "class_declaration":     "class",
    "interface_declaration": "interface",
    "enum_declaration":      "enum",
    "record_declaration":    "record",
}


def _extract_superclass(class_node, src: bytes) -> str | None:
    """Return the name after `extends` for a class, or None."""
    sc = class_node.child_by_field_name("superclass")
    if sc is None:
        return None
    # `superclass` node has children: `extends` keyword + the type_identifier.
    for child in sc.children:
        if child.type in ("type_identifier", "generic_type", "scoped_type_identifier"):
            return _text(child, src)
    return None


def _extract_implements(class_node, src: bytes) -> list[str]:
    """Return list of interface names from an `implements A, B, C` clause."""
    si = class_node.child_by_field_name("interfaces")
    if si is None:
        return []
    names: list[str] = []
    for grandchild in _walk(si):
        if grandchild.type in ("type_identifier", "scoped_type_identifier"):
            n = _text(grandchild, src)
            # Avoid duplicates from nested type_identifier inside scoped_type_identifier.
            if n and n not in names:
                names.append(n)
    return names


def _extract_method_calls(method_node, src: bytes) -> list[str]:
    """Return a deduplicated, order-preserving list of method names invoked
    inside the method's body."""
    seen: set[str] = set()
    out: list[str] = []
    body = method_node.child_by_field_name("body")
    if body is None:
        return out
    for descendant in _walk(body):
        if descendant.type == "method_invocation":
            name_node = descendant.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node, src)
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _extract_methods(class_body, src: bytes) -> list[JavaMethod]:
    methods: list[JavaMethod] = []
    for child in class_body.children:
        if child.type != "method_declaration":
            continue
        mname = _name_field(child, src)
        if not mname:
            continue
        calls = _extract_method_calls(child, src)
        methods.append(JavaMethod(name=mname, calls=calls))
    return methods


def _extract_classes(root, src: bytes) -> list[JavaClass]:
    classes: list[JavaClass] = []
    for node in _walk(root):
        if node.type not in _CLASS_LIKE_TYPES:
            continue
        kind = _CLASS_LIKE_TYPES[node.type]
        cname = _name_field(node, src)
        if not cname:
            continue
        body = node.child_by_field_name("body")
        methods = _extract_methods(body, src) if body is not None else []
        classes.append(JavaClass(
            name=cname,
            kind=kind,
            inherits=_extract_superclass(node, src) if kind == "class" else None,
            implements=_extract_implements(node, src),
            methods=methods,
        ))
    return classes


# ──────────────────────────────────────────────────────────────────────────────
# Within-file called_by reverse pass
# ──────────────────────────────────────────────────────────────────────────────

def _fill_called_by_within_file(classes: list[JavaClass]) -> None:
    """For each method M, populate `M.called_by` with the names of methods in
    the SAME FILE that invoke M. Cross-file resolution is deferred."""
    # method name -> set of caller method names
    caller_map: dict[str, list[str]] = {}
    all_method_names: set[str] = set()

    for cls in classes:
        for m in cls.methods:
            all_method_names.add(m.name)

    for cls in classes:
        for m in cls.methods:
            for callee in m.calls:
                if callee in all_method_names:
                    caller_map.setdefault(callee, []).append(m.name)

    for cls in classes:
        for m in cls.methods:
            # Dedup while preserving first-appearance order.
            seen: set[str] = set()
            ordered: list[str] = []
            for caller in caller_map.get(m.name, []):
                if caller not in seen:
                    seen.add(caller)
                    ordered.append(caller)
            m.called_by = ordered


# ──────────────────────────────────────────────────────────────────────────────
# Public entry
# ──────────────────────────────────────────────────────────────────────────────

def extract(content: str) -> JavaSymbolGraph:
    """Parse Java `content` and extract structural symbol relationships.

    Returns an empty `JavaSymbolGraph` when the tree-sitter grammar is
    unavailable or input is empty. Never raises.
    """
    if not content or not content.strip():
        return JavaSymbolGraph()

    parser = _get_parser()
    if parser is None:
        return JavaSymbolGraph()

    src = content.encode("utf-8")
    tree = parser.parse(src)
    root = tree.root_node

    classes = _extract_classes(root, src)
    _fill_called_by_within_file(classes)

    return JavaSymbolGraph(
        imports=_extract_imports(root, src),
        classes=classes,
    )


def to_dict(graph: JavaSymbolGraph) -> dict[str, Any]:
    """Convenience: dump JavaSymbolGraph to a plain JSON-safe dict."""
    return {
        "imports": list(graph.imports),
        "classes": [
            {
                "name":       c.name,
                "kind":       c.kind,
                "inherits":   c.inherits,
                "implements": list(c.implements),
                "methods": [
                    {"name": m.name, "calls": list(m.calls), "called_by": list(m.called_by)}
                    for m in c.methods
                ],
            }
            for c in graph.classes
        ],
    }
