# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 17 — tree-sitter-based Java symbol-graph extractor.

Pure — uses the real tree-sitter-java grammar (installed in Slice 3) to
verify extraction on concrete Java source fixtures. No DB, no LLM.
"""
from __future__ import annotations

import pytest

from app.rag import symbol_graph_extractor_java as extractor


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

_SIMPLE_CLASS = """
package com.acme.pay;

import java.util.List;
import com.acme.tenant.TenantContext;
import com.acme.ratelimit.RateLimiter;

public class TieredRateLimiter extends RateLimiter implements AutoCloseable, Runnable {

    private int counter;

    public boolean acquire(TenantContext ctx, int permits) {
        this.counter += permits;
        return validate(ctx) && this.counter <= getLimit();
    }

    private boolean validate(TenantContext ctx) {
        return ctx.isEnterprise();
    }

    @Override
    public void close() {
        this.counter = 0;
    }

    @Override
    public void run() {
        acquire(null, 1);
    }
}
"""


_INTERFACE_ONLY = """
package com.acme.pay;

public interface PaymentGateway extends Closeable {
    boolean charge(int amount);
    default boolean refund(int amount) { return charge(-amount); }
}
"""


_TWO_CLASSES = """
package com.acme;

public class Outer {
    public int call_a() { return helper(); }
    private int helper() { return 42; }
}

class Unrelated {
    void doThing() { Outer.nothing(); }
}
"""


_EMPTY = ""


# ──────────────────────────────────────────────────────────────────────────────
# extract — happy paths
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractClassWithExtendsAndImplements:
    def test_imports_captured(self):
        g = extractor.extract(_SIMPLE_CLASS)
        assert "java.util.List" in g.imports
        assert "com.acme.tenant.TenantContext" in g.imports
        assert "com.acme.ratelimit.RateLimiter" in g.imports

    def test_imports_deduplicated(self):
        g = extractor.extract(_SIMPLE_CLASS + "\nimport java.util.List;\n")
        # "java.util.List" should appear exactly once despite duplicate import lines.
        assert g.imports.count("java.util.List") == 1

    def test_class_name_and_kind(self):
        g = extractor.extract(_SIMPLE_CLASS)
        assert len(g.classes) == 1
        assert g.classes[0].name == "TieredRateLimiter"
        assert g.classes[0].kind == "class"

    def test_inherits_extracted(self):
        g = extractor.extract(_SIMPLE_CLASS)
        assert g.classes[0].inherits == "RateLimiter"

    def test_implements_extracted(self):
        g = extractor.extract(_SIMPLE_CLASS)
        cls = g.classes[0]
        assert "AutoCloseable" in cls.implements
        assert "Runnable" in cls.implements

    def test_methods_captured_in_order(self):
        g = extractor.extract(_SIMPLE_CLASS)
        names = [m.name for m in g.classes[0].methods]
        assert names == ["acquire", "validate", "close", "run"]

    def test_method_calls_captured(self):
        g = extractor.extract(_SIMPLE_CLASS)
        cls = g.classes[0]
        acquire = next(m for m in cls.methods if m.name == "acquire")
        # acquire() calls validate(ctx) AND getLimit()
        assert "validate" in acquire.calls
        assert "getLimit" in acquire.calls
        # isEnterprise() is called inside validate(), not acquire().
        assert "isEnterprise" not in acquire.calls

    def test_method_calls_deduplicated_and_ordered(self):
        src = """
        class X {
            void a() { foo(); bar(); foo(); baz(); bar(); }
        }
        """
        g = extractor.extract(src)
        a = g.classes[0].methods[0]
        # Dedup preserves first-appearance order.
        assert a.calls == ["foo", "bar", "baz"]


# ──────────────────────────────────────────────────────────────────────────────
# extract — within-file called_by
# ──────────────────────────────────────────────────────────────────────────────

class TestCalledByWithinFile:
    def test_called_by_populated_for_local_callees(self):
        g = extractor.extract(_SIMPLE_CLASS)
        cls = g.classes[0]
        validate = next(m for m in cls.methods if m.name == "validate")
        # validate() is called by acquire()
        assert "acquire" in validate.called_by

    def test_called_by_empty_for_methods_never_called(self):
        g = extractor.extract(_SIMPLE_CLASS)
        cls = g.classes[0]
        close = next(m for m in cls.methods if m.name == "close")
        # close() is @Override but never called from within the file.
        assert close.called_by == []

    def test_called_by_spans_across_classes_within_same_file(self):
        """helper() is called by call_a() in the same class Outer."""
        g = extractor.extract(_TWO_CLASSES)
        outer = next(c for c in g.classes if c.name == "Outer")
        helper = next(m for m in outer.methods if m.name == "helper")
        assert "call_a" in helper.called_by

    def test_external_method_invocation_ignored_in_called_by(self):
        """External calls like Outer.nothing() don't populate called_by for
        methods we don't own (we have no nothing() method in this file)."""
        g = extractor.extract(_TWO_CLASSES)
        # nothing() is not a method in any of our classes; won't appear.
        for cls in g.classes:
            for m in cls.methods:
                assert m.name != "nothing"


