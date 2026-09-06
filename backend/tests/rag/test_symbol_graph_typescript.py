# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the TypeScript / JavaScript symbol-graph extractor (Slice 22b).

Pure tree-sitter exercise — no DB, no LLM. Covers both .ts and .tsx
grammars + plain .js parsing through the TS grammar.
"""
from __future__ import annotations

import pytest

from app.rag import symbol_graph_extractor_typescript as extractor


# ──────────────────────────────────────────────────────────────────────────────
# Source fixtures
# ──────────────────────────────────────────────────────────────────────────────

SAMPLE_IMPORTS = """\
import express from 'express';
import { Router, Request } from 'express';
import { OldName as Renamed } from 'old-mod';
import * as fs from 'fs';
import 'side-effect-only';
const lodash = require('lodash');
"""

SAMPLE_CLASS_TS = """\
import { BaseService } from './base';

interface Logger {
  log(s: string): void;
}

abstract class AbstractRepo<T> {
  abstract find(id: string): T | null;
}

export class UserRepo extends AbstractRepo<User> implements Logger, Cache {
  log(s: string): void {
    console.log(s);
  }
  find(id: string) {
    this.log("finding " + id);
    return helper(id);
  }
}

class Solo {
  lonely() { return 0; }
}
"""

SAMPLE_CALLS_JS = """\
function topLevel() {
  return helperOne() + helperTwo();
}

function helperOne() {
  return 1;
}

function helperTwo() {
  return helperOne() * 2;
}

class Worker {
  run() {
    helperOne();
    this.compute();
    return this.compute();    // duplicate — calls list dedups
  }
  compute() {
    return helperTwo();
  }
}
"""

SAMPLE_TSX = """\
import React from 'react';

