# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Python symbol-graph extractor (Slice 22a).

Mirrors `symbol_graph_extractor_java.py` but for Python source files.
Captures:

  - file-level `imports` list (one entry per imported module/symbol):
      `import x`              → "x"
      `import x.y`            → "x.y"
      `import x as y`         → "x"          (alias dropped)
      `from a.b import c`     → "a.b.c"      (module-qualified)
      `from a.b import c as d` → "a.b.c"
      `from . import c`       → ".c"
      `from .a import b`      → ".a.b"

  - per-class `inherits`: first base class name (positional, non-keyword)
    or None. Subclassing patterns like `class C(A, B, metaclass=M)` capture
    "A"; `B` ends up in `implements`. This is a heuristic — Python doesn't
    distinguish "extends" from "implements" syntactically, but the most
    common convention is single inheritance for behaviour + ABC mixins for
    contract.

  - per-class `implements`: remaining positional bases (after the first)
    plus any class-level keyword arg target (e.g. `metaclass=`). Best-
    effort; consumers should treat as informational.

  - per-method `calls`: bare callable names invoked inside the body
    (`foo(...)`, `self.bar(...)`, `obj.baz(...)`). Module-qualified calls
    capture the rightmost name (so `a.b.c()` → "c"). Deduplicated,
    preserves first-appearance order.

  - per-method `called_by`: filled by a within-file reverse pass. Cross-
    file resolution deferred to Slice 23 (Python LSP).

Decorators, async functions, `@property`, dataclasses, and nested
classes are all handled by the standard `function_definition` /
`class_definition` node types — the tree-sitter Python grammar exposes
them uniformly.

Pure — no I/O, no DB. Lazy-imports tree-sitter so the module remains
importable in environments without the grammar package.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Data shapes — parallel to Java's for downstream code_ingestion uniformity.
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PyMethod:
    name: str
    calls:     list[str] = field(default_factory=list)
    called_by: list[str] = field(default_factory=list)


@dataclass
class PyClass:
    name: str
    kind: str = "class"           # always "class" for Python — kept for shape parity
    inherits:   str | None = field(default=None)
    implements: list[str] = field(default_factory=list)
    methods:    list[PyMethod] = field(default_factory=list)


@dataclass
class PySymbolGraph:
    imports:           list[str]      = field(default_factory=list)
    classes:           list[PyClass]  = field(default_factory=list)
    module_functions:  list[PyMethod] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Parser access (lazy)
# ──────────────────────────────────────────────────────────────────────────────

_PARSER = None


def _get_parser():
    """Lazy-init tree-sitter Python parser. Returns None if grammar unavailable."""
    global _PARSER
    if _PARSER is not None:
        return _PARSER
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_python
        _PARSER = Parser(Language(tree_sitter_python.language()))
    except Exception as e:
        logger.warning("symbol_graph_extractor_python: tree-sitter unavailable: %s", e)
        _PARSER = False
    return _PARSER if _PARSER is not False else None


def _reset_parser_for_tests():
    """Test hook — clear the cached parser."""
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

def _dotted_name(node, src: bytes) -> str:
    """Render a dotted_name / identifier / aliased_import node as 'a.b.c'."""
    # `aliased_import` has fields name + alias; we only want name.
    if node.type == "aliased_import":
        target = node.child_by_field_name("name")
        return _text(target, src) if target is not None else ""
    return _text(node, src)


