# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""TypeScript / JavaScript symbol-graph extractor (Slice 22b).

Mirrors `symbol_graph_extractor_java.py` and `symbol_graph_extractor_python.py`
for the TypeScript grammar. The same parser handles plain JavaScript (the
TypeScript grammar is a superset — JS files parse cleanly with a couple of
shape differences this module accommodates).

Captures:

  - file-level `imports` list (one entry per imported module/symbol):
      `import x from 'mod'`           → "mod.x"
      `import { a, b } from 'mod'`    → "mod.a", "mod.b"
      `import * as ns from 'mod'`     → "mod.*"
      `import 'mod'`                  → "mod"  (side-effect import)
      `const x = require('mod')`      → "mod"  (CommonJS, best-effort)
      `import { a as b } from 'mod'`  → "mod.a"  (alias dropped)

  - per-class `inherits`: the `extends` target name (single — JS/TS classes
    are single-inheritance), or None.

  - per-class `implements`: list from the `implements A, B, C` clause (TS
    only — JS has no implements; will always be empty for .js).

  - per-method `calls`: bare callable names invoked inside the body
    (`foo()`, `this.bar()`, `obj.baz()`). Module-qualified calls keep the
    rightmost name. Deduped, first-appearance order.

  - per-method `called_by`: filled by within-file reverse pass. Cross-file
    resolution deferred to Slice 24 (TS LSP).

The extractor recognises:
  - `class_declaration` (both regular and abstract classes)
  - `method_definition` for class methods (incl. async/static/getter/setter)
  - `function_declaration` for module-level functions (incl. async)
  - `arrow_function` and `function_expression` are NOT promoted to module-
    level functions even if assigned to a top-level const — keeps the slice
    scope tight; consumers wanting that should land it as a follow-up.

Pure — no I/O, no DB. Lazy-imports tree-sitter so the module remains
importable in environments without the grammar package.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Data shapes — parallel to other extractors for ingestion uniformity.
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TsMethod:
    name: str
    calls:     list[str] = field(default_factory=list)
    called_by: list[str] = field(default_factory=list)


@dataclass
class TsClass:
    name: str
    kind: str = "class"     # always "class" for now; "interface" handled separately
    inherits:   str | None = field(default=None)
    implements: list[str] = field(default_factory=list)
    methods:    list[TsMethod] = field(default_factory=list)


@dataclass
class TsSymbolGraph:
    imports:           list[str]    = field(default_factory=list)
    classes:           list[TsClass] = field(default_factory=list)
    module_functions:  list[TsMethod] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Parser access (lazy)
# ──────────────────────────────────────────────────────────────────────────────

_PARSER_TS = None
_PARSER_TSX = None


def _get_parser(is_tsx: bool = False):
    """Lazy-init tree-sitter TS parser. Returns None if grammar unavailable.

    `tree-sitter-typescript` ships TWO grammars: `typescript` (for .ts) and
    `tsx` (for .tsx + handles JSX in .js). Plain .js parses fine with the
    `typescript` grammar.
    """
    global _PARSER_TS, _PARSER_TSX
    cache = _PARSER_TSX if is_tsx else _PARSER_TS
    if cache is not None:
        return cache if cache is not False else None
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_typescript
        if is_tsx:
            lang = Language(tree_sitter_typescript.language_tsx())
            _PARSER_TSX = Parser(lang)
        else:
            lang = Language(tree_sitter_typescript.language_typescript())
            _PARSER_TS = Parser(lang)
    except Exception as e:
        logger.warning("symbol_graph_extractor_typescript: tree-sitter unavailable: %s", e)
        if is_tsx:
            _PARSER_TSX = False
        else:
            _PARSER_TS = False
    cache = _PARSER_TSX if is_tsx else _PARSER_TS
    return cache if cache is not False else None


def _reset_parser_for_tests():
    global _PARSER_TS, _PARSER_TSX
    _PARSER_TS = None
    _PARSER_TSX = None


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


def _strip_string_quotes(s: str) -> str:
    if not s:
        return s
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')) \
       or (s.startswith("`") and s.endswith("`")):
        return s[1:-1]
    return s


# ──────────────────────────────────────────────────────────────────────────────
# Import extraction
# ──────────────────────────────────────────────────────────────────────────────

