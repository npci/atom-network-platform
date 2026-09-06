# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Diagram source persistence + Mermaid rendering (M6).

The unlock: diagram SOURCE is persisted (generated_diagram_sources.json) instead
of only the rendered PNG, so a diagram is re-renderable/editable. Mermaid is an
optional engine that renders via the mmdc CLI when present and falls back to the
Pillow JSON renderer when it isn't (so nothing breaks without the binary).
"""
from pathlib import Path


class _Resp:
    def __init__(self, content):
        self.content = content


class _LLM:
    def __init__(self, content):
        self._c = content

    def invoke(self, _msgs):
        return _Resp(self._c)


# ── Mermaid renderer: graceful without the binary ───────────────────────────
def test_generate_mermaid_returns_none_without_cli(monkeypatch):
    import app.docgen.tools.diagram_generator as DG
    monkeypatch.setattr(DG, "_find_mermaid_cli", lambda: None)
    assert DG.generate_mermaid_diagram("flowchart TD\nA-->B", "/tmp/none.png") is None


def test_dispatcher_falls_back_to_pillow_when_no_mermaid(monkeypatch, tmp_path):
    import app.docgen.tools.diagram_generator as DG
    monkeypatch.setattr(DG, "_find_mermaid_cli", lambda: None)
    out = str(tmp_path / "d.png")
    spec = {
        "mermaid_source": "flowchart TD\n A-->B",
        "title": "t",
        "nodes": [{"id": "a", "label": "A", "node_type": "start"},
                  {"id": "b", "label": "B", "node_type": "end"}],
        "edges": [{"from_node": "a", "to_node": "b", "label": ""}],
    }
    result = DG.generate_diagram(spec, "flowchart", out)
    assert result == out and Path(out).exists()


# ── Source persistence ──────────────────────────────────────────────────────
def test_single_diagram_returns_plantuml_source(monkeypatch, tmp_path):
    import app.docgen.agents.pipeline as PL
    import app.docgen.tools.diagram_generator as DG
    monkeypatch.setattr(DG, "generate_plantuml_diagram", lambda src, out: out)  # pretend jar rendered
    spec = {"diagram_id": "d0", "diagram_type": "sequence", "description": "login flow"}
    llm = _LLM("@startuml\nA -> B: hi\n@enduml")
    did, path, src = PL._generate_single_diagram(llm, spec, str(tmp_path), llm_content=llm)
    assert did == "d0"
    assert src["source_type"] == "plantuml"
    assert "@startuml" in src["source"]


def test_generate_diagrams_persists_sources(monkeypatch, tmp_path):
    import app.docgen.agents.pipeline as PL
    monkeypatch.setattr(PL, "_make_llm_json", lambda: object())
    monkeypatch.setattr(PL, "_make_llm_content", lambda: object())
    monkeypatch.setattr(
        PL, "_generate_single_diagram",
        lambda llm, spec, out, llm_content=None: (spec["diagram_id"], out + "/x.png",
                                                  {"source_type": "plantuml", "source": "@startuml"}),
    )
    saved: dict = {}
    monkeypatch.setattr(PL, "save_json_artifact", lambda job, name, payload: saved.setdefault(name, payload))

    state = {"job_id": "DIAGTEST_TMP", "include_diagrams": True,
             "diagram_specs": [{"diagram_id": "d0", "diagram_type": "sequence", "description": "x"}]}
    out = PL.generate_diagrams(state)

    assert "generated_diagram_sources.json" in saved
    assert saved["generated_diagram_sources.json"]["d0"]["source_type"] == "plantuml"
    assert out["generated_diagram_sources"]["d0"]["source"] == "@startuml"


# ── Diagram-persistence fix: editors can re-embed diagrams on re-assembly ────
def test_generate_diagrams_persists_map_and_specs(monkeypatch):
    """Regression fix: the pipeline must persist generated_diagrams.json (id→PNG map) and
    diagram_specs (into document_plan.json) so an edit/re-assembly re-embeds diagrams
    instead of silently dropping them."""
    import json
    import shutil
    import app.docgen.agents.pipeline as PL
    from app.docgen.plan_store import artifact_dir

    job = "DIAGPERSIST_TEST"
    jd = artifact_dir(job)
    try:
        (jd / "document_plan.json").write_text(json.dumps(
            {"title": "T", "doc_type": "BRD", "sections": [{"section_key": "s", "heading": "H"}]}))
        monkeypatch.setattr(PL, "_make_llm_json", lambda: object())
        monkeypatch.setattr(PL, "_make_llm_content", lambda: object())
        monkeypatch.setattr(PL, "_generate_single_diagram",
                            lambda llm, spec, out, llm_content=None: (
                                spec["diagram_id"], out + "/x.png",
                                {"source_type": "plantuml", "source": "@startuml"}))
        PL.generate_diagrams({"job_id": job, "include_diagrams": True,
                              "diagram_specs": [{"diagram_id": "diagram_1", "section_index": 0,
                                                 "target_heading": "H", "diagram_type": "sequence",
                                                 "description": "x"}]})
        assert (jd / "generated_diagrams.json").exists()   # the id→PNG map is now on disk
        assert len(json.loads((jd / "document_plan.json").read_text()).get("diagram_specs") or []) == 1
    finally:
        shutil.rmtree(jd, ignore_errors=True)


# ── Mermaid one-retry: invalid syntax self-corrects before PlantUML fallback ─
def test_mermaid_render_retries_before_fallback(monkeypatch, tmp_path):
    import app.docgen.agents.pipeline as PL
    import app.docgen.tools.diagram_generator as DG
    from app.docgen.config import settings

    monkeypatch.setattr(settings, "diagram_engine", "mermaid")
    calls = {"n": 0}

    def _fake_render(src, out):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, "Parse error on line 2: got 'TXT'"   # first render fails
        Path(out).write_text("png"); return out, ""            # retry succeeds
    monkeypatch.setattr(DG, "render_mermaid", _fake_render)

    class _Resp:
        def __init__(self, c): self.content = c

    class _Seq:
        def __init__(self, seq): self.seq = seq; self.i = 0
        def invoke(self, _m):
            c = self.seq[min(self.i, len(self.seq) - 1)]; self.i += 1; return _Resp(c)

    llm = _Seq(["bad mermaid", "sequenceDiagram\n A->>B: ok"])   # gen, then fix
    did, path, src = PL._generate_single_diagram(
        object(), {"diagram_id": "d0", "diagram_type": "sequence", "description": "x"},
        str(tmp_path), llm_content=llm)

    assert src["source_type"] == "mermaid"   # recovered via retry — did NOT fall back to PlantUML
    assert calls["n"] == 2                    # rendered twice (initial + retry)
    assert llm.i == 2                         # model asked twice (generate + fix)