def _extract_imports(root, src: bytes) -> list[str]:
    """Order-preserving dedup'd list of imports.

    `import x.y, z` and `from a import b, c, d` each yield multiple entries.
    Aliases are dropped (they're local naming, not graph identity).
    Relative imports (`from . import x`) are kept with leading dots.
    """
    seen: set[str] = set()
    out: list[str] = []

    for node in _walk(root):
        # `import x.y, z`
        if node.type == "import_statement":
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import"):
                    name = _dotted_name(child, src)
                    if name and name not in seen:
                        seen.add(name)
                        out.append(name)

        # `from a.b import c, d`  /  `from . import x`
        elif node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            module = _text(module_node, src) if module_node is not None else ""

            # The `relative_import` node represents leading dots (e.g. `.` or `..`).
            # When module_name is absent (pure `from . import x`), we still want to
            # capture the dot prefix — find it among the children.
            relative_prefix = ""
            for child in node.children:
                if child.type == "relative_import":
                    relative_prefix = _text(child, src)
                    break
            if relative_prefix and not module.startswith("."):
                module = relative_prefix + module

            # Imported names follow `import` keyword. Field name varies across
            # grammar versions — walk children and pick out dotted_name /
            # identifier / aliased_import after the `import` token.
            past_import = False
            for child in node.children:
                if child.type == "import":
                    past_import = True
                    continue
                if not past_import:
                    continue
                if child.type in ("dotted_name", "aliased_import", "identifier"):
                    sym = _dotted_name(child, src)
                    if not sym:
                        continue
                    full = f"{module}.{sym}" if module else sym
                    if full not in seen:
                        seen.add(full)
                        out.append(full)

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Class / function extraction
# ──────────────────────────────────────────────────────────────────────────────

def _extract_class_bases(class_node, src: bytes) -> tuple[str | None, list[str]]:
    """Inspect the `argument_list` after the class name and split bases:
       (first_positional_base, [remaining_positional + keyword_targets])."""
    args = class_node.child_by_field_name("superclasses")
    if args is None:
        return None, []

    positional: list[str] = []
    keyword:    list[str] = []

    for child in args.children:
        if child.type == "identifier":
            positional.append(_text(child, src))
        elif child.type == "attribute":
            # e.g. `abc.ABC` → keep rightmost component
            ident_text = _text(child, src)
            positional.append(ident_text.split(".")[-1])
        elif child.type == "keyword_argument":
            # e.g. `metaclass=Meta`
            value = child.child_by_field_name("value")
            if value is None:
                continue
            v_text = _text(value, src)
            keyword.append(v_text.split(".")[-1])

    inherits = positional[0] if positional else None
    implements = positional[1:] + keyword
    # Dedup implements while preserving order
    seen: set[str] = set()
    impl_dedup: list[str] = []
    for n in implements:
        if n and n not in seen:
            seen.add(n)
            impl_dedup.append(n)
    return inherits, impl_dedup


def _call_target_name(call_node, src: bytes) -> str | None:
    """Extract the callable's rightmost name from a `call` node.

    `foo(...)`        → "foo"
    `self.bar(...)`   → "bar"
    `a.b.c(...)`      → "c"
    `obj[i](...)`     → None       (subscript-based call — skip)
    `(lambda: 1)()`   → None
    """
    func = call_node.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "identifier":
        return _text(func, src)
    if func.type == "attribute":
        # Last child of attribute is the rightmost identifier.
        attr = func.child_by_field_name("attribute")
        if attr is not None:
            return _text(attr, src)
    return None


def _extract_function_calls(func_node, src: bytes) -> list[str]:
    """Bare-name list of every callable invoked inside the function body.
    Deduplicated, first-appearance order."""
    seen: set[str] = set()
    out: list[str] = []
    body = func_node.child_by_field_name("body")
    if body is None:
        return out
    for descendant in _walk(body):
        if descendant.type == "call":
            name = _call_target_name(descendant, src)
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _extract_methods_in_block(block, src: bytes) -> list[PyMethod]:
    """Walk one indentation block + collect direct function_definition children
    (handles `def`/`async def`, decorated-or-not). Nested functions inside a
    method body are NOT promoted to top-level methods."""
    methods: list[PyMethod] = []
    if block is None:
        return methods
    for child in block.children:
        target = None
        if child.type == "function_definition":
            target = child
        elif child.type == "decorated_definition":
            # Decorated function/method: the actual function_definition is a
            # child of the decorated_definition.
            for sub in child.children:
                if sub.type == "function_definition":
                    target = sub
                    break
        if target is None:
            continue
        name = _name_field(target, src)
        if not name:
            continue
        calls = _extract_function_calls(target, src)
        methods.append(PyMethod(name=name, calls=calls))
    return methods