def _import_specifiers(import_clause, src: bytes) -> list[str]:
    """Extract the imported symbol names from an `import_clause` subtree.

    Handles:
      - default import:        `import X from 'mod'`        → ["X"]
      - named imports:         `import { a, b } from 'mod'` → ["a", "b"]
      - namespace import:      `import * as ns from 'mod'`  → ["*"]
      - mixed:                 `import X, { a, b } from m`  → ["X", "a", "b"]
      - aliases dropped:       `import { a as b } from m`   → ["a"]
    """
    out: list[str] = []
    for node in _walk(import_clause):
        if node.type == "identifier":
            # Default-import binding (lives directly under import_clause, not
            # inside a named_imports list).
            parent = node.parent
            if parent is None:
                continue
            if parent.type == "import_clause":
                out.append(_text(node, src))
        elif node.type == "namespace_import":
            out.append("*")
        elif node.type == "import_specifier":
            # `name` field is the source-side name (the imported symbol).
            n = node.child_by_field_name("name")
            if n is not None:
                out.append(_text(n, src))
    # Dedup preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def _extract_imports(root, src: bytes) -> list[str]:
    """Order-preserving dedup'd list of imports.

    `import X from 'mod'`              → "mod.X"
    `import { a } from 'mod'`          → "mod.a"
    `import * as ns from 'mod'`        → "mod.*"
    `import 'mod'`                     → "mod"
    `const x = require('mod')`         → "mod"  (best-effort CommonJS)
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(s: str) -> None:
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    for node in _walk(root):
        if node.type == "import_statement":
            # The module path is in field "source" as a string literal.
            source_node = node.child_by_field_name("source")
            module = ""
            if source_node is not None:
                module = _strip_string_quotes(_text(source_node, src))

            # Find the import_clause child to enumerate specifiers.
            specifiers: list[str] = []
            for child in node.children:
                if child.type == "import_clause":
                    specifiers = _import_specifiers(child, src)
                    break

            if not specifiers:
                # Side-effect import: `import 'mod'`
                _add(module)
            else:
                for s in specifiers:
                    _add(f"{module}.{s}" if module else s)

        elif node.type == "call_expression":
            # `require('mod')` — best-effort.
            fn = node.child_by_field_name("function")
            if fn is not None and _text(fn, src) == "require":
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for arg in args.children:
                        if arg.type == "string":
                            _add(_strip_string_quotes(_text(arg, src)))
                            break

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Class / function extraction
# ──────────────────────────────────────────────────────────────────────────────

def _extract_class_heritage(class_node, src: bytes) -> tuple[str | None, list[str]]:
    """Extract `extends X` (single) and `implements A, B` from a class header.

    Tree-sitter shape: `class_heritage` child contains:
        - `extends_clause` with a single type expression
        - `implements_clause` with one or more type identifiers
    Plain JS classes only have extends; .ts adds implements.
    """
    inherits: str | None = None
    implements: list[str] = []

    def _name_from_heritage_target(node) -> str | None:
        """Resolve an extends/implements target node to a rightmost name.

        - identifier / type_identifier      → the text
        - member_expression (`A.B`)         → rightmost property
        - generic_type (`Foo<T>`)           → recurse into the name child
        - type_arguments (`<T>`) skipped    → not a target, just generics
        """
        if node is None:
            return None
        if node.type in ("identifier", "type_identifier"):
            return _text(node, src)
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            if prop is not None:
                return _text(prop, src)
        if node.type == "generic_type":
            # tree-sitter-typescript's `generic_type` has the type-name first.
            for child in node.children:
                resolved = _name_from_heritage_target(child)
                if resolved:
                    return resolved
        return None

    for child in class_node.children:
        if child.type != "class_heritage":
            continue
        for sub in child.children:
            if sub.type == "extends_clause":
                # Walk DIRECT children only — type_arguments would contain
                # the generic parameter names which are not the parent class.
                for desc in sub.children:
                    name = _name_from_heritage_target(desc)
                    if name:
                        inherits = name
                        break
            elif sub.type == "implements_clause":
                # Direct children only — each is one interface target.
                # Skips type_arguments inside generic_type (same logic as extends).
                for desc in sub.children:
                    name = _name_from_heritage_target(desc)
                    if name and name not in implements:
                        implements.append(name)

    return inherits, implements


def _call_target_name(call_node, src: bytes) -> str | None:
    """Extract the callable's rightmost name from a `call_expression` node.

    `foo()`             → "foo"
    `this.bar()`        → "bar"
    `obj.method()`      → "method"
    `a.b.c()`           → "c"
    `(x) => x`()        → None  (arrow IIFE — skip)
    `obj[i]()`          → None  (subscript — skip)
    """
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        return _text(fn, src)
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        if prop is not None and prop.type == "property_identifier":
            return _text(prop, src)
    return None


def _extract_function_calls(func_node, src: bytes) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    body = func_node.child_by_field_name("body")
    if body is None:
        return out
    for descendant in _walk(body):
        if descendant.type == "call_expression":
            name = _call_target_name(descendant, src)
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _method_name(method_node, src: bytes) -> str:
    """Extract method name from a method_definition. The grammar puts it
    in field `name` with type `property_identifier`, but accessors have
    a `kind` modifier (get/set) — we want just the property name."""
    n = method_node.child_by_field_name("name")
    if n is None:
        return ""
    return _text(n, src)


def _extract_methods_in_body(class_body, src: bytes) -> list[TsMethod]:
    """Walk class body + collect `method_definition` children.

    Properties initialised to arrow functions (`x = () => {}`) are NOT
    captured this slice — they're public_field_definition nodes with a
    nested arrow_function. Ignored to keep the scope tight.
    """
    methods: list[TsMethod] = []
    if class_body is None:
        return methods
    for child in class_body.children:
        if child.type != "method_definition":
            continue
        name = _method_name(child, src)
        if not name:
            continue
        calls = _extract_function_calls(child, src)
        methods.append(TsMethod(name=name, calls=calls))
    return methods


def _extract_classes_and_functions(
    root, src: bytes,
) -> tuple[list[TsClass], list[TsMethod]]:
    classes:           list[TsClass]  = []
    module_functions:  list[TsMethod] = []

    # Pass 1: classes (anywhere — supports nested classes via _walk).
    for node in _walk(root):
        if node.type not in ("class_declaration", "abstract_class_declaration"):
            continue
        cname = _name_field(node, src)
        if not cname:
            continue
        body = node.child_by_field_name("body")
        methods = _extract_methods_in_body(body, src)
        inherits, implements = _extract_class_heritage(node, src)
        classes.append(TsClass(
            name=cname, kind="class",
            inherits=inherits, implements=implements,
            methods=methods,
        ))

    # Pass 2: top-level (module-level) function declarations only.
    # A `function_declaration` directly under the program root is module-level.
    for child in root.children:
        target = None
        if child.type in ("function_declaration",):
            target = child
        # Sometimes a function is wrapped in an `export_statement`. Unwrap.
        elif child.type == "export_statement":
            for sub in child.children:
                if sub.type == "function_declaration":
                    target = sub
                    break
        if target is None:
            continue
        name = _name_field(target, src)
        if not name:
            continue
        module_functions.append(TsMethod(
            name=name, calls=_extract_function_calls(target, src),
        ))

    return classes, module_functions


# ──────────────────────────────────────────────────────────────────────────────
# Within-file called_by reverse pass
# ──────────────────────────────────────────────────────────────────────────────

def _fill_called_by_within_file(
    classes: list[TsClass],
    module_functions: list[TsMethod],
) -> None:
    all_names: set[str] = set()
    for c in classes:
        for m in c.methods:
            all_names.add(m.name)
    for f in module_functions:
        all_names.add(f.name)

    caller_map: dict[str, list[str]] = {}

    for c in classes:
        for m in c.methods:
            for callee in m.calls:
                if callee in all_names:
                    caller_map.setdefault(callee, []).append(m.name)
    for f in module_functions:
        for callee in f.calls:
            if callee in all_names:
                caller_map.setdefault(callee, []).append(f.name)

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

def extract(content: str, *, is_tsx: bool = False) -> TsSymbolGraph:
    """Parse TS/JS `content` and extract structural symbol relationships.

    Set `is_tsx=True` for `.tsx` files (and `.jsx` if you want JSX support
    on plain JS). Returns an empty graph when grammar is unavailable or
    input is empty. Never raises.
    """
    if not content or not content.strip():
        return TsSymbolGraph()

    parser = _get_parser(is_tsx=is_tsx)
    if parser is None:
        return TsSymbolGraph()

    src = content.encode("utf-8")
    tree = parser.parse(src)
    root = tree.root_node

    classes, module_functions = _extract_classes_and_functions(root, src)
    _fill_called_by_within_file(classes, module_functions)

    return TsSymbolGraph(
        imports=_extract_imports(root, src),
        classes=classes,
        module_functions=module_functions,
    )


def to_dict(graph: TsSymbolGraph) -> dict[str, Any]:
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
