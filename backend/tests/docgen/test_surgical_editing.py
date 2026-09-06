# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Surgical (patch-based) document editing — M1–M4.

The guarantee under test: every content block carries a stable id (block_ids), a
diff is computed by id, and a patch touches ONLY the declared blocks — everything
else stays byte-identical. This is what the old whole-section regeneration only
*pleaded* for. Also exercises the M4 orchestrator with the patch-planner LLM
mocked (no network).
"""
import asyncio
import copy
import json

import pytest

from app.docgen.block_ids import ensure_document_ids
from app.docgen.section_diff import diff_sections
from app.docgen.patch import apply_ops, apply_patch, PatchError
import app.docgen.tools.surgical_edit as SE


def _sections():
    return [
        {"section_heading": "Settlement and Reconciliation",
         "paragraphs": ["Limit applies daily.", "Settlement occurs on T+1.", "Refunds reconciled nightly."],
         "bullet_points": ["Bullet one.", "Bullet two."],
         "table_data": {"headers": ["Flow", "Window"], "rows": [["Debit", "T+1"], ["Refund", "T+2"]]}},
        {"section_heading": "Overview", "paragraphs": ["Describes the feature."]},
    ]


# ── M1: block ids ───────────────────────────────────────────────────────────
def test_ensure_ids_assigns_and_aligns():
    s0 = ensure_document_ids(_sections())[0]
    assert s0["section_key"] == "settlement_and_reconciliation"
    assert len(s0["block_ids"]["paragraphs"]) == 3
    assert len(s0["block_ids"]["bullet_points"]) == 2
    assert s0["block_ids"]["table"]["table_id"]
    assert len(s0["block_ids"]["table"]["row_ids"]) == 2


def test_ensure_ids_idempotent():
    secs = ensure_document_ids(_sections())
    snap = json.dumps(secs, sort_keys=True)
    ensure_document_ids(secs)
    assert json.dumps(secs, sort_keys=True) == snap


def test_unique_section_keys_on_duplicate_headings():
    secs = ensure_document_ids([{"section_heading": "X", "paragraphs": []},
                                {"section_heading": "X", "paragraphs": []}])
    assert secs[0]["section_key"] != secs[1]["section_key"]


# ── M2: diff ────────────────────────────────────────────────────────────────
def test_diff_isolates_changed_block():
    secs = ensure_document_ids(_sections())
    after = copy.deepcopy(secs)
    pid = secs[0]["block_ids"]["paragraphs"][1]
    after[0]["paragraphs"][1] = "Settlement occurs on T+0."
    d = diff_sections(secs[0], after[0])
    assert d.changed == {pid} and not d.added and not d.removed


# ── M3: patch ───────────────────────────────────────────────────────────────
def test_find_replace_is_surgical_and_gated():
    secs = ensure_document_ids(_sections())
    pid = secs[0]["block_ids"]["paragraphs"][1]
    new, rep = apply_patch(secs, [{"op": "find_replace", "block_id": pid, "find": "T+1", "replace": "T+0"}])
    assert new[0]["paragraphs"][1] == "Settlement occurs on T+0."
    assert rep["violations"] == {}
    assert new[0]["paragraphs"][0] == secs[0]["paragraphs"][0]   # verbatim
    assert new[0]["paragraphs"][2] == secs[0]["paragraphs"][2]   # verbatim
    assert new[1] == secs[1]                                     # other section verbatim
    assert secs[0]["paragraphs"][1] == "Settlement occurs on T+1."  # original not mutated


def test_set_cell_by_header_name():
    secs = ensure_document_ids(_sections())
    t = secs[0]["block_ids"]["table"]
    new, rep = apply_patch(secs, [{"op": "set_cell", "table_id": t["table_id"],
                                   "row_id": t["row_ids"][0], "column": "Window", "value": "T+0"}])
    assert new[0]["table_data"]["rows"][0] == ["Debit", "T+0"]
    assert new[0]["table_data"]["rows"][1] == ["Refund", "T+2"]   # untouched
    assert rep["violations"] == {}


def test_delete_and_insert_keep_ids_aligned():
    secs = ensure_document_ids(_sections())
    bid = secs[0]["block_ids"]["bullet_points"][0]
    new, rep = apply_patch(secs, [{"op": "delete_block", "block_id": bid}])
    assert new[0]["bullet_points"] == ["Bullet two."]
    assert len(new[0]["block_ids"]["bullet_points"]) == 1
    assert rep["violations"] == {}

    new2, rep2 = apply_patch(secs, [{"op": "insert_block", "section_key": "overview",
                                     "field": "paragraphs", "after": None, "text": "Added."}])
    assert new2[1]["paragraphs"][-1] == "Added."
    assert len(new2[1]["block_ids"]["paragraphs"]) == 2
    assert rep2["violations"] == {}


def test_dangling_op_raises():
    secs = ensure_document_ids(_sections())
    with pytest.raises(PatchError):
        apply_ops(secs, [{"op": "replace_text", "block_id": "nope", "text": "x"}])


def test_insert_row_appends_and_aligns():
    secs = ensure_document_ids(_sections())
    t = secs[0]["block_ids"]["table"]
    new, rep = apply_patch(secs, [{"op": "insert_row", "table_id": t["table_id"],
                                   "after": None, "cells": ["Chargeback", "T+3"]}])
    assert new[0]["table_data"]["rows"][-1] == ["Chargeback", "T+3"]
    assert len(new[0]["block_ids"]["table"]["row_ids"]) == 3
    assert new[0]["table_data"]["rows"][0] == ["Debit", "T+1"]   # existing rows verbatim
    assert rep["violations"] == {}


# ── Defaults + output quality ───────────────────────────────────────────────
def test_surgical_edit_flag_defaults_on():
    import app.docgen.config as cfg
    assert cfg.settings.surgical_edit is True   # new path is the default; legacy is opt-out


def test_md_cell_escapes_pipe_and_newline():
    from app.services.docgen_runner import _md_cell
    assert _md_cell("a|b") == "a\\|b"
    assert _md_cell("line1\nline2") == "line1<br>line2"


# ── M4: orchestrator with the planner LLM mocked ────────────────────────────
def test_orchestrator_applies_planner_ops(monkeypatch, tmp_path):
    secs = _sections()
    # pin ids so the mocked planner can target a known block by id
    secs[0]["block_ids"] = {"paragraphs": ["p_a", "p_b", "p_c"],
                            "bullet_points": ["bl_a", "bl_b"],
                            "table": {"table_id": "t_x", "row_ids": ["r_a", "r_b"]}}
    plan = {"doc_type": "BRD", "title": "T", "diagram_specs": [], "sections": [
        {"section_key": "settlement_and_reconciliation", "heading": "Settlement and Reconciliation"},
        {"section_key": "overview", "heading": "Overview"}]}

    def _load(job_id, name):
        if name == "document_plan.json":
            return plan
        if name == "generated_sections.json":
            return [dict(s) for s in secs]
        raise FileNotFoundError(name)

    async def _fake_llm(*a, **kw):
        return ({"ops": [{"op": "find_replace", "block_id": "p_b",
                                    "find": "T+1", "replace": "T+0"}]})

    monkeypatch.setattr(SE, "_load", _load)
    monkeypatch.setattr(SE, "call_llm_structured", _fake_llm)
    monkeypatch.setattr("app.docgen.tools.docx_builder.assemble_document", lambda *a, **k: a[2])
    monkeypatch.setattr("app.docgen.plan_store.artifact_dir", lambda job_id: tmp_path)

    asyncio.run(SE.surgical_edit_document("job1", "change the settlement window to T+0"))

    saved = json.loads((tmp_path / "generated_sections.json").read_text())
    assert saved[0]["paragraphs"][1] == "Settlement occurs on T+0."   # the edit landed
    assert saved[0]["paragraphs"][0] == "Limit applies daily."        # untouched
    assert saved[1]["paragraphs"] == ["Describes the feature."]       # other section untouched


# ── M5: consistency repair by block id ──────────────────────────────────────
def test_locate_items_maps_names_to_block_ids():
    from app.docgen.tools.surgical_edit import _locate_items
    secs = ensure_document_ids(_sections())
    secs[0]["paragraphs"][1] = "Introduce ReqSetSpendLimit before settlement."
    pid = secs[0]["block_ids"]["paragraphs"][1]
    hits = _locate_items(secs, ["ReqSetSpendLimit", "NonExistent"], None)
    assert hits.get("ReqSetSpendLimit") == [pid]
    assert "NonExistent" not in hits


def test_orchestrator_consistency_repair_removes_divergent_block(monkeypatch, tmp_path):
    secs = _sections()
    secs[0]["paragraphs"][1] = "Introduce ReqSetSpendLimit before settlement."
    secs[0]["block_ids"] = {"paragraphs": ["p_a", "p_b", "p_c"],
                            "bullet_points": ["bl_a", "bl_b"],
                            "table": {"table_id": "t_x", "row_ids": ["r_a", "r_b"]}}
    plan = {"doc_type": "BRD", "title": "T", "diagram_specs": [], "sections": [
        {"section_key": "settlement_and_reconciliation", "heading": "Settlement and Reconciliation"},
        {"section_key": "overview", "heading": "Overview"}]}
    captured: dict = {}

    def _load(job_id, name):
        if name == "document_plan.json":
            return plan
        if name == "generated_sections.json":
            return [dict(s) for s in secs]
        raise FileNotFoundError(name)

    async def _fake_llm(*a, **kw):
        captured["prompt"] = a[1]
        return ({"ops": [{"op": "delete_block", "block_id": "p_b"}]})

    monkeypatch.setattr(SE, "_load", _load)
    monkeypatch.setattr(SE, "call_llm_structured", _fake_llm)
    monkeypatch.setattr("app.docgen.tools.docx_builder.assemble_document", lambda *a, **k: a[2])
    monkeypatch.setattr("app.docgen.plan_store.artifact_dir", lambda job_id: tmp_path)

    asyncio.run(SE.surgical_edit_document("job1", "remove ReqSetSpendLimit — it is not in the plan",
                                          focus_items=["ReqSetSpendLimit"]))

    saved = json.loads((tmp_path / "generated_sections.json").read_text())
    assert saved[0]["paragraphs"] == ["Limit applies daily.", "Refunds reconciled nightly."]
    assert "p_b" not in saved[0]["block_ids"]["paragraphs"]
    # the focus hint reached the planner with the item resolved to its block id
    assert "ReqSetSpendLimit" in captured["prompt"] and "p_b" in captured["prompt"]


# ── Task #1: surgical diagram editing ───────────────────────────────────────
def test_surgical_diagram_edit_updates_source(monkeypatch):
    import shutil
    from app.docgen.plan_store import artifact_dir

    job_id = "SURG_DIAG_TMP"
    d = artifact_dir(job_id)   # real dir under settings.output_dir; cleaned up below
    try:
        plan = {"doc_type": "BRD", "title": "T",
                "diagram_specs": [{"diagram_id": "d0", "diagram_type": "sequence", "description": "x"}],
                "sections": [{"section_key": "overview", "heading": "Overview"}]}
        sections = [{"section_key": "overview", "section_heading": "Overview",
                     "paragraphs": ["p."], "block_ids": {"paragraphs": ["p_a"]}}]
        dsources = {"d0": {"source_type": "mermaid", "source": "sequenceDiagram\n A->>B: hi"}}
        (d / "document_plan.json").write_text(json.dumps(plan))
        (d / "generated_sections.json").write_text(json.dumps(sections))
        (d / "generated_diagram_sources.json").write_text(json.dumps(dsources))
        (d / "generated_diagrams.json").write_text(json.dumps({"d0": str(d / "d0.png")}))

        async def _fake_llm(*a, **kw):
            return ({"ops": [{"op": "replace_diagram_source", "diagram_id": "d0",
                                        "source": "sequenceDiagram\n A->>B: updated flow"}]})

        monkeypatch.setattr(SE, "call_llm_structured", _fake_llm)
        monkeypatch.setattr(SE, "_rerender_diagram",
                            lambda job, did, st, src, dm: str(artifact_dir(job) / f"{did}.png"))
        monkeypatch.setattr("app.docgen.tools.docx_builder.assemble_document", lambda *a, **k: a[2])

        asyncio.run(SE.surgical_edit_document(job_id, "update the diagram to show the new flow"))

        saved_src = json.loads((d / "generated_diagram_sources.json").read_text())
        assert "updated flow" in saved_src["d0"]["source"]
        assert saved_src["d0"]["source_type"] == "mermaid"       # engine preserved
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_diagram_edit_render_failure_does_not_persist_source(monkeypatch):
    # H2 regression: when re-render fails (None), the NEW source must NOT be
    # persisted — otherwise generated_diagram_sources.json claims the new source
    # while the embedded PNG (from generated_diagrams.json) stays old = silent divergence.
    import shutil
    from app.docgen.plan_store import artifact_dir

    job_id = "SURG_DIAG_FAIL"
    d = artifact_dir(job_id)
    try:
        plan = {"doc_type": "BRD", "title": "T",
                "diagram_specs": [{"diagram_id": "d0", "diagram_type": "sequence", "description": "x"}],
                "sections": [{"section_key": "overview", "heading": "Overview"}]}
        sections = [{"section_key": "overview", "section_heading": "Overview",
                     "paragraphs": ["p."], "block_ids": {"paragraphs": ["p_a"]}}]
        old_src = "sequenceDiagram\n A->>B: original"
        dsources = {"d0": {"source_type": "mermaid", "source": old_src}}
        (d / "document_plan.json").write_text(json.dumps(plan))
        (d / "generated_sections.json").write_text(json.dumps(sections))
        (d / "generated_diagram_sources.json").write_text(json.dumps(dsources))
        (d / "generated_diagrams.json").write_text(json.dumps({"d0": str(d / "d0.png")}))

        async def _fake_llm(*a, **kw):
            return ({"ops": [{"op": "replace_diagram_source", "diagram_id": "d0",
                                        "source": "sequenceDiagram\n A->>B: updated"}]})

        monkeypatch.setattr(SE, "call_llm_structured", _fake_llm)
        monkeypatch.setattr(SE, "_rerender_diagram", lambda *a, **k: None)   # render fails
        monkeypatch.setattr("app.docgen.tools.docx_builder.assemble_document", lambda *a, **k: a[2])

        asyncio.run(SE.surgical_edit_document(job_id, "update the diagram"))

        saved_src = json.loads((d / "generated_diagram_sources.json").read_text())
        assert saved_src["d0"]["source"] == old_src   # NOT overwritten with the un-rendered new source
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_rerender_delegates_to_generate_diagram_with_fallback(monkeypatch, tmp_path):
    # M1: _rerender_diagram must route through generate_diagram (which owns the
    # mermaid→plantuml→pillow fallback), not a strict single-engine dispatch.
    captured = {}

    def _fake_generate_diagram(spec, dtype, out_path):
        captured["spec"] = spec
        captured["dtype"] = dtype
        return out_path

    monkeypatch.setattr("app.docgen.plan_store.artifact_dir", lambda job: tmp_path)
    monkeypatch.setattr("app.docgen.tools.diagram_generator.generate_diagram", _fake_generate_diagram)

    out = SE._rerender_diagram("job", "d0", "mermaid", "sequenceDiagram\n A->>B: x", {"d0": "sequence"})
    assert out == str(tmp_path / "d0.png")
    assert captured["spec"] == {"mermaid_source": "sequenceDiagram\n A->>B: x"}   # mermaid wrapped for fallback
    assert captured["dtype"] == "sequence"

    SE._rerender_diagram("job", "d1", "plantuml", "@startuml\nA->B\n@enduml", {})
    assert captured["spec"] == {"plantuml_source": "@startuml\nA->B\n@enduml"}