def _extract_classes_and_functions(
    root, src: bytes,
) -> tuple[list[PyClass], list[PyMethod]]:
    """Walk module-level + collect classes (with their methods) and module-
    level functions. Decorated definitions are unwrapped."""
    classes:           list[PyClass]  = []
    module_functions:  list[PyMethod] = []

    # Module-level children + classes-inside-classes are picked up via _walk
    # but module_functions only includes top-level (module body) functions to
    # avoid promoting nested helpers.
    module_block = root  # for Python the root IS the module — children are stmts

    def _module_top_level_children():
        for child in module_block.children:
            yield child

    # First pass: classes (anywhere in the tree — supports nested classes too).
    for node in _walk(root):
        if node.type != "class_definition":
            continue
        cname = _name_field(node, src)
        if not cname:
            continue
        body = node.child_by_field_name("body")
        methods = _extract_methods_in_block(body, src)
        inherits, implements = _extract_class_bases(node, src)
        classes.append(PyClass(
            name=cname, kind="class",
            inherits=inherits, implements=implements,
            methods=methods,
        ))

    # Second pass: top-level (module-level) functions only.
    for child in _module_top_level_children():
        target = None
        if child.type == "function_definition":
            target = child
        elif child.type == "decorated_definition":
            for sub in child.children:
                if sub.type == "function_definition":
                    target = sub
                    break
        if target is None:
            continue
        name = _name_field(target, src)
        if not name:
            continue
        module_functions.append(PyMethod(
            name=name, calls=_extract_function_calls(target, src),
        ))

    return classes, module_functions


# ──────────────────────────────────────────────────────────────────────────────
# Within-file called_by reverse pass
# ──────────────────────────────────────────────────────────────────────────────

def _fill_called_by_within_file(
    classes: list[PyClass],
    module_functions: list[PyMethod],
) -> None:
    """Populate `called_by` for every method + module function based on the
    `calls` lists of every other in the file. Cross-file resolution is
    Slice 23's job."""
    # all callable names in the file
    all_names: set[str] = set()
    for c in classes:
        for m in c.methods:
            all_names.add(m.name)
    for f in module_functions:
        all_names.add(f.name)

    # callee_name → list of caller_names (preserving discovery order)
    caller_map: dict[str, list[str]] = {}

    def _record_callers(callers_iter):
        for caller_name, callees in callers_iter:
            for callee in callees:
                if callee in all_names:
                    caller_map.setdefault(callee, []).append(caller_name)

    _record_callers(
        (m.name, m.calls) for c in classes for m in c.methods
    )
    _record_callers(
        (f.name, f.calls) for f in module_functions
    )

    def _apply(target_list):
        for t in target_list:
            seen: set[str] = set()
            ordered: list[str] = []
            for caller in caller_map.get(t.name, []):
                if caller not in seen:
                    seen.add(caller)
                    ordered.append(caller)
            t.called_by = ordered

    for c in classes:
        _apply(c.methods)
    _apply(module_functions)


# ──────────────────────────────────────────────────────────────────────────────
# Public entry
# ──────────────────────────────────────────────────────────────────────────────

def extract(content: str) -> PySymbolGraph:
    """Parse Python `content` and extract structural symbol relationships.

    Returns an empty `PySymbolGraph` when the tree-sitter grammar is
    unavailable or input is empty. Never raises.
    """
    if not content or not content.strip():
        return PySymbolGraph()

    parser = _get_parser()
    if parser is None:
        return PySymbolGraph()

    src = content.encode("utf-8")
    tree = parser.parse(src)
    root = tree.root_node

    classes, module_functions = _extract_classes_and_functions(root, src)
    _fill_called_by_within_file(classes, module_functions)

    return PySymbolGraph(
        imports=_extract_imports(root, src),
        classes=classes,
        module_functions=module_functions,
    )


def to_dict(graph: PySymbolGraph) -> dict[str, Any]:
    """Convenience: dump PySymbolGraph to a plain JSON-safe dict."""
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
        "module_functions": [
            {"name": f.name, "calls": list(f.calls), "called_by": list(f.called_by)}
            for f in graph.module_functions
        ],
    }
