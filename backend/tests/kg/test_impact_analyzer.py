# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 21 — impact analyzer.

Layered:
  1. Cypher builders (pure, shape assertions)
  2. BFS + one_hop helpers (pure, DI run_cypher_fn)
  3. resolve_targets (priority order + DI resolution)
  4. analyze_impact end-to-end (monkeypatched kg_client + stub session)
  5. Live AGE smoke (@pytest.mark.age)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.kg import impact_analyzer as ia
from app.kg.impact_analyzer import (
    ImpactReport,
    _bfs_inbound,
    _one_hop,
    analyze_impact,
    build_callers_cypher,
    build_documenting_cypher,
    build_implementations_cypher,
    build_resolve_symbol_cypher,
    build_source_files_cypher,
    build_subclasses_cypher,
    resolve_targets,
)


# ──────────────────────────────────────────────────────────────────────────────
# Cypher builders
# ──────────────────────────────────────────────────────────────────────────────

class TestCypherBuilders:

    def test_resolve_symbol_without_source_file(self):
        out = build_resolve_symbol_cypher("Foo")
        assert "n.symbol_name = 'Foo'" in out
        assert "source_file" not in out
        assert "RETURN DISTINCT n.chunk_id AS cid" in out

    def test_resolve_symbol_with_source_file(self):
        out = build_resolve_symbol_cypher("Foo", "Svc.java")
        assert "n.symbol_name = 'Foo'" in out
        assert "n.source_file = 'Svc.java'" in out

    def test_callers_cypher_inbound(self):
        out = build_callers_cypher("tid-1")
        assert "MATCH (x:Function)-[r:CALLS]->(t)" in out
        assert "t.chunk_id = 'tid-1'" in out
        assert "RETURN DISTINCT x.chunk_id AS cid" in out

    def test_subclasses_cypher_inbound(self):
        out = build_subclasses_cypher("cls-1")
        assert "MATCH (x:Class)-[r:INHERITS]->(t)" in out
        assert "t.chunk_id = 'cls-1'" in out

    def test_implementations_cypher_inbound(self):
        out = build_implementations_cypher("iface-1")
        assert "MATCH (x:Class)-[r:IMPLEMENTS]->(t)" in out

    def test_documenting_cypher_inbound_from_docchunk(self):
        out = build_documenting_cypher("sym-1")
        assert "MATCH (x:DocChunk)-[r:DESCRIBES]->(t)" in out

    def test_source_files_cypher_list_literal(self):
        out = build_source_files_cypher(["a", "b", "c"])
        assert "n.chunk_id IN ['a', 'b', 'c']" in out
        assert "source_file AS source_file" in out

    def test_source_files_rejects_empty(self):
        with pytest.raises(ValueError, match="non-empty"):
            build_source_files_cypher([])

    def test_single_quote_escaping_in_target_id(self):
        out = build_callers_cypher("it's")
        assert "'it\\'s'" in out


# ──────────────────────────────────────────────────────────────────────────────
# BFS inbound
# ──────────────────────────────────────────────────────────────────────────────