class Greet extends React.Component {
  render() {
    return <div>{this.greet()}</div>;
  }
  greet() {
    return 'hello';
  }
}
"""


# ──────────────────────────────────────────────────────────────────────────────
# imports
# ──────────────────────────────────────────────────────────────────────────────

class TestImports:

    def test_default_import(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        assert "express.express" in graph.imports

    def test_named_imports(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        assert "express.Router" in graph.imports
        assert "express.Request" in graph.imports

    def test_aliased_named_import_drops_alias(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        # `import { OldName as Renamed }` keeps OldName, drops Renamed
        assert "old-mod.OldName" in graph.imports
        assert all("Renamed" not in i for i in graph.imports)

    def test_namespace_import_uses_star(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        assert "fs.*" in graph.imports

    def test_side_effect_import(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        assert "side-effect-only" in graph.imports

    def test_require_call_captured(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        assert "lodash" in graph.imports


# ──────────────────────────────────────────────────────────────────────────────
# classes
# ──────────────────────────────────────────────────────────────────────────────

class TestClasses:

    def test_class_names_extracted(self):
        graph = extractor.extract(SAMPLE_CLASS_TS)
        names = {c.name for c in graph.classes}
        assert "UserRepo" in names
        assert "Solo" in names
        assert "AbstractRepo" in names

    def test_extends_captured(self):
        graph = extractor.extract(SAMPLE_CLASS_TS)
        repo = next(c for c in graph.classes if c.name == "UserRepo")
        assert repo.inherits == "AbstractRepo"

    def test_implements_captured(self):
        graph = extractor.extract(SAMPLE_CLASS_TS)
        repo = next(c for c in graph.classes if c.name == "UserRepo")
        assert "Logger" in repo.implements
        assert "Cache" in repo.implements

    def test_solo_class_no_inheritance(self):
        graph = extractor.extract(SAMPLE_CLASS_TS)
        solo = next(c for c in graph.classes if c.name == "Solo")
        assert solo.inherits is None
        assert solo.implements == []

    def test_methods_collected_per_class(self):
        graph = extractor.extract(SAMPLE_CLASS_TS)
        repo = next(c for c in graph.classes if c.name == "UserRepo")
        method_names = {m.name for m in repo.methods}
        assert "log" in method_names
        assert "find" in method_names


# ──────────────────────────────────────────────────────────────────────────────
# calls
# ──────────────────────────────────────────────────────────────────────────────

class TestCalls:

    def test_module_function_calls_collected(self):
        graph = extractor.extract(SAMPLE_CALLS_JS)
        top = next(f for f in graph.module_functions if f.name == "topLevel")
        assert "helperOne" in top.calls
        assert "helperTwo" in top.calls

    def test_method_attribute_call_uses_rightmost_name(self):
        graph = extractor.extract(SAMPLE_CALLS_JS)
        worker = next(c for c in graph.classes if c.name == "Worker")
        run = next(m for m in worker.methods if m.name == "run")
        assert "helperOne" in run.calls
        assert "compute" in run.calls

    def test_calls_dedup_preserves_first_appearance_order(self):
        graph = extractor.extract(SAMPLE_CALLS_JS)
        worker = next(c for c in graph.classes if c.name == "Worker")
        run = next(m for m in worker.methods if m.name == "run")
        assert run.calls.count("compute") == 1
        assert run.calls.index("helperOne") < run.calls.index("compute")


# ──────────────────────────────────────────────────────────────────────────────
# called_by
# ──────────────────────────────────────────────────────────────────────────────

class TestCalledBy:

    def test_module_fn_called_by_other_callers(self):
        graph = extractor.extract(SAMPLE_CALLS_JS)
        helperOne = next(f for f in graph.module_functions if f.name == "helperOne")
        assert "topLevel" in helperOne.called_by
        assert "helperTwo" in helperOne.called_by
        assert "run" in helperOne.called_by

    def test_method_called_by_only_in_file(self):
        graph = extractor.extract(SAMPLE_CALLS_JS)
        worker = next(c for c in graph.classes if c.name == "Worker")
        compute = next(m for m in worker.methods if m.name == "compute")
        assert "run" in compute.called_by


# ──────────────────────────────────────────────────────────────────────────────
# TSX (JSX-aware grammar)
# ──────────────────────────────────────────────────────────────────────────────

class TestTsx:

    def test_tsx_class_extends_react_component(self):
        graph = extractor.extract(SAMPLE_TSX, is_tsx=True)
        names = {c.name for c in graph.classes}
        assert "Greet" in names
        greet = next(c for c in graph.classes if c.name == "Greet")
        # extends React.Component → rightmost identifier is "Component"
        assert greet.inherits == "Component"

    def test_tsx_method_calls_resolved_within_class(self):
        graph = extractor.extract(SAMPLE_TSX, is_tsx=True)
        greet = next(c for c in graph.classes if c.name == "Greet")
        render = next(m for m in greet.methods if m.name == "render")
        # this.greet() captured by rightmost name
        assert "greet" in render.calls

    def test_tsx_called_by_within_class(self):
        graph = extractor.extract(SAMPLE_TSX, is_tsx=True)
        greet = next(c for c in graph.classes if c.name == "Greet")
        method_greet = next(m for m in greet.methods if m.name == "greet")
        assert "render" in method_greet.called_by


# ──────────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_empty_string(self):
        graph = extractor.extract("")
        assert graph.imports == []
        assert graph.classes == []
        assert graph.module_functions == []

    def test_whitespace_only(self):
        graph = extractor.extract("   \n\t\n")
        assert graph.classes == []

    def test_syntax_error_doesnt_crash(self):
        # Tree-sitter's recovery on broken TS/JS varies — we only require
        # `extract` to not raise on malformed input.
        broken = "function x( {}\n\nclass Y { m() {} }"
        graph = extractor.extract(broken)
        # Either we parsed something or we got an empty graph; both are fine.
        assert isinstance(graph.classes, list)
        assert isinstance(graph.module_functions, list)


# ──────────────────────────────────────────────────────────────────────────────
# to_dict
# ──────────────────────────────────────────────────────────────────────────────

class TestToDict:

    def test_dict_shape(self):
        graph = extractor.extract(SAMPLE_CALLS_JS)
        d = extractor.to_dict(graph)
        assert "imports" in d
        assert "classes" in d
        assert "module_functions" in d

    def test_dict_json_serialisable(self):
        import json
        graph = extractor.extract(SAMPLE_CLASS_TS)
        json.dumps(extractor.to_dict(graph))


# ──────────────────────────────────────────────────────────────────────────────
# Graceful degradation
# ──────────────────────────────────────────────────────────────────────────────

class TestGracefulDegradation:

    def test_returns_empty_graph_when_grammar_unavailable(self, monkeypatch):
        extractor._reset_parser_for_tests()
        monkeypatch.setattr(extractor, "_get_parser", lambda is_tsx=False: None)
        graph = extractor.extract("class X { m() {} }")
        assert graph.classes == []
        extractor._reset_parser_for_tests()
