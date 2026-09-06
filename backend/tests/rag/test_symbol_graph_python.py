# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the Python symbol-graph extractor (Slice 22a).

Mirrors `test_symbol_graph_java.py` for the Python grammar. Pure tree-
sitter exercise — no DB, no LLM. Runs as a regular pytest test.
"""
from __future__ import annotations

import pytest

from app.rag import symbol_graph_extractor_python as extractor


# ──────────────────────────────────────────────────────────────────────────────
# Source fixtures — small Python snippets covering each grammar feature
# ──────────────────────────────────────────────────────────────────────────────

SAMPLE_IMPORTS = '''\
import os
import sys, json
import pathlib as pl
from collections import OrderedDict, defaultdict
from collections import OrderedDict   # duplicate — should not double-count
from . import sibling
from .relmod import helper
from a.b.c import D
from .relpkg.sub import E as Aliased
'''

SAMPLE_CLASS = '''\
class Base:
    def first(self):
        return 1


class Derived(Base):
    """Single inheritance."""
    def __init__(self):
        self.x = self.helper()

    def helper(self):
        return 42

    @staticmethod
    def free_helper():
        return None


class Multi(Base, OtherMixin, metaclass=Meta):
    pass


class Solo:
    def lonely(self):
        pass
'''

SAMPLE_CALLS = '''\
def top_level():
    return helper_one() + helper_two()

def helper_one():
    return 1

def helper_two():
    return helper_one() * 2

class Worker:
    def run(self):
        helper_one()
        self.compute()
        return self.compute()    # duplicate — calls list dedups

    def compute(self):
        return helper_two()
'''

SAMPLE_DECORATED = '''\
import functools

@functools.lru_cache(maxsize=None)
def cached_top():
    return 1

class Service:
    @property
    def status(self):
        return "ok"

    @staticmethod
    @functools.lru_cache(maxsize=4)
    def utility():
        return cached_top()
'''

SAMPLE_NESTED = '''\
class Outer:
    def parent(self):
        def inner_helper():
            return 1
        return inner_helper()

class Container:
    class Inner:
        def deep(self):
            return 2
'''

SAMPLE_ASYNC = '''\
import asyncio

async def fetch():
    return await produce()

async def produce():
    return 1

class AsyncWorker:
    async def run(self):
        return await fetch()
'''


# ──────────────────────────────────────────────────────────────────────────────
# imports
# ──────────────────────────────────────────────────────────────────────────────

class TestImports:

    def test_simple_imports_dedup_and_alias_dropped(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        assert "os" in graph.imports
        assert "sys" in graph.imports
        assert "json" in graph.imports
        # `import pathlib as pl` → "pathlib" (alias dropped)
        assert "pathlib" in graph.imports
        # Duplicate `from collections import OrderedDict` only counted once.
        assert graph.imports.count("collections.OrderedDict") == 1

    def test_from_imports_module_qualified(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        assert "collections.OrderedDict" in graph.imports
        assert "collections.defaultdict" in graph.imports
        assert "a.b.c.D" in graph.imports

    def test_relative_imports_kept_with_dots(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        assert any(
            i.startswith(".") and "sibling" in i for i in graph.imports
        ), f"expected '.sibling'-style entry in {graph.imports}"
        assert any(
            i.startswith(".") and "helper" in i for i in graph.imports
        ), f"expected '.relmod.helper' in {graph.imports}"

    def test_aliased_from_import_drops_alias(self):
        graph = extractor.extract(SAMPLE_IMPORTS)
        # `from .relpkg.sub import E as Aliased` → "...E", never "...Aliased"
        assert any(i.endswith(".E") for i in graph.imports)
        assert all("Aliased" not in i for i in graph.imports)


# ──────────────────────────────────────────────────────────────────────────────
# classes
# ──────────────────────────────────────────────────────────────────────────────

class TestClasses:

    def test_classes_extracted_with_kind(self):
        graph = extractor.extract(SAMPLE_CLASS)
        names = [c.name for c in graph.classes]
        assert "Base" in names
        assert "Derived" in names
        assert "Multi" in names
        assert "Solo" in names
        assert all(c.kind == "class" for c in graph.classes)

    def test_inherits_first_base(self):
        graph = extractor.extract(SAMPLE_CLASS)
        derived = next(c for c in graph.classes if c.name == "Derived")
        assert derived.inherits == "Base"
        assert derived.implements == []

    def test_implements_remaining_bases_plus_keyword(self):
        graph = extractor.extract(SAMPLE_CLASS)
        multi = next(c for c in graph.classes if c.name == "Multi")
        assert multi.inherits == "Base"
        assert "OtherMixin" in multi.implements
        assert "Meta" in multi.implements   # metaclass=Meta keyword

    def test_solo_class_has_no_inherits(self):
        graph = extractor.extract(SAMPLE_CLASS)
        solo = next(c for c in graph.classes if c.name == "Solo")
        assert solo.inherits is None
        assert solo.implements == []

    def test_methods_collected_per_class(self):
        graph = extractor.extract(SAMPLE_CLASS)
        derived = next(c for c in graph.classes if c.name == "Derived")
        method_names = [m.name for m in derived.methods]
        assert "__init__" in method_names
        assert "helper" in method_names
        assert "free_helper" in method_names    # @staticmethod still captured


# ──────────────────────────────────────────────────────────────────────────────
# calls
# ──────────────────────────────────────────────────────────────────────────────

class TestCalls:

    def test_module_function_calls_collected(self):
        graph = extractor.extract(SAMPLE_CALLS)
        top = next(f for f in graph.module_functions if f.name == "top_level")
        assert "helper_one" in top.calls
        assert "helper_two" in top.calls

    def test_method_calls_attribute_collected_as_rightmost_name(self):
        graph = extractor.extract(SAMPLE_CALLS)
        worker = next(c for c in graph.classes if c.name == "Worker")
        run = next(m for m in worker.methods if m.name == "run")
        # bare call + self.compute → both captured by rightmost name
        assert "helper_one" in run.calls
        assert "compute" in run.calls

    def test_calls_deduplicated_preserve_first_appearance_order(self):
        graph = extractor.extract(SAMPLE_CALLS)
        worker = next(c for c in graph.classes if c.name == "Worker")
        run = next(m for m in worker.methods if m.name == "run")
        # `self.compute()` appears twice in body — should be in calls exactly once.
        assert run.calls.count("compute") == 1
        # Order: helper_one comes before compute (first appearance order)
        assert run.calls.index("helper_one") < run.calls.index("compute")


# ──────────────────────────────────────────────────────────────────────────────
# called_by within file
# ──────────────────────────────────────────────────────────────────────────────

class TestCalledByWithinFile:

    def test_module_function_called_by_other_module_function(self):
        graph = extractor.extract(SAMPLE_CALLS)
        helper_one = next(f for f in graph.module_functions if f.name == "helper_one")
        # Both top_level and helper_two and Worker.run call helper_one.
        assert "top_level" in helper_one.called_by
        assert "helper_two" in helper_one.called_by
        assert "run" in helper_one.called_by

    def test_method_called_by_only_its_in_file_callers(self):
        graph = extractor.extract(SAMPLE_CALLS)
        worker = next(c for c in graph.classes if c.name == "Worker")
        compute = next(m for m in worker.methods if m.name == "compute")
        assert "run" in compute.called_by

    def test_unreferenced_method_has_empty_called_by(self):
        graph = extractor.extract(SAMPLE_CLASS)
        solo = next(c for c in graph.classes if c.name == "Solo")
        lonely = next(m for m in solo.methods if m.name == "lonely")
        assert lonely.called_by == []


# ──────────────────────────────────────────────────────────────────────────────
# decorated / nested / async
# ──────────────────────────────────────────────────────────────────────────────

class TestDecoratedAndNested:

    def test_decorated_module_function_extracted(self):
        graph = extractor.extract(SAMPLE_DECORATED)
        names = [f.name for f in graph.module_functions]
        assert "cached_top" in names

    def test_decorated_class_method_extracted(self):
        graph = extractor.extract(SAMPLE_DECORATED)
        service = next(c for c in graph.classes if c.name == "Service")
        method_names = [m.name for m in service.methods]
        assert "status" in method_names           # @property
        assert "utility" in method_names          # stacked decorators

    def test_nested_function_not_promoted_to_module(self):
        graph = extractor.extract(SAMPLE_NESTED)
        names = [f.name for f in graph.module_functions]
        # `inner_helper` is nested inside Outer.parent — must NOT appear at module scope.
        assert "inner_helper" not in names

    def test_nested_class_methods_captured(self):
        graph = extractor.extract(SAMPLE_NESTED)
        # Both Container and Container.Inner are class_definition nodes, so we
        # expect both classes in the graph.
        names = [c.name for c in graph.classes]
        assert "Container" in names
        assert "Inner" in names
        inner = next(c for c in graph.classes if c.name == "Inner")
        assert any(m.name == "deep" for m in inner.methods)

    def test_async_functions_extracted(self):
        graph = extractor.extract(SAMPLE_ASYNC)
        mod_names = [f.name for f in graph.module_functions]
        assert "fetch" in mod_names
        assert "produce" in mod_names

    def test_async_method_extracted(self):
        graph = extractor.extract(SAMPLE_ASYNC)
        worker = next(c for c in graph.classes if c.name == "AsyncWorker")
        assert any(m.name == "run" for m in worker.methods)


# ──────────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_empty_string_returns_empty_graph(self):
        graph = extractor.extract("")
        assert graph.imports == []
        assert graph.classes == []
        assert graph.module_functions == []

    def test_whitespace_only_returns_empty_graph(self):
        graph = extractor.extract("   \n\t\n  ")
        assert graph.imports == []
        assert graph.classes == []

    def test_syntax_error_doesnt_crash(self):
        """tree-sitter is error-tolerant; we should still return a graph
        (possibly partial) for slightly-broken input."""
        broken = "def x(:\n    return 1\n\nclass Y:\n    def m(self): pass\n"
        graph = extractor.extract(broken)
        # Y.m should still be reachable.
        assert any(c.name == "Y" for c in graph.classes)


# ──────────────────────────────────────────────────────────────────────────────
# to_dict
# ──────────────────────────────────────────────────────────────────────────────

class TestToDict:

    def test_dict_shape(self):
        graph = extractor.extract(SAMPLE_CALLS)
        d = extractor.to_dict(graph)
        assert "imports" in d
        assert "classes" in d
        assert "module_functions" in d
        # module_functions are dicts with name/calls/called_by
        first_mod = d["module_functions"][0]
        assert "name" in first_mod and "calls" in first_mod and "called_by" in first_mod

    def test_dict_is_json_serialisable(self):
        import json
        graph = extractor.extract(SAMPLE_CLASS)
        d = extractor.to_dict(graph)
        # Should round-trip through json without error.
        json.dumps(d)


# ──────────────────────────────────────────────────────────────────────────────
# Graceful degradation (grammar unavailable)
# ──────────────────────────────────────────────────────────────────────────────

class TestGracefulDegradation:

    def test_returns_empty_graph_when_grammar_unavailable(self, monkeypatch):
        # Force the parser-init to fail by stubbing _get_parser → None.
        extractor._reset_parser_for_tests()
        monkeypatch.setattr(extractor, "_get_parser", lambda: None)
        graph = extractor.extract("def x(): pass")
        assert graph.imports == []
        assert graph.classes == []
        assert graph.module_functions == []
        extractor._reset_parser_for_tests()
