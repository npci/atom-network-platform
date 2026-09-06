# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Targeted divergence repair.

`edit_divergent_sections` re-writes ONLY the sections that mention a flagged item
(the plan-consistency gate's blocker item names), leaving every other section
byte-identical — and falls back to a full-document edit when nothing matches, so a
flagged blocker is never silently left in place.
"""
import json

import app.docgen.tools.document_editor as ED


def _setup(monkeypatch, tmp_path, sections):
    """Wire edit_divergent_sections onto in-memory artifacts + a marker writer.

    Returns the list that records which section_keys got regenerated.
    """
    plan = {
        "doc_type": "BRD",
        "document_meta": {"audience": "a", "desired_outcome": "o"},
        "sections": [
            {"section_key": s["section_key"], "heading": s["section_heading"],
             "render_style": "body", "level": 1, "content_instructions": "ci"}
            for s in sections
        ],
        "diagram_specs": [],
    }

    def _load(job_id, name):
        if name == "document_plan.json":
            return plan
        if name == "generated_sections.json":
            return [dict(s) for s in sections]
        raise FileNotFoundError(name)

    written: list[str] = []

    def _write_section(llm, section, rag, doc_type, audience="", desired_outcome=""):
        written.append(section.get("section_key"))
        return {"body": f"REWRITTEN::{section.get('section_key')}"}

    monkeypatch.setattr(ED, "_load_artifact", _load)
    monkeypatch.setattr("app.docgen.agents.pipeline._write_section", _write_section)
    monkeypatch.setattr("app.docgen.agents.pipeline._make_llm_json", lambda: object())
    monkeypatch.setattr("app.docgen.agents.pipeline._provider", lambda: "openai_compat")
    monkeypatch.setattr("app.docgen.tools.docx_builder.assemble_document",
                        lambda *a, **k: a[2])          # echo the output_path
    monkeypatch.setattr("app.docgen.plan_store.artifact_dir", lambda job_id: tmp_path)
    return written


def test_edits_only_divergent_section(monkeypatch, tmp_path):
    sections = [
        {"section_key": "s0", "section_heading": "Overview", "body": "no divergence here"},
        {"section_key": "s1", "section_heading": "Messages", "body": "introduces ReqSetSpendLimit pair"},
        {"section_key": "s2", "section_heading": "Ops", "body": "unrelated content"},
    ]
    written = _setup(monkeypatch, tmp_path, sections)

    ED.edit_divergent_sections("job1", "remove ReqSetSpendLimit", ["ReqSetSpendLimit"])

    assert written == ["s1"]                                  # only the divergent section regenerated
    saved = {s["section_key"]: s for s in
             json.loads((tmp_path / "generated_sections.json").read_text())}
    assert saved["s1"]["body"] == "REWRITTEN::s1"             # divergent section rewritten
    assert saved["s0"]["body"] == "no divergence here"        # others preserved verbatim
    assert saved["s2"]["body"] == "unrelated content"


def test_matches_item_in_a_table_cell_not_just_prose(monkeypatch, tmp_path):
    # The needle is matched against the section's full JSON, so it's caught wherever
    # it lives — here, nested inside a table structure rather than a prose field.
    sections = [
        {"section_key": "s0", "section_heading": "Intro", "body": "clean"},
        {"section_key": "s1", "section_heading": "Schema",
         "table": {"rows": [["field", "ReqTransfer.txnAmount"]]}},
    ]
    written = _setup(monkeypatch, tmp_path, sections)

    ED.edit_divergent_sections("job1", "drop the field", ["ReqTransfer.txnAmount"])

    assert written == ["s1"]


def test_falls_back_to_full_document_when_no_match(monkeypatch, tmp_path):
    sections = [{"section_key": "s0", "section_heading": "Overview", "body": "nothing flagged"}]
    written = _setup(monkeypatch, tmp_path, sections)
    called = {"full": False}

    def _full(job_id, instruction, output_suffix="_edited", progress=None):
        called["full"] = True
        return "full.docx"

    monkeypatch.setattr(ED, "edit_full_document", _full)

    ED.edit_divergent_sections("job1", "fix it", ["ItemNotPresentAnywhere"])

    assert called["full"] is True                             # no match → full-document fallback
    assert written == []                                      # targeted writer never invoked


def test_empty_items_falls_back_to_full_document(monkeypatch, tmp_path):
    sections = [{"section_key": "s0", "section_heading": "Overview", "body": "x"}]
    written = _setup(monkeypatch, tmp_path, sections)
    called = {"full": False}
    monkeypatch.setattr(ED, "edit_full_document",
                        lambda *a, **k: called.__setitem__("full", True) or "full.docx")

    ED.edit_divergent_sections("job1", "fix it", [])

    assert called["full"] is True
    assert written == []
