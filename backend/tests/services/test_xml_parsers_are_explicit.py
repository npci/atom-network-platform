# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Every lxml parse call must pass an explicit parser.

WHY THIS EXISTS, stated precisely so nobody over-reads it: an external SAST
report flagged `app/rag/chunking.py` for XXE because it called
`etree.parse(path)` with no parser. Measured against the pinned lxml (6.1.0)
and the previous pin (5.3.0), that call was NOT exploitable — libxml2 refuses
external SYSTEM entities and caps entity amplification, so neither file
disclosure nor a billion-laughs bomb gets through.

So this is a CONSISTENCY and FUTURE-PROOFING gate, not a vulnerability gate:

  * four of the five lxml call sites already passed a hardened parser, and the
    fifth was simply missed — the convention existed, it just was not enforced;
  * `resolve_entities` / `no_network` are the parser's documented safety knobs,
    and relying on a library default for a safety property means a future
    default change is a silent regression;
  * the input at several of these sites is an uploaded document.

Scope note: this checks LXML only. Stdlib `xml.etree.ElementTree` and
`xmlschema` (both used in `services/xsd_validation.py`) are deliberately out of
scope — they were measured too. Stdlib ET refuses external entities and, since
Python 3.12, raises "limit on input amplification factor" on a bomb; xmlschema
refuses external entities. Neither takes a comparable `parser=` argument, and
swapping them to `defusedxml` would change behaviour (it raises
EntitiesForbidden on *internal* entities) for no security gain.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOTS = [
    Path(__file__).resolve().parents[2] / "app",
]

# lxml functions where the parser argument carries the safety configuration.
_PARSE_FUNCS = {"parse", "fromstring", "XML"}


def _lxml_parse_calls(tree: ast.AST, source_path: Path):
    """Yield (lineno, func_name, has_parser) for lxml etree parse-ish calls.

    Matched on `etree.<func>(...)` because that is how every call site in this
    codebase spells it. A module that imports `parse` bare would be missed; the
    check below asserts a known population so that shape cannot silently empty
    this test out.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in _PARSE_FUNCS:
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "etree"):
            continue
        has_parser = any(kw.arg == "parser" for kw in node.keywords)
        # etree.parse(source, parser) — second positional is the parser.
        if not has_parser and fn.attr in {"parse", "fromstring"} and len(node.args) >= 2:
            has_parser = True
        yield node.lineno, fn.attr, has_parser


def _all_calls():
    found = []
    for root in APP_ROOTS:
        for py in root.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover — not this test's job
                continue
            for lineno, fname, has_parser in _lxml_parse_calls(tree, py):
                found.append((py, lineno, fname, has_parser))
    return found


def test_the_scan_actually_finds_call_sites():
    """Guards the guard.

    An AST matcher that silently matches nothing would make the real assertion
    below vacuous and permanently green. This codebase has five lxml parse
    sites; if a refactor legitimately drops below that, lower the number
    deliberately rather than deleting the check.
    """
    assert len(_all_calls()) >= 5, (
        "found fewer lxml parse calls than expected — the matcher is probably "
        "broken, not the tree"
    )


@pytest.mark.parametrize("case", _all_calls(), ids=lambda c: f"{c[0].name}:{c[1]}")
def test_lxml_parse_passes_an_explicit_parser(case):
    path, lineno, fname, has_parser = case
    assert has_parser, (
        f"{path.name}:{lineno} calls etree.{fname}() without a parser. Pass one "
        f"— e.g. etree.XMLParser(resolve_entities=False, no_network=True) — to "
        f"match the other call sites. See this module's docstring for why this "
        f"is a consistency rule rather than a live-vulnerability rule."
    )
