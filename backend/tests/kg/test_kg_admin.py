# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the KG admin endpoints (sub-slice 20a).

Handlers are tested by direct invocation (bypassing the HTTP + auth layer)
with stubbed kg_client / kg_schema / ingest_from_db / analyze_impact and a
minimal fake DB session — matches the existing test style for this codebase.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api import kg_admin


# ──────────────────────────────────────────────────────────────────────────────
# Fake session that supports `db.scalar(...)` for count queries
# ──────────────────────────────────────────────────────────────────────────────

class _FakeSessionWithCounts:
    """Minimal session whose `.scalar(...)` returns a round-robin of values.

    Slightly ugly but lets us stub `chunks_total` and `links_total` without
    building a full SQLAlchemy-compatible double.
    """
    def __init__(self, scalars_in_order: list[int]):
        self._scalars = list(scalars_in_order)
        self._idx = 0

    def scalar(self, stmt):
        val = self._scalars[self._idx] if self._idx < len(self._scalars) else 0
        self._idx += 1
        return val

    def rollback(self):
        pass


# ──────────────────────────────────────────────────────────────────────────────
# _count_nodes_per_label
# ──────────────────────────────────────────────────────────────────────────────

class TestCountNodesPerLabel:

    def test_per_label_counts_aggregated(self, monkeypatch):
        """One row per label, each returning an int count."""
        def fake_run_cypher(db, cypher, *, graph_name=None, return_cols=None):
            # Extract label from "MATCH (n:Label)" — reflect the count back.
            import re
            m = re.search(r"MATCH \(n:(\w+)\)", cypher)
            label = m.group(1) if m else "?"
            # Deterministic count per label: length of label name.
            return [{"cid": len(label)}]
        monkeypatch.setattr(kg_admin.kg_client, "run_cypher", fake_run_cypher)

        out = kg_admin._count_nodes_per_label(MagicMock())

        # Every NODE_LABELS label should have an entry.
        assert set(out.keys()) == set(kg_admin.kg_schema.NODE_LABELS)
        assert out["DocChunk"] == len("DocChunk")
        assert out["Function"] == len("Function")

    def test_failing_label_doesnt_abort_others(self, monkeypatch):
        def fake(db, cypher, *, graph_name=None, return_cols=None):
            if "Class" in cypher:
                raise RuntimeError("AGE hiccup on Class")
            return [{"cid": 7}]
        monkeypatch.setattr(kg_admin.kg_client, "run_cypher", fake)

        out = kg_admin._count_nodes_per_label(MagicMock())
        assert "Class" not in out
        assert out["DocChunk"] == 7

    def test_empty_rows_treated_as_missing(self, monkeypatch):
        monkeypatch.setattr(
            kg_admin.kg_client, "run_cypher",
            lambda db, c, **kw: [],
        )
        out = kg_admin._count_nodes_per_label(MagicMock())
        assert out == {}

    def test_string_count_coerced_to_int(self, monkeypatch):
        """AGE sometimes stringifies numerics — we coerce via int(str(...))."""
        monkeypatch.setattr(
            kg_admin.kg_client, "run_cypher",
            lambda db, c, **kw: [{"cid": "42"}],
        )
        out = kg_admin._count_nodes_per_label(MagicMock())
        assert all(v == 42 for v in out.values())


# ──────────────────────────────────────────────────────────────────────────────
# kg_status
# ──────────────────────────────────────────────────────────────────────────────

class TestKgStatus:

    def test_reports_age_available_and_counts(self, monkeypatch):
        monkeypatch.setattr(kg_admin.kg_client, "is_age_available", lambda db: True)
        monkeypatch.setattr(
            kg_admin, "_count_nodes_per_label", lambda db: {"DocChunk": 10},
        )

        db = _FakeSessionWithCounts([100, 25])
        out = kg_admin.kg_status(db, _=MagicMock())

        assert out.age_available is True
        assert out.chunks_total == 100
        assert out.doc_links_total == 25
        assert out.node_counts == {"DocChunk": 10}
        assert out.graph_name    # non-empty from settings

    def test_age_unavailable_skips_node_counts_but_still_reports_rag(self, monkeypatch):
        monkeypatch.setattr(kg_admin.kg_client, "is_age_available", lambda db: False)
        # _count_nodes_per_label should NOT be called — if it is, the test
        # fails via the stub.
        monkeypatch.setattr(
            kg_admin, "_count_nodes_per_label",
            lambda db: pytest.fail("should not be called when AGE unavailable"),
        )

        db = _FakeSessionWithCounts([50, 3])
        out = kg_admin.kg_status(db, _=MagicMock())

        assert out.age_available is False
        assert out.chunks_total == 50
        assert out.doc_links_total == 3
        assert out.node_counts == {}


# ──────────────────────────────────────────────────────────────────────────────
# kg_initialise
# ──────────────────────────────────────────────────────────────────────────────

