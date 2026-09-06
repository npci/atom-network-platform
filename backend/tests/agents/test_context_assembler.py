# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ContextPack doc-section parsing (§4/§8). DB-backed assembly is in the S8 smoke."""
from app.agents import context_assembler
from app.agents.context_assembler import doc_sections, _impact_files

BRD = """# BRD

## 1. Overview
The refund status feature.

## 10. Regulatory & Compliance
| code | meaning |
| RB01 | declined |
"""


def test_doc_sections_keeps_every_section_including_regulatory():
    secs = doc_sections(BRD)
    # the legacy compressor dropped this section wholesale — it must survive
    assert "10. Regulatory & Compliance" in secs
    assert "RB01" in secs["10. Regulatory & Compliance"]
    assert "1. Overview" in secs and "refund status" in secs["1. Overview"]


def test_doc_sections_skips_empty_bodies():
    # "# BRD" has no body before the first subsection → not emitted as a section
    assert "BRD" not in doc_sections(BRD)


def test_doc_sections_empty_input():
    assert doc_sections("") == {} and doc_sections(None) == {}


def test_doc_sections_repeated_headings_are_not_lost():
    # Two "## Description" sections must both survive (no silent overwrite).
    doc = "## Description\nfirst body\n\n## Other\nx\n\n## Description\nsecond body\n"
    secs = doc_sections(doc)
    bodies = " ".join(secs.values())
    assert "first body" in bodies and "second body" in bodies
    assert "Description" in secs and "Description (2)" in secs


def test_doc_sections_no_headings_is_overview():
    secs = doc_sections("just some prose with no headings at all")
    assert list(secs) == ["Overview"] and "prose" in secs["Overview"]


def _patch_sql_backend(monkeypatch, *, seeds_out, self_chunks, caller_chunks, capture=None):
    """Wire the "sql" impact backend: stub the seed extractor + the two
    sql_graph queries so no live DB/AGE is needed."""
    monkeypatch.setattr(context_assembler.settings, "impact_backend", "sql")

    def _seeds(desc, **kw):
        if capture is not None:
            capture["desc"] = desc
        return seeds_out

    monkeypatch.setattr("app.rag.graph_retriever._extract_query_seeds", _seeds)
    monkeypatch.setattr("app.kg.sql_graph.find_chunks_by_symbol_name", lambda db, syms: self_chunks)
    monkeypatch.setattr("app.kg.sql_graph.inbound_callers", lambda db, syms: caller_chunks)


def test_impact_files_uses_intent_when_no_brd_tsd(monkeypatch):
    # Quick-start flow: BRD/TSD are None, so impact analysis must run on the INTENT
    # (first non-empty description) — otherwise the LLM gets no blast-radius hints.
    cap = {}
    _patch_sql_backend(
        monkeypatch, seeds_out=["RefundRequest"],
        self_chunks=[{"source_file": "network-parent/api-gateway/A.java"}],
        caller_chunks=[{"source_file": "psp/B.java"}],
        capture=cap,
    )
    files = _impact_files(None, ["add a reasonCode field to RefundRequest", None, None])
    assert cap["desc"].startswith("add a reasonCode field")
    assert files == ["network-parent/api-gateway/A.java", "psp/B.java"]


def test_impact_files_sql_dedups_and_caps(monkeypatch):
    # Union of seed-owned files + inbound callers, de-duplicated, capped at 25.
    self_chunks = [{"source_file": "A.java"}, {"source_file": "A.java"}]  # dup
    caller_chunks = [{"source_file": f"C{i}.java"} for i in range(40)] + [{"source_file": None}]
    _patch_sql_backend(monkeypatch, seeds_out=["Foo"], self_chunks=self_chunks, caller_chunks=caller_chunks)
    files = _impact_files(None, ["change the Foo handler behaviour", None, None])
    assert files[0] == "A.java"              # dedup kept one
    assert len(files) == 25                  # capped
    assert None not in files                 # null source_file dropped


def test_impact_files_sql_no_seeds_returns_empty(monkeypatch):
    _patch_sql_backend(monkeypatch, seeds_out=[], self_chunks=[{"source_file": "x"}], caller_chunks=[])
    assert _impact_files(None, ["some description with no symbol tokens", None, None]) == []


def test_impact_files_sql_failopen_on_error(monkeypatch):
    monkeypatch.setattr(context_assembler.settings, "impact_backend", "sql")
    def _boom(*a, **k):
        raise RuntimeError("document_chunks gone")
    monkeypatch.setattr("app.rag.graph_retriever._extract_query_seeds", _boom)
    assert _impact_files(None, ["modify the RefundRequest schema", None, None]) == []


def test_impact_files_age_backend_still_routes_to_analyze_impact(monkeypatch):
    # The legacy AGE path stays reachable when explicitly selected.
    monkeypatch.setattr(context_assembler.settings, "impact_backend", "age")

    class _Rep:
        files_affected = ["network-parent/api-gateway/A.java", "psp/B.java"]

    monkeypatch.setattr("app.kg.impact_analyzer.analyze_impact",
                        lambda db, change_description: _Rep())
    files = _impact_files(None, ["add a reasonCode field to RefundRequest", None, None])
    assert files == ["network-parent/api-gateway/A.java", "psp/B.java"]
