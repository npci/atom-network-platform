# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for sub-slice 19a — RAG→AGE graph projection.

Split into four layers:
  1. classify_chunk (pure, tiny)
  2. Cypher builders (pure, string-shape assertions)
  3. plan_ingest_from_chunks (pure, inspect the planned ops)
  4. execute_plan (DI-friendly; fake run_cypher_fn captures calls + injects failures)
"""
from __future__ import annotations

import pytest

from app.kg import ingest_from_rag as ifr
from app.kg.ingest_from_rag import (
    IngestPlan,
    IngestReport,
    build_calls_edge_within_file,
    build_class_node,
    build_cross_file_call_edge,
    build_describes_edge,
    build_doc_chunk_node,
    build_document_node,
    build_file_node,
    build_function_node,
    build_implements_edge_within_file,
    build_inherits_edge_within_file,
    build_merge_edge_cypher,
    build_merge_node_cypher,
    classify_chunk,
    execute_plan,
    plan_ingest_from_chunks,
)


# ──────────────────────────────────────────────────────────────────────────────
# classify_chunk
# ──────────────────────────────────────────────────────────────────────────────

class TestClassifyChunk:

    def test_class_symbol_kind(self):
        assert classify_chunk({"symbol_kind": "class"}) == "class"

    def test_interface_classifies_as_class(self):
        assert classify_chunk({"symbol_kind": "interface"}) == "class"

    def test_method_classifies_as_function(self):
        assert classify_chunk({"symbol_kind": "method"}) == "function"

    def test_function_symbol_kind(self):
        assert classify_chunk({"symbol_kind": "function"}) == "function"

    def test_constructor_classifies_as_function(self):
        assert classify_chunk({"symbol_kind": "constructor"}) == "function"

    def test_explicit_file_marker(self):
        assert classify_chunk({"symbol_kind": "file"}) == "file_level"

    def test_java_source_category_no_kind_is_file(self):
        # Regex-chunked Java gives doc_category=java_source with no symbol_kind
        # → treat as file-level container.
        assert classify_chunk({
            "doc_category": "java_source", "symbol_kind": None,
        }) == "file_level"

    def test_plain_narrative_doc(self):
        assert classify_chunk({
            "doc_category": "rbi_guideline", "symbol_kind": None,
        }) == "doc"

    def test_code_with_language_but_no_kind_is_file(self):
        # Pure tree-sitter path: file-level row carries `language` but no
        # symbol_kind — treat as File.
        assert classify_chunk({"language": "java", "symbol_kind": None}) == "file_level"

    def test_mixed_case_symbol_kind_tolerated(self):
        assert classify_chunk({"symbol_kind": "Class"}) == "class"


# ──────────────────────────────────────────────────────────────────────────────
# Generic MERGE builders
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildMergeNodeCypher:

    def test_basic_merge_with_key_only(self):
        out = build_merge_node_cypher("Document", key_props={"source_file": "x.pdf"})
        assert "MERGE (n:Document {source_file: 'x.pdf'})" in out
        assert "SET" not in out

    def test_set_clause_present_when_set_props_given(self):
        out = build_merge_node_cypher(
            "Document",
            key_props={"source_file": "x.pdf"},
            set_props={"author": "alice"},
        )
        assert "MERGE (n:Document {source_file: 'x.pdf'})" in out
        assert "SET n.author = 'alice'" in out

    def test_none_values_dropped_from_set_clause(self):
        out = build_merge_node_cypher(
            "Document",
            key_props={"source_file": "x.pdf"},
            set_props={"author": "alice", "product_area": None, "deprecated": False},
        )
        assert "product_area" not in out
        assert "author = 'alice'" in out
        assert "deprecated = false" in out

    def test_invalid_label_rejected(self):
        with pytest.raises(ValueError, match="invalid node label"):
            build_merge_node_cypher(" bad", key_props={"a": 1})

    def test_all_null_key_props_rejected(self):
        with pytest.raises(ValueError, match="non-None"):
            build_merge_node_cypher("Document", key_props={"source_file": None})

    def test_custom_variable_name_used(self):
        out = build_merge_node_cypher(
            "File", key_props={"source_file": "f.java"}, var="v",
        )
        assert "(v:File" in out


class TestBuildMergeEdgeCypher:

    def test_basic_edge(self):
        out = build_merge_edge_cypher(
            src_label="Function", src_key={"chunk_id": "a"},
            dst_label="Function", dst_key={"source_file": "f.java", "symbol_name": "bar"},
            edge_label="CALLS",
        )
        assert "MATCH (a:Function {chunk_id: 'a'})" in out
        assert "MATCH (b:Function {source_file: 'f.java', symbol_name: 'bar'})" in out
        assert "MERGE (a)-[r:CALLS]->(b)" in out

    def test_edge_props_add_set(self):
        out = build_merge_edge_cypher(
            src_label="DocChunk", src_key={"chunk_id": "d"},
            dst_label="Function", dst_key={"chunk_id": "s"},
            edge_label="DESCRIBES",
            edge_props={"confidence": 0.85},
        )
        assert "SET r.confidence = 0.85" in out

    def test_invalid_label_rejected(self):
        with pytest.raises(ValueError, match="invalid label"):
            build_merge_edge_cypher(
                src_label="bad label", src_key={"x": 1},
                dst_label="Y", dst_key={"x": 2}, edge_label="E",
            )

    def test_empty_key_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            build_merge_edge_cypher(
                src_label="A", src_key={}, dst_label="B", dst_key={"x": 1},
                edge_label="E",
            )


# ──────────────────────────────────────────────────────────────────────────────
# Specific node builders
# ──────────────────────────────────────────────────────────────────────────────

class TestNodeBuilders:

    def test_document_node_keyed_on_source_file(self):
        out = build_document_node({
            "source_file":  "rbi_spec.pdf",
            "doc_category": "rbi_guideline",
            "product_area": "network-core",
        })
        assert "MERGE (n:Document {source_file: 'rbi_spec.pdf'})" in out
        assert "doc_category = 'rbi_guideline'" in out
        assert "product_area = 'network-core'" in out

    def test_doc_chunk_node_keyed_on_id(self):
        out = build_doc_chunk_node({
            "id":           "uuid-1",
            "source_file":  "rbi.pdf",
            "doc_category": "rbi_guideline",
            "chunk_index":  2,
            "title_breadcrumb": "§2.1 Retry",
        })
        assert "MERGE (n:DocChunk {chunk_id: 'uuid-1'})" in out
        assert "chunk_index = 2" in out
        assert "title_breadcrumb = '§2.1 Retry'" in out

    def test_file_node_serializes_imports_list(self):
        out = build_file_node({
            "source_file": "Switch.java",
            "language":    "java",
            "imports":     ["java.util.List", "java.util.Map"],
        })
        assert "MERGE (n:File {source_file: 'Switch.java'})" in out
        assert "language = 'java'" in out
        assert '"java.util.List"' in out  # JSON-encoded

    def test_class_node_carries_signature_and_lines(self):
        out = build_class_node({
            "id":          "class-uuid",
            "symbol_name": "NetworkSwitchService",
            "source_file": "NetworkSwitchService.java",
            "signature":   "public class NetworkSwitchService",
            "line_start":  10,
            "line_end":    200,
        })
        assert "MERGE (n:Class {chunk_id: 'class-uuid'})" in out
        assert "symbol_name = 'NetworkSwitchService'" in out
        assert "line_start = 10" in out
        assert "line_end = 200" in out

    def test_function_node_carries_parent_symbol_id(self):
        out = build_function_node({
            "id":                "fn-uuid",
            "symbol_name":       "processBalance",
            "source_file":       "Svc.java",
            "parent_symbol_id":  "cls-uuid",
        })
        assert "MERGE (n:Function {chunk_id: 'fn-uuid'})" in out
        assert "parent_symbol_id = 'cls-uuid'" in out


# ──────────────────────────────────────────────────────────────────────────────
# Edge builders
# ──────────────────────────────────────────────────────────────────────────────

class TestEdgeBuilders:

    def test_describes_edge_unlabeled_target(self):
        # Target label omitted so it matches Class OR Function.
        out = build_describes_edge("doc-1", "sym-1", 0.82)
        assert "MATCH (a:DocChunk {chunk_id: 'doc-1'})" in out
        assert "MATCH (b {chunk_id: 'sym-1'})" in out
        assert "MERGE (a)-[r:DESCRIBES]->(b)" in out
        assert "r.confidence = 0.82" in out

    def test_calls_edge_keys_callee_by_name_and_file(self):
        out = build_calls_edge_within_file(
            caller_chunk_id="c1",
            callee_symbol_name="doWork",
            source_file="Svc.java",
        )
        assert "MATCH (a:Function {chunk_id: 'c1'})" in out
        assert "symbol_name: 'doWork'" in out
        assert "source_file: 'Svc.java'" in out
        assert "MERGE (a)-[r:CALLS]->(b)" in out

    def test_inherits_edge_shape(self):
        out = build_inherits_edge_within_file("child-id", "Parent", "F.java")
        assert "MATCH (a:Class {chunk_id: 'child-id'})" in out
        assert "symbol_name: 'Parent'" in out
        assert "MERGE (a)-[r:INHERITS]->(b)" in out

    def test_implements_edge_shape(self):
        out = build_implements_edge_within_file("cls-id", "IFoo", "F.java")
        assert "symbol_name: 'IFoo'" in out
        assert "MERGE (a)-[r:IMPLEMENTS]->(b)" in out

    def test_cross_file_call_edge_shape(self):
        out = build_cross_file_call_edge("caller-1", "utils/helpers.py", "do_thing")
        assert "MATCH (a:Function {chunk_id: 'caller-1'})" in out
        # Callee MATCH is unlabeled so Function or Class nodes both resolve.
        assert "MATCH (b {source_file: 'utils/helpers.py', symbol_name: 'do_thing'})" in out
        assert "MERGE (a)-[r:CALLS]->(b)" in out

    def test_cross_file_call_edge_uses_same_CALLS_label_as_within_file(self):
        within = build_calls_edge_within_file("c1", "callee", "f.py")
        cross  = build_cross_file_call_edge("c1", "other.py", "callee")
        # Same edge label → graph_retriever / impact_analyzer pick up both.
        assert "[r:CALLS]" in within
        assert "[r:CALLS]" in cross


# ──────────────────────────────────────────────────────────────────────────────
# Planner
# ──────────────────────────────────────────────────────────────────────────────

class TestPlanIngestFromChunks:

    def test_empty_input_gives_empty_plan(self):
        plan = plan_ingest_from_chunks([])
        assert isinstance(plan, IngestPlan)
        assert len(plan) == 0
        assert plan.operations == []

    def test_single_doc_chunk_emits_document_plus_docchunk(self):
        plan = plan_ingest_from_chunks([{
            "id": "d1", "source_file": "rbi.pdf",
            "doc_category": "rbi_guideline",
        }])
        kinds = [k for k, _ in plan.operations]
        assert kinds == ["document", "doc_chunk"]

    def test_same_source_file_document_not_duplicated(self):
        plan = plan_ingest_from_chunks([
            {"id": "d1", "source_file": "rbi.pdf", "doc_category": "rbi_guideline"},
            {"id": "d2", "source_file": "rbi.pdf", "doc_category": "rbi_guideline"},
            {"id": "d3", "source_file": "rbi.pdf", "doc_category": "rbi_guideline"},
        ])
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("document") == 1
        assert kinds.count("doc_chunk") == 3

    def test_class_chunk_emits_file_then_class(self):
        plan = plan_ingest_from_chunks([{
            "id": "c1", "source_file": "Svc.java",
            "symbol_kind": "class", "symbol_name": "Svc", "language": "java",
        }])
        kinds = [k for k, _ in plan.operations]
        assert kinds == ["file", "class"]

    def test_function_chunk_emits_file_then_function(self):
        plan = plan_ingest_from_chunks([{
            "id": "f1", "source_file": "Svc.java",
            "symbol_kind": "method", "symbol_name": "doWork", "language": "java",
        }])
        kinds = [k for k, _ in plan.operations]
        assert kinds == ["file", "function"]

    def test_class_and_function_share_one_file_node(self):
        plan = plan_ingest_from_chunks([
            {"id": "c1", "source_file": "Svc.java", "symbol_kind": "class",
             "symbol_name": "Svc"},
            {"id": "f1", "source_file": "Svc.java", "symbol_kind": "method",
             "symbol_name": "doWork"},
        ])
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("file") == 1
        assert "class" in kinds and "function" in kinds

    def test_inherits_edge_planned_when_parent_set(self):
        plan = plan_ingest_from_chunks([{
            "id": "c1", "source_file": "F.java", "symbol_kind": "class",
            "symbol_name": "Child", "inherits": "Parent",
        }])
        kinds = [k for k, _ in plan.operations]
        assert "inherits" in kinds

    def test_inherits_skipped_when_no_parent(self):
        plan = plan_ingest_from_chunks([{
            "id": "c1", "source_file": "F.java", "symbol_kind": "class",
            "symbol_name": "Solo", "inherits": None,
        }])
        kinds = [k for k, _ in plan.operations]
        assert "inherits" not in kinds

    def test_implements_edge_per_interface(self):
        plan = plan_ingest_from_chunks([{
            "id": "c1", "source_file": "F.java", "symbol_kind": "class",
            "symbol_name": "Thing", "implements": ["IFoo", "IBar"],
        }])
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("implements") == 2

    def test_implements_skips_empty_interface_name(self):
        plan = plan_ingest_from_chunks([{
            "id": "c1", "source_file": "F.java", "symbol_kind": "class",
            "symbol_name": "Thing", "implements": ["IFoo", "", None, "IBar"],
        }])
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("implements") == 2

    def test_calls_edges_per_callee(self):
        plan = plan_ingest_from_chunks([{
            "id": "f1", "source_file": "F.java", "symbol_kind": "method",
            "symbol_name": "caller", "calls": ["bar", "baz", "qux"],
        }])
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("calls") == 3

    def test_calls_skipped_when_missing_source_file(self):
        plan = plan_ingest_from_chunks([{
            "id": "f1", "source_file": None, "symbol_kind": "method",
            "symbol_name": "x", "calls": ["bar"],
        }])
        kinds = [k for k, _ in plan.operations]
        assert "calls" not in kinds

    def test_describes_edge_planned_for_each_link(self):
        plan = plan_ingest_from_chunks(
            chunks=[
                {"id": "d1", "source_file": "rbi.pdf", "doc_category": "rbi_guideline"},
                {"id": "s1", "source_file": "Svc.java", "symbol_kind": "method",
                 "symbol_name": "x"},
            ],
            links=[
                {"doc_chunk_id": "d1", "symbol_chunk_id": "s1", "confidence": 0.7},
                {"doc_chunk_id": "d1", "symbol_chunk_id": "s1", "confidence": 0.9},
            ],
        )
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("describes") == 2

    def test_describes_skipped_for_zero_or_none_confidence(self):
        # None confidence is dropped; explicit 0.0 is kept (valid low-confidence).
        plan = plan_ingest_from_chunks(
            chunks=[],
            links=[
                {"doc_chunk_id": "d", "symbol_chunk_id": "s", "confidence": None},
                {"doc_chunk_id": "d", "symbol_chunk_id": "s", "confidence": 0.0},
            ],
        )
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("describes") == 1

    def test_chunks_without_id_skipped(self):
        plan = plan_ingest_from_chunks([
            {"source_file": "rbi.pdf", "doc_category": "rbi_guideline"},  # no id
        ])
        assert len(plan) == 0

    def test_cross_file_calls_emit_one_edge_per_entry(self):
        plan = plan_ingest_from_chunks([{
            "id": "fn1", "source_file": "main.py", "symbol_kind": "function",
            "symbol_name": "caller", "language": "python",
            "cross_file_calls": [
                {"callee_symbol": "do_a", "callee_path": "utils/a.py",
                 "line": 10, "language": "python"},
                {"callee_symbol": "do_b", "callee_path": "utils/b.py",
                 "line": 20, "language": "python"},
            ],
        }])
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("cross_file_calls") == 2

    def test_cross_file_calls_skipped_when_missing_path_or_symbol(self):
        plan = plan_ingest_from_chunks([{
            "id": "fn1", "source_file": "main.py", "symbol_kind": "function",
            "symbol_name": "caller", "language": "python",
            "cross_file_calls": [
                {"callee_symbol": "ok", "callee_path": "x.py"},   # valid
                {"callee_symbol": "no_path"},                     # skipped
                {"callee_path": "no_symbol.py"},                  # skipped
                {},                                               # skipped
            ],
        }])
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("cross_file_calls") == 1

    def test_cross_file_calls_skipped_for_non_dict_entries(self):
        plan = plan_ingest_from_chunks([{
            "id": "fn1", "source_file": "main.py", "symbol_kind": "function",
            "symbol_name": "caller", "language": "python",
            "cross_file_calls": ["string", 42, None],   # all malformed
        }])
        kinds = [k for k, _ in plan.operations]
        assert "cross_file_calls" not in kinds

    def test_cross_file_calls_only_for_function_chunks(self):
        """Class chunks with cross_file_calls (shouldn't happen in practice
        but defensive) — planner only emits for function chunks."""
        plan = plan_ingest_from_chunks([{
            "id": "c1", "source_file": "f.py", "symbol_kind": "class",
            "symbol_name": "Cls", "language": "python",
            "cross_file_calls": [
                {"callee_symbol": "x", "callee_path": "other.py"},
            ],
        }])
        kinds = [k for k, _ in plan.operations]
        assert "cross_file_calls" not in kinds

    def test_within_file_and_cross_file_calls_coexist(self):
        plan = plan_ingest_from_chunks([{
            "id": "fn1", "source_file": "main.py", "symbol_kind": "function",
            "symbol_name": "caller", "language": "python",
            "calls": ["local_helper"],
            "cross_file_calls": [
                {"callee_symbol": "remote_helper", "callee_path": "other.py"},
            ],
        }])
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("calls") == 1                # within-file
        assert kinds.count("cross_file_calls") == 1     # cross-file

    def test_node_ordering_before_edges(self):
        plan = plan_ingest_from_chunks(
            chunks=[
                {"id": "c1", "source_file": "F.java", "symbol_kind": "class",
                 "symbol_name": "Child", "inherits": "Parent",
                 "implements": ["IFoo"]},
                {"id": "f1", "source_file": "F.java", "symbol_kind": "method",
                 "symbol_name": "caller", "calls": ["callee"]},
            ],
            links=[{"doc_chunk_id": "c1", "symbol_chunk_id": "f1", "confidence": 0.5}],
        )
        kinds = [k for k, _ in plan.operations]
        node_kinds = {"document", "doc_chunk", "file", "class", "function"}
        edge_kinds = {"inherits", "implements", "calls", "describes"}
        last_node_idx = max(i for i, k in enumerate(kinds) if k in node_kinds)
        first_edge_idx = min(i for i, k in enumerate(kinds) if k in edge_kinds)
        assert last_node_idx < first_edge_idx


# ──────────────────────────────────────────────────────────────────────────────
# Executor (DI)
# ──────────────────────────────────────────────────────────────────────────────

class TestExecutePlan:

    def test_empty_plan_yields_empty_report(self):
        report = execute_plan(IngestPlan(), run_cypher_fn=lambda c: None)
        assert report.total_successes() == 0
        assert report.total_failures() == 0
        assert report.counts == {}

    def test_all_cyphers_invoked_in_order(self):
        seen: list[str] = []

        def fake(cypher: str) -> None:
            seen.append(cypher)

        plan = IngestPlan(operations=[
            ("document", "MERGE (n:Document {source_file: 'a'})"),
            ("doc_chunk", "MERGE (n:DocChunk {chunk_id: 'b'})"),
        ])
        report = execute_plan(plan, run_cypher_fn=fake)
        assert len(seen) == 2
        assert report.counts == {"document": 1, "doc_chunk": 1}
        assert report.total_failures() == 0

    def test_exception_in_one_op_doesnt_stop_others(self):
        seen: list[str] = []

        def fake(cypher: str) -> None:
            seen.append(cypher)
            if "boom" in cypher:
                raise RuntimeError("synthetic failure")

        plan = IngestPlan(operations=[
            ("document", "MERGE (n:Document {source_file: 'boom'})"),
            ("document", "MERGE (n:Document {source_file: 'ok'})"),
            ("doc_chunk", "MERGE (n:DocChunk {chunk_id: 'c'})"),
        ])
        report = execute_plan(plan, run_cypher_fn=fake)
        assert len(seen) == 3
        assert report.counts == {"document": 1, "doc_chunk": 1}
        assert report.total_failures() == 1
        assert report.failures[0]["kind"] == "document"
        # SCR #6: `failures` is returned by POST /api/kg/ingest. A RuntimeError
        # is not an authored type, so its text is replaced with a category
        # label rather than echoed. The original message is still available to
        # operators on the logger.warning line in execute_plan. The `kind`
        # above is what actually tells a caller which operation failed.
        assert "synthetic failure" not in report.failures[0]["error"]
        assert report.failures[0]["error"] == "an internal processing error occurred"

    def test_failure_never_echoes_the_cypher_query(self):
        """SCR #6: `failures` is returned by POST /api/kg/ingest, so the
        generated query must not travel with it.

        This previously shipped `cypher[:200]` unconditionally — graph labels,
        property names and the interpolated `source_file` path — to any caller
        who could reach the endpoint. The excerpt now goes to the log only.
        """
        def fake(cypher: str) -> None:
            raise RuntimeError("err")

        long_cypher = "MERGE (n:Class {chunk_id: 'x'}) " + ("//pad " * 500)
        plan = IngestPlan(operations=[("class", long_cypher)])
        report = execute_plan(plan, run_cypher_fn=fake)

        assert report.failures[0]["cypher_excerpt"] == ""
        serialised = str(report.failures[0])
        assert "MERGE" not in serialised
        assert "chunk_id" not in serialised

    def test_counts_aggregate_by_kind(self):
        plan = IngestPlan(operations=[
            ("doc_chunk", "MERGE a"),
            ("doc_chunk", "MERGE b"),
            ("doc_chunk", "MERGE c"),
            ("class",     "MERGE d"),
        ])
        report = execute_plan(plan, run_cypher_fn=lambda c: None)
        assert report.counts == {"doc_chunk": 3, "class": 1}


# ──────────────────────────────────────────────────────────────────────────────
# End-to-end (planner + executor composition, still DI)
# ──────────────────────────────────────────────────────────────────────────────

class TestPlannerExecutorComposition:

    def test_synthetic_corpus_flows_through(self):
        """Realistic 4-chunk mini-corpus → planner → fake executor."""
        chunks = [
            # narrative doc
            {"id": "d1", "source_file": "rbi.pdf",
             "doc_category": "rbi_guideline", "chunk_index": 0},
            # class with one interface + one parent
            {"id": "c1", "source_file": "Svc.java", "symbol_kind": "class",
             "symbol_name": "Svc", "inherits": "AbstractSvc",
             "implements": ["IService"]},
            # method calling two others
            {"id": "m1", "source_file": "Svc.java", "symbol_kind": "method",
             "symbol_name": "doWork", "calls": ["helper", "logTx"],
             "parent_symbol_id": "c1"},
            # another method that doesn't call anything
            {"id": "m2", "source_file": "Svc.java", "symbol_kind": "method",
             "symbol_name": "helper"},
        ]
        links = [
            {"doc_chunk_id": "d1", "symbol_chunk_id": "m1", "confidence": 0.75},
        ]

        called: list[str] = []
        plan = plan_ingest_from_chunks(chunks, links)
        report = execute_plan(plan, run_cypher_fn=lambda c: called.append(c))

        # Verify plan contents
        kinds = [k for k, _ in plan.operations]
        assert kinds.count("document") == 1
        assert kinds.count("doc_chunk") == 1
        assert kinds.count("file") == 1           # Svc.java deduped
        assert kinds.count("class") == 1
        assert kinds.count("function") == 2
        assert kinds.count("inherits") == 1
        assert kinds.count("implements") == 1
        assert kinds.count("calls") == 2           # doWork → helper, logTx
        assert kinds.count("describes") == 1

        # All 11 cyphers invoked, no failures
        assert len(called) == 11
        assert report.total_successes() == 11
        assert report.total_failures() == 0


# ──────────────────────────────────────────────────────────────────────────────
# Integration placeholder (skipped when AGE unreachable)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.age
def test_ingest_from_db_runs_against_live_graph():
    """Smoke: project a tiny handcrafted chunk set into the real AGE graph
    via ingest_from_db → count nodes per label. Requires live AGE."""
    from app.core.database import SessionLocal
    from app.kg.client import is_age_available, run_cypher
    from app.kg.schema import initialise_graph

    db = SessionLocal()
    try:
        if not is_age_available(db):
            pytest.skip("Apache AGE not available")
        # Ensure labels exist before any MERGE.
        initialise_graph(db)

        # Plan + execute a 2-chunk synthetic corpus in isolation (no DB data
        # touched — we call execute_plan directly rather than ingest_from_db).
        chunks = [
            {"id": "test-d1", "source_file": "test_smoke.pdf",
             "doc_category": "rbi_guideline"},
            {"id": "test-c1", "source_file": "SmokeSvc.java",
             "symbol_kind": "class", "symbol_name": "SmokeSvc"},
        ]
        plan = ifr.plan_ingest_from_chunks(chunks)

        def _run(c: str) -> None:
            run_cypher(db, c)

        report = ifr.execute_plan(plan, run_cypher_fn=_run)
        assert report.total_failures() == 0
        assert report.total_successes() >= 3    # document + doc_chunk + file + class

        # Cleanup — remove test nodes to keep the graph tidy.
        run_cypher(db, "MATCH (n:Document {source_file: 'test_smoke.pdf'}) DETACH DELETE n")
        run_cypher(db, "MATCH (n:DocChunk {chunk_id: 'test-d1'}) DETACH DELETE n")
        run_cypher(db, "MATCH (n:File {source_file: 'SmokeSvc.java'}) DETACH DELETE n")
        run_cypher(db, "MATCH (n:Class {chunk_id: 'test-c1'}) DETACH DELETE n")
        db.commit()
    finally:
        db.close()
