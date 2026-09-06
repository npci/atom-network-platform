# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""edit_document_section must resolve the current content AND the write-back slot by
section_key — not by plan index. generated_sections.json can be in a different order
than the plan; a plain index would feed the wrong section as the "reproduce verbatim"
baseline and overwrite the wrong slot (cross-section data loss).
"""
import json

import app.docgen.tools.document_editor as ED


def _setup_reordered(monkeypatch, tmp_path):
    # Plan order: [Alpha, Bravo]. Artifact (generated_sections.json) order: [Bravo, Alpha] — REVERSED.
    plan = {
        "doc_type": "BRD",
        "document_meta": {"audience": "a", "desired_outcome": "o"},
        "sections": [
            {"section_key": "kA", "heading": "Alpha", "render_style": "body", "level": 1, "content_instructions": "ciA"},
            {"section_key": "kB", "heading": "Bravo", "render_style": "body", "level": 1, "content_instructions": "ciB"},
        ],
        "diagram_specs": [],
    }
    artifact = [
        {"section_key": "kB", "section_heading": "Bravo", "body": "BODY_B"},
        {"section_key": "kA", "section_heading": "Alpha", "body": "BODY_A"},
    ]
    seen: dict = {}

    def _load(job_id, name):
        if name == "document_plan.json":
            return plan
        if name == "generated_sections.json":
            return [dict(s) for s in artifact]
        raise FileNotFoundError(name)

    def _write_section(llm, section, rag, doc_type, audience="", desired_outcome=""):
        seen["instructions"] = section.get("content_instructions", "")
        return {"body": "REWRITTEN_A"}

    monkeypatch.setattr(ED, "_load_artifact", _load)
    monkeypatch.setattr("app.docgen.agents.pipeline._write_section", _write_section)
    monkeypatch.setattr("app.docgen.agents.pipeline._make_llm_json", lambda: object())
    monkeypatch.setattr("app.docgen.tools.docx_builder.assemble_document", lambda *a, **k: a[2])
    monkeypatch.setattr("app.docgen.plan_store.artifact_dir", lambda job_id: tmp_path)
    return seen


def test_edit_document_section_uses_section_key_not_plan_index(monkeypatch, tmp_path):
    seen = _setup_reordered(monkeypatch, tmp_path)

    ED.edit_document_section("job1", "Alpha", "tweak alpha")

    # (1) read-by-key: the writer saw ALPHA's current content, not BRAVO's.
    assert "BODY_A" in seen["instructions"] and "BODY_B" not in seen["instructions"]
    # (2) write-by-key: the rewrite landed on Alpha's slot; Bravo is preserved verbatim (no data loss).
    saved = {s["section_key"]: s for s in
             json.loads((tmp_path / "generated_sections.json").read_text())}
    assert saved["kA"]["body"] == "REWRITTEN_A"
    assert saved["kB"]["body"] == "BODY_B"