class TestKgInitialise:

    def test_age_unavailable_raises_503(self, monkeypatch):
        monkeypatch.setattr(kg_admin.kg_client, "is_age_available", lambda db: False)
        with pytest.raises(HTTPException) as exc:
            kg_admin.kg_initialise(MagicMock(), _=MagicMock())
        assert exc.value.status_code == 503

    def test_delegates_to_schema_initialise(self, monkeypatch):
        called = {"n": 0}
        def fake(db, **kw):
            called["n"] += 1
            return {
                "graph_name": "npci_kg",
                "vlabels_created": ["Document"],
                "elabels_created": [],
                "vlabels_skipped":  ["DocChunk"],
                "elabels_skipped":  ["CALLS"],
                "failures": [],
            }
        monkeypatch.setattr(kg_admin.kg_client, "is_age_available", lambda db: True)
        monkeypatch.setattr(kg_admin.kg_schema, "initialise_graph", fake)

        out = kg_admin.kg_initialise(MagicMock(), _=MagicMock())
        assert called["n"] == 1
        assert out.vlabels_created == ["Document"]
        assert out.vlabels_skipped == ["DocChunk"]
        assert out.elabels_skipped == ["CALLS"]
        assert out.failures == []


# ──────────────────────────────────────────────────────────────────────────────
# kg_ingest
# ──────────────────────────────────────────────────────────────────────────────

class TestKgIngest:

    def test_age_unavailable_raises_503(self, monkeypatch):
        monkeypatch.setattr(kg_admin.kg_client, "is_age_available", lambda db: False)
        with pytest.raises(HTTPException) as exc:
            kg_admin.kg_ingest(MagicMock(), _=MagicMock())
        assert exc.value.status_code == 503

    def test_delegates_to_ingest_from_db(self, monkeypatch):
        # Use the real IngestReport so total_successes / total_failures behave.
        from app.kg.ingest_from_rag import IngestReport
        report = IngestReport(counts={"document": 5, "class": 2}, failures=[{"kind": "calls", "error": "x", "cypher_excerpt": ""}])

        monkeypatch.setattr(kg_admin.kg_client, "is_age_available", lambda db: True)
        monkeypatch.setattr(kg_admin, "ingest_from_db", lambda db: report)

        out = kg_admin.kg_ingest(MagicMock(), _=MagicMock())

        assert out.counts == {"document": 5, "class": 2}
        assert out.successes == 7
        assert out.failures_count == 1
        assert len(out.failures) == 1


# ──────────────────────────────────────────────────────────────────────────────
# kg_impact
# ──────────────────────────────────────────────────────────────────────────────

class TestKgImpact:

    def test_requires_at_least_one_target_input(self):
        body = kg_admin.KgImpactRequest()
        with pytest.raises(HTTPException) as exc:
            kg_admin.kg_impact(body, MagicMock(), _=MagicMock())
        assert exc.value.status_code == 400

    def test_age_unavailable_raises_503(self, monkeypatch):
        monkeypatch.setattr(kg_admin.kg_client, "is_age_available", lambda db: False)
        body = kg_admin.KgImpactRequest(target_chunk_ids=["X"])
        with pytest.raises(HTTPException) as exc:
            kg_admin.kg_impact(body, MagicMock(), _=MagicMock())
        assert exc.value.status_code == 503

    def test_happy_path_delegates_to_analyze_impact(self, monkeypatch):
        from app.kg.impact_analyzer import ImpactReport
        report = ImpactReport(
            targets=["X"], callers={"Y": 1}, subclasses={"C": 1},
            implementations=["I"], documenting=["D"],
            files_affected=["xfile", "yfile"],
        )
        captured = {}
        def fake_analyze(**kw):
            captured.update(kw)
            return report

        monkeypatch.setattr(kg_admin.kg_client, "is_age_available", lambda db: True)
        monkeypatch.setattr(kg_admin, "analyze_impact", fake_analyze)

        body = kg_admin.KgImpactRequest(
            target_chunk_ids=["X"], max_hops=3,
        )
        out = kg_admin.kg_impact(body, MagicMock(), _=MagicMock())

        assert captured["target_chunk_ids"] == ["X"]
        assert captured["max_hops"] == 3
        assert out.targets == ["X"]
        assert out.callers == {"Y": 1}
        assert out.total_impacted == 4    # Y, C, I, D

    def test_description_path_flows_through(self, monkeypatch):
        from app.kg.impact_analyzer import ImpactReport
        captured = {}
        def fake_analyze(**kw):
            captured.update(kw)
            return ImpactReport(targets=["t1"])
        monkeypatch.setattr(kg_admin.kg_client, "is_age_available", lambda db: True)
        monkeypatch.setattr(kg_admin, "analyze_impact", fake_analyze)

        body = kg_admin.KgImpactRequest(change_description="update FooService")
        out = kg_admin.kg_impact(body, MagicMock(), _=MagicMock())

        assert captured["change_description"] == "update FooService"
        assert out.targets == ["t1"]