# ──────────────────────────────────────────────────────────────────────────────
# Interfaces, enums, records
# ──────────────────────────────────────────────────────────────────────────────

class TestInterfaceExtraction:
    def test_interface_kind_identified(self):
        g = extractor.extract(_INTERFACE_ONLY)
        assert len(g.classes) == 1
        assert g.classes[0].kind == "interface"
        assert g.classes[0].name == "PaymentGateway"

    def test_interface_has_no_superclass_field(self):
        """Plain extends on an interface is NOT captured as `inherits` here —
        that field is class-specific in our MVP scope."""
        g = extractor.extract(_INTERFACE_ONLY)
        assert g.classes[0].inherits is None


# ──────────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_input_returns_empty_graph(self):
        g = extractor.extract(_EMPTY)
        assert g.imports == []
        assert g.classes == []

    def test_whitespace_only_returns_empty_graph(self):
        g = extractor.extract("   \n\n  \t  ")
        assert g.imports == []
        assert g.classes == []

    def test_multiple_classes_both_extracted(self):
        g = extractor.extract(_TWO_CLASSES)
        names = sorted(c.name for c in g.classes)
        assert names == ["Outer", "Unrelated"]

    def test_no_extends_no_implements_yields_none_and_empty(self):
        src = "public class Plain { void noop() {} }"
        g = extractor.extract(src)
        cls = g.classes[0]
        assert cls.inherits is None
        assert cls.implements == []


# ──────────────────────────────────────────────────────────────────────────────
# to_dict — serialisation
# ──────────────────────────────────────────────────────────────────────────────

class TestToDict:
    def test_to_dict_has_expected_shape(self):
        g = extractor.extract(_SIMPLE_CLASS)
        d = extractor.to_dict(g)
        assert isinstance(d["imports"], list)
        assert isinstance(d["classes"], list)
        assert d["classes"][0]["name"] == "TieredRateLimiter"
        assert d["classes"][0]["kind"] == "class"
        assert d["classes"][0]["inherits"] == "RateLimiter"
        assert d["classes"][0]["implements"] == ["AutoCloseable", "Runnable"]
        methods = d["classes"][0]["methods"]
        assert any(m["name"] == "acquire" for m in methods)

    def test_to_dict_is_json_serialisable(self):
        import json
        g = extractor.extract(_SIMPLE_CLASS)
        d = extractor.to_dict(g)
        json_str = json.dumps(d)   # must not raise
        assert "TieredRateLimiter" in json_str


# ──────────────────────────────────────────────────────────────────────────────
# Graceful degradation when grammar unavailable
# ──────────────────────────────────────────────────────────────────────────────

class TestGracefulDegradation:
    def test_returns_empty_when_grammar_unavailable(self, monkeypatch):
        # Force _get_parser() to fail by blocking the import.
        import builtins
        real_import = builtins.__import__
        extractor._reset_parser_for_tests()

        def blocking_import(name, *args, **kwargs):
            if name.startswith("tree_sitter"):
                raise ImportError("grammar not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocking_import)

        g = extractor.extract(_SIMPLE_CLASS)
        assert g.imports == []
        assert g.classes == []

        # Restore for subsequent tests.
        extractor._reset_parser_for_tests()