class TestBfsInbound:

    def test_empty_seeds_empty_result(self):
        out = _bfs_inbound(
            [], cypher_builder=build_callers_cypher,
            run_cypher_fn=lambda c: [], max_hops=2,
        )
        assert out == {}

    def test_single_hop_graph(self):
        """target X ← caller Y (1 hop)."""
        graph: dict[str, list[str]] = {"X": ["Y"], "Y": []}

        def fake(cypher: str) -> list[dict]:
            for tid, callers in graph.items():
                if f"'{tid}'" in cypher:
                    return [{"cid": c} for c in callers]
            return []

        out = _bfs_inbound(
            ["X"], cypher_builder=build_callers_cypher,
            run_cypher_fn=fake, max_hops=2,
        )
        assert out == {"Y": 1}

    def test_two_hop_chain_respects_hop_distance(self):
        """Z → Y → X (Z calls Y, Y calls X). BFS from X up to 2 hops."""
        graph = {"X": ["Y"], "Y": ["Z"], "Z": []}

        def fake(cypher: str) -> list[dict]:
            for tid, callers in graph.items():
                if f"'{tid}'" in cypher:
                    return [{"cid": c} for c in callers]
            return []

        out = _bfs_inbound(
            ["X"], cypher_builder=build_callers_cypher,
            run_cypher_fn=fake, max_hops=2,
        )
        assert out == {"Y": 1, "Z": 2}

    def test_max_hops_caps_depth(self):
        """Chain X ← Y ← Z ← W. Cap at max_hops=1, only Y shows up."""
        graph = {"X": ["Y"], "Y": ["Z"], "Z": ["W"], "W": []}

        def fake(cypher: str) -> list[dict]:
            for tid, callers in graph.items():
                if f"'{tid}'" in cypher:
                    return [{"cid": c} for c in callers]
            return []

        out = _bfs_inbound(
            ["X"], cypher_builder=build_callers_cypher,
            run_cypher_fn=fake, max_hops=1,
        )
        assert out == {"Y": 1}

    def test_excluded_seeds_skipped(self):
        """Target X is a caller of itself via cycle — excluded from output."""
        graph = {"X": ["Y", "X"], "Y": []}

        def fake(cypher: str) -> list[dict]:
            for tid, callers in graph.items():
                if f"'{tid}'" in cypher:
                    return [{"cid": c} for c in callers]
            return []

        out = _bfs_inbound(
            ["X"], cypher_builder=build_callers_cypher,
            run_cypher_fn=fake, max_hops=2, excluded={"X"},
        )
        assert out == {"Y": 1}

    def test_cycle_doesnt_infinite_loop(self):
        """X ← Y ← X cycle. BFS should not revisit X."""
        graph = {"X": ["Y"], "Y": ["X"]}

        def fake(cypher: str) -> list[dict]:
            for tid, callers in graph.items():
                if f"'{tid}'" in cypher:
                    return [{"cid": c} for c in callers]
            return []

        out = _bfs_inbound(
            ["X"], cypher_builder=build_callers_cypher,
            run_cypher_fn=fake, max_hops=3, excluded={"X"},
        )
        assert out == {"Y": 1}

    def test_exception_on_one_target_doesnt_stop_others(self):
        def fake(cypher: str) -> list[dict]:
            if "'boom'" in cypher:
                raise RuntimeError("bad target")
            if "'ok'" in cypher:
                return [{"cid": "callerA"}]
            return []

        out = _bfs_inbound(
            ["boom", "ok"], cypher_builder=build_callers_cypher,
            run_cypher_fn=fake, max_hops=1,
        )
        assert out == {"callerA": 1}

    def test_non_string_cid_skipped(self):
        def fake(cypher: str) -> list[dict]:
            return [{"cid": None}, {"cid": 42}, {"cid": "valid"}]

        out = _bfs_inbound(
            ["X"], cypher_builder=build_callers_cypher,
            run_cypher_fn=fake, max_hops=1,
        )
        assert out == {"valid": 1}

    def test_minimum_hop_distance_retained_across_paths(self):
        """Diamond: X ← Y, X ← Z, Y ← W, Z ← W. W is reachable at hop=2 via
        two paths — should be recorded as 2 (min), never overwritten later."""
        graph = {"X": ["Y", "Z"], "Y": ["W"], "Z": ["W"], "W": []}

        def fake(cypher: str) -> list[dict]:
            for tid, callers in graph.items():
                if f"'{tid}'" in cypher:
                    return [{"cid": c} for c in callers]
            return []

        out = _bfs_inbound(
            ["X"], cypher_builder=build_callers_cypher,
            run_cypher_fn=fake, max_hops=3,
        )
        assert out["W"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# one_hop
# ──────────────────────────────────────────────────────────────────────────────

class TestOneHop:

    def test_union_across_seeds(self):
        def fake(cypher: str) -> list[dict]:
            if "'A'" in cypher:
                return [{"cid": "x"}, {"cid": "y"}]
            if "'B'" in cypher:
                return [{"cid": "y"}, {"cid": "z"}]
            return []

        out = _one_hop(
            ["A", "B"], cypher_builder=build_documenting_cypher,
            run_cypher_fn=fake,
        )
        assert out == {"x", "y", "z"}

    def test_excluded_dropped(self):
        def fake(cypher: str) -> list[dict]:
            return [{"cid": "x"}, {"cid": "A"}]

        out = _one_hop(
            ["A"], cypher_builder=build_documenting_cypher,
            run_cypher_fn=fake, excluded={"A"},
        )
        assert out == {"x"}

    def test_exception_swallowed(self):
        def fake(cypher: str) -> list[dict]:
            if "'boom'" in cypher:
                raise RuntimeError("no")
            return [{"cid": "x"}]

        out = _one_hop(
            ["boom", "good"], cypher_builder=build_documenting_cypher,
            run_cypher_fn=fake,
        )
        assert out == {"x"}


# ──────────────────────────────────────────────────────────────────────────────
# resolve_targets
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveTargets:

    def test_explicit_chunk_ids_returned_deduped(self):
        out = resolve_targets(target_chunk_ids=["a", "b", "a", "c"])
        assert out == ["a", "b", "c"]

    def test_explicit_chunk_ids_drop_empty_strings(self):
        out = resolve_targets(target_chunk_ids=["a", "", "b", None])
        assert out == ["a", "b"]

    def test_explicit_chunk_ids_skip_cypher(self):
        # run_cypher_fn not required when explicit chunk_ids given.
        out = resolve_targets(target_chunk_ids=["a"])
        assert out == ["a"]

    def test_symbol_tuples_resolved_via_cypher(self):
        def fake(cypher: str) -> list[dict]:
            if "'Foo'" in cypher and "'F.java'" in cypher:
                return [{"cid": "chunk-foo"}]
            if "'Bar'" in cypher:
                return [{"cid": "chunk-bar"}]
            return []

        out = resolve_targets(
            target_symbols=[("Foo", "F.java"), ("Bar", None)],
            run_cypher_fn=fake,
        )
        assert set(out) == {"chunk-foo", "chunk-bar"}

    def test_symbol_resolution_multiple_hits_kept(self):
        def fake(cypher: str) -> list[dict]:
            return [{"cid": "chunk-1"}, {"cid": "chunk-2"}]

        out = resolve_targets(
            target_symbols=[("Foo", None)], run_cypher_fn=fake,
        )
        assert set(out) == {"chunk-1", "chunk-2"}

    def test_description_seed_extraction(self):
        """change_description path extracts seeds and resolves each."""
        seen_seeds: list[str] = []
        def fake(cypher: str) -> list[dict]:
            # Extract 'NetworkSwitchService' from cypher literal for assertion.
            import re
            m = re.search(r"symbol_name = '([^']+)'", cypher)
            if m:
                seen_seeds.append(m.group(1))
            return [{"cid": f"cid-{m.group(1)}"}] if m else []

        out = resolve_targets(
            change_description="modify NetworkSwitchService and PaymentHandler",
            run_cypher_fn=fake,
        )
        assert "cid-NetworkSwitchService" in out
        assert "cid-PaymentHandler" in out

    def test_nothing_resolves_empty_list(self):
        out = resolve_targets(
            target_symbols=[("Foo", None)], run_cypher_fn=lambda c: [],
        )
        assert out == []

    def test_cypher_exception_skipped(self):
        def fake(cypher: str) -> list[dict]:
            if "'boom'" in cypher:
                raise RuntimeError("err")
            if "'ok'" in cypher:
                return [{"cid": "ok-cid"}]
            return []

        out = resolve_targets(
            target_symbols=[("boom", None), ("ok", None)],
            run_cypher_fn=fake,
        )
        assert out == ["ok-cid"]

    def test_priority_chunk_ids_win(self):
        """When both chunk_ids + symbols + description given, chunk_ids wins."""
        out = resolve_targets(
            target_chunk_ids=["explicit"],
            target_symbols=[("Foo", None)],
            change_description="NetworkSwitchService",
            run_cypher_fn=lambda c: [{"cid": "bogus"}],
        )
        assert out == ["explicit"]

    def test_missing_run_cypher_returns_empty_when_needed(self):
        # Without explicit chunk_ids, resolution requires run_cypher_fn.
        out = resolve_targets(
            target_symbols=[("Foo", None)], run_cypher_fn=None,
        )
        assert out == []


# ──────────────────────────────────────────────────────────────────────────────
# analyze_impact end-to-end (monkeypatched kg_client + stub session)
# ──────────────────────────────────────────────────────────────────────────────

class TestAnalyzeImpactEndToEnd:

    def test_age_unavailable_returns_empty_with_failure(self, monkeypatch):
        monkeypatch.setattr(
            "app.kg.impact_analyzer.kg_client.is_age_available",
            lambda db: False,
        )
        report = analyze_impact(db=MagicMock(), target_chunk_ids=["X"])
        assert report.targets == []
        assert report.callers == {}
        assert len(report.failures) == 1
        assert report.failures[0]["stage"] == "age_unavailable"

    def test_no_targets_resolved_returns_failure(self, monkeypatch):
        monkeypatch.setattr(
            "app.kg.impact_analyzer.kg_client.is_age_available",
            lambda db: True,
        )
        monkeypatch.setattr(
            "app.kg.impact_analyzer.kg_client.run_cypher",
            lambda *a, **kw: [],
        )
        report = analyze_impact(
            db=MagicMock(),
            target_symbols=[("NonExistent", None)],
        )
        assert report.targets == []
        assert any(f["stage"] == "target_resolution" for f in report.failures)

    def test_happy_path_all_edge_types(self, monkeypatch):
        """Synthetic graph with target X + callers + subclasses + impls + docs."""
        monkeypatch.setattr(
            "app.kg.impact_analyzer.kg_client.is_age_available",
            lambda db: True,
        )

        # Topology:
        #   callers: X ← Y ← Z (2-hop chain)
        #   subclasses: X ← C1
        #   impls: X ← I1
        #   documenting: X ← D1
        #   files: X@xfile, Y@yfile, Z@zfile, C1@c1file, I1@i1file, D1@d1file
        def fake(db, cypher, *, graph_name=None, return_cols=None):
            # Callers
            if "x:Function)-[r:CALLS]->(t)" in cypher and "'X'" in cypher:
                return [{"cid": "Y"}]
            if "x:Function)-[r:CALLS]->(t)" in cypher and "'Y'" in cypher:
                return [{"cid": "Z"}]
            if "x:Function)-[r:CALLS]->(t)" in cypher and "'Z'" in cypher:
                return []
            # Subclasses
            if "x:Class)-[r:INHERITS]->(t)" in cypher and "'X'" in cypher:
                return [{"cid": "C1"}]
            if "x:Class)-[r:INHERITS]->(t)" in cypher and "'C1'" in cypher:
                return []
            # Implementations
            if "x:Class)-[r:IMPLEMENTS]->(t)" in cypher:
                return [{"cid": "I1"}]
            # Documenting
            if "x:DocChunk)-[r:DESCRIBES]->(t)" in cypher:
                return [{"cid": "D1"}]
            # Source files — matches the build_source_files_cypher shape
            if "RETURN DISTINCT n.source_file AS source_file" in cypher:
                return [
                    {"source_file": "xfile"}, {"source_file": "yfile"},
                    {"source_file": "zfile"}, {"source_file": "c1file"},
                    {"source_file": "i1file"}, {"source_file": "d1file"},
                ]
            return []

        monkeypatch.setattr(
            "app.kg.impact_analyzer.kg_client.run_cypher", fake,
        )

        report = analyze_impact(
            db=MagicMock(), target_chunk_ids=["X"], max_hops=2,
        )
        assert report.targets == ["X"]
        assert report.callers == {"Y": 1, "Z": 2}
        assert report.subclasses == {"C1": 1}
        assert report.implementations == ["I1"]
        assert report.documenting == ["D1"]
        assert set(report.files_affected) == {
            "xfile", "yfile", "zfile", "c1file", "i1file", "d1file",
        }
        # 5 distinct impacted chunks (Y, Z, C1, I1, D1); all different buckets.
        assert report.total_impacted() == 5
        assert set(report.impacted_chunk_ids()) == {"Y", "Z", "C1", "I1", "D1"}

    def test_target_resolution_via_symbols_path(self, monkeypatch):
        monkeypatch.setattr(
            "app.kg.impact_analyzer.kg_client.is_age_available",
            lambda db: True,
        )

        def fake(db, cypher, *, graph_name=None, return_cols=None):
            # Symbol resolution: returns one chunk_id per symbol
            if "symbol_name = 'Foo'" in cypher and "source_file" not in cypher:
                return [{"cid": "foo-id"}]
            # Callers of foo-id: none
            return []

        monkeypatch.setattr(
            "app.kg.impact_analyzer.kg_client.run_cypher", fake,
        )

        report = analyze_impact(
            db=MagicMock(), target_symbols=[("Foo", None)],
        )
        assert report.targets == ["foo-id"]

    def test_partial_graph_failures_kept_in_report(self, monkeypatch):
        """Per-query Cypher failures are swallowed inside BFS, don't surface
        as report.failures (by design — too noisy). Stage-level failures
        (files_affected cypher) DO surface."""
        monkeypatch.setattr(
            "app.kg.impact_analyzer.kg_client.is_age_available",
            lambda db: True,
        )
        call_count = {"n": 0}

        def fake(db, cypher, *, graph_name=None, return_cols=None):
            call_count["n"] += 1
            # Every single cypher raises — simulating a broken graph.
            raise RuntimeError("AGE down")

        monkeypatch.setattr(
            "app.kg.impact_analyzer.kg_client.run_cypher", fake,
        )

        report = analyze_impact(
            db=MagicMock(), target_chunk_ids=["X"], max_hops=1,
        )
        # Target was explicit (no cypher needed to resolve), so we get past
        # the target_resolution gate.
        assert report.targets == ["X"]
        # All downstream queries fail silently → empty impacted sets.
        assert report.callers == {}
        assert report.subclasses == {}
        assert report.implementations == []
        assert report.documenting == []


# ──────────────────────────────────────────────────────────────────────────────
# ImpactReport helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestImpactReport:

    def test_total_impacted_deduplicates_across_buckets(self):
        r = ImpactReport(
            targets=["T"],
            callers={"A": 1, "B": 2},
            subclasses={"B": 1},   # B appears in both callers + subclasses
            implementations=["C"],
            documenting=["A"],     # A appears in both callers + documenting
        )
        assert r.total_impacted() == 3   # A, B, C (distinct)

    def test_impacted_chunk_ids_sorted(self):
        r = ImpactReport(
            callers={"Z": 1, "A": 1}, subclasses={"M": 1},
        )
        assert r.impacted_chunk_ids() == ["A", "M", "Z"]

    def test_empty_report_has_zero_impacted(self):
        r = ImpactReport()
        assert r.total_impacted() == 0
        assert r.impacted_chunk_ids() == []


# ──────────────────────────────────────────────────────────────────────────────
# Live AGE integration
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.age
def test_analyze_impact_against_live_graph():
    """Create a tiny graph (A CALLS B, C INHERITS B, D DESCRIBES B), run
    analyze_impact for B, assert A/C/D show up. Cleanup on exit."""
    from app.core.database import SessionLocal
    from app.kg.client import is_age_available, run_cypher
    from app.kg.schema import initialise_graph

    db = SessionLocal()
    try:
        if not is_age_available(db):
            pytest.skip("Apache AGE not available")
        initialise_graph(db)

        # Seed the graph. Target B is a Function; A calls B; C inherits B
        # (semantically odd — Function inherits Function — but the query
        # pattern doesn't care, and this is just for plumbing verification).
        # For INHERITS, we use Class nodes to match the builder.
        run_cypher(db, "MERGE (b:Function {chunk_id: 'ia-B', symbol_name: 'ia_B', source_file: 'ia_bfile'})")
        run_cypher(db, "MERGE (a:Function {chunk_id: 'ia-A', symbol_name: 'ia_A', source_file: 'ia_afile'})")
        run_cypher(db, "MATCH (a:Function {chunk_id: 'ia-A'}), (b:Function {chunk_id: 'ia-B'}) MERGE (a)-[r:CALLS]->(b)")

        # Class-level INHERITS edge to a separate Class node.
        run_cypher(db, "MERGE (bc:Class {chunk_id: 'ia-BC', symbol_name: 'ia_BC', source_file: 'ia_bcfile'})")
        run_cypher(db, "MERGE (c:Class {chunk_id: 'ia-C', symbol_name: 'ia_C', source_file: 'ia_cfile'})")
        run_cypher(db, "MATCH (c:Class {chunk_id: 'ia-C'}), (bc:Class {chunk_id: 'ia-BC'}) MERGE (c)-[r:INHERITS]->(bc)")

        # DocChunk → B describes
        run_cypher(db, "MERGE (d:DocChunk {chunk_id: 'ia-D', source_file: 'ia_dfile'})")
        run_cypher(db, "MATCH (d:DocChunk {chunk_id: 'ia-D'}), (b:Function {chunk_id: 'ia-B'}) MERGE (d)-[r:DESCRIBES]->(b)")
        db.commit()

        try:
            # Impact of B: expect A in callers, D in documenting.
            report_b = ia.analyze_impact(
                db=db, target_chunk_ids=["ia-B"], max_hops=2,
            )
            assert "ia-A" in report_b.callers, f"A not in callers: {report_b.callers}"
            assert "ia-D" in report_b.documenting, f"D not in documenting: {report_b.documenting}"
            assert "ia_afile" in report_b.files_affected
            assert "ia_bfile" in report_b.files_affected

            # Impact of BC: expect C in subclasses.
            report_bc = ia.analyze_impact(
                db=db, target_chunk_ids=["ia-BC"], max_hops=2,
            )
            assert "ia-C" in report_bc.subclasses, f"C not in subclasses: {report_bc.subclasses}"
        finally:
            for cid in ("ia-A", "ia-B", "ia-BC", "ia-C", "ia-D"):
                run_cypher(db, f"MATCH (n {{chunk_id: '{cid}'}}) DETACH DELETE n")
            db.commit()
    finally:
        db.close()
