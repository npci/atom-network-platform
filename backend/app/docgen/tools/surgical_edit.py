# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Surgical (patch-based) document editor — the M4 orchestrator.

Turns a natural-language edit instruction into a MINIMAL set of deterministic
patch ops and applies them, instead of regenerating whole sections. Flow:

    load plan + sections  →  ensure stable block IDs (persist the backfill)
      →  build a block index for the target scope
      →  ask a small patch-planner LLM for the ops (the "intent layer")
      →  apply ops deterministically (app.docgen.patch) with the diff gate
      →  discard the patch if the gate is violated (never corrupt the doc)
      →  persist generated_sections.json  →  re-assemble the .docx (deterministic)

The .docx re-assembly is whole-document (TOC / page numbers require it) but
LLM-free, so "surgical" only needs to hold at the JSON layer. When the planner
returns no ops the document is left byte-identical and simply re-assembled.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.llm import call_llm_structured
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE
from app.docgen.block_ids import LIST_FIELDS, ensure_document_ids
from app.docgen.patch import apply_ops, PatchError
from app.docgen.section_diff import diff_sections

logger = logging.getLogger(__name__)

MAX_PLANNER_TOKENS = 8000

_PLANNER_SYSTEM = (
    "You are a precise document PATCH PLANNER for the Authority change-management documents. "
    "You are given the addressable blocks of a document (each with a stable [id] and its "
    "current text) and an edit instruction. Output the MINIMAL set of patch operations that "
    "carry out the instruction. Touch ONLY blocks the instruction concerns — do NOT reword, "
    "reorder, or 'improve' any unrelated block.\n\n"
    "Operations (each a JSON object):\n"
    '  {"op":"find_replace","block_id":"<id>","find":"<literal>","replace":"<literal>"}\n'
    "      — smallest change: swap a literal substring inside one block. PREFER THIS.\n"
    '  {"op":"replace_text","block_id":"<id>","text":"<full new text>"}\n'
    "      — rewrite one block entirely (use only when the change exceeds a substring swap).\n"
    '  {"op":"set_cell","table_id":"<tid>","row_id":"<rid>","column":"<header name or index>","value":"<v>"}\n'
    "      — change one table cell.\n"
    '  {"op":"insert_row","table_id":"<tid>","after":"<row_id or null>","cells":["<c1>","<c2>"]}\n'
    "      — add one table row (cells align to the header order).\n"
    '  {"op":"delete_block","block_id":"<id>"}   — remove one block (or one table row).\n'
    '  {"op":"insert_block","section_key":"<skey>","field":"paragraphs|bullet_points|numbered_items|code_blocks","after":"<id or null>","text":"<text>"}\n'
    '  {"op":"replace_diagram_source","diagram_id":"<did>","source":"<full new diagram source>"}\n'
    "      — replace a diagram's source (the same PlantUML/Mermaid/JSON dialect shown for it); it is re-rendered.\n\n"
    "Rules:\n"
    " - Use the SMALLEST op that works (find_replace over replace_text when a substring suffices).\n"
    " - Only use block ids shown below; never invent an id.\n"
    " - Never touch a block the instruction does not require.\n"
    " - If the instruction requires no change, return an empty ops list.\n"
    'Respond with ONLY JSON: {"ops":[ ... ]}\n' + ANTI_INJECTION_CLAUSE
)


def _load(job_id: str, name: str):
    from app.docgen.config import settings
    path = Path(settings.output_dir) / job_id / name
    if not path.exists():
        raise FileNotFoundError(f"Artifact not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_scope(sections: list[dict], section_heading: str | None) -> str | None:
    """Return the section_key to scope the planner to, or None for whole-doc."""
    if not section_heading:
        return None
    want = section_heading.strip().lower()
    for sec in sections:
        if (sec.get("section_heading") or "").strip().lower() == want:
            return sec.get("section_key")
    logger.warning("[surgical_edit] section heading %r not found — planning over whole doc", section_heading)
    return None


def _block_index(sections: list[dict], only_key: str | None,
                 diagram_sources: dict | None = None) -> str:
    lines: list[str] = []
    for sec in sections:
        if only_key and sec.get("section_key") != only_key:
            continue
        lines.append(f"## [{sec.get('section_key')}] {sec.get('section_heading', '')}")
        ids = sec.get("block_ids") or {}
        for f in LIST_FIELDS:
            for bid, txt in zip(ids.get(f, []) or [], sec.get(f, []) or []):
                lines.append(f"[{bid}] {txt}")
        table = sec.get("table_data")
        if isinstance(table, dict) and table.get("rows"):
            t = ids.get("table") or {}
            lines.append(f"[{t.get('table_id')}] TABLE headers={table.get('headers')}")
            for rid, row in zip(t.get("row_ids", []) or [], table.get("rows", []) or []):
                lines.append(f"  [{rid}] {row}")
        lines.append("")
    # Diagrams are document-level; only expose them for whole-doc edits.
    if diagram_sources and not only_key:
        lines.append("## DIAGRAMS (edit via replace_diagram_source)")
        for did, info in diagram_sources.items():
            st = (info or {}).get("source_type", "")
            src = (info or {}).get("source", "")
            if isinstance(src, dict):
                src = json.dumps(src, ensure_ascii=False)
            lines.append(f"[diagram {did}] ({st})")
            lines.append(str(src))
            lines.append("")
    return "\n".join(lines)


def _locate_items(sections: list[dict], items: list[str], only_key: str | None) -> dict[str, list[str]]:
    """Map each named item (e.g. a divergent wire message) → the block IDs whose
    text contains it. Deterministic grep — the "by-ID" half of surgical
    consistency repair. Items that don't resolve (contradiction/decision findings
    with no literal token) simply return no ids and the planner locates them."""
    needles = [(it, it.strip().lower()) for it in (items or []) if it and it.strip()]
    hits: dict[str, list[str]] = {}
    if not needles:
        return hits
    for sec in sections:
        if only_key and sec.get("section_key") != only_key:
            continue
        ids = sec.get("block_ids") or {}
        for f in LIST_FIELDS:
            for bid, txt in zip(ids.get(f, []) or [], sec.get(f, []) or []):
                low = str(txt).lower()
                for orig, n in needles:
                    if n in low:
                        hits.setdefault(orig, []).append(bid)
        table = sec.get("table_data")
        if isinstance(table, dict):
            t = ids.get("table") or {}
            for rid, row in zip(t.get("row_ids", []) or [], table.get("rows", []) or []):
                low = json.dumps(row, ensure_ascii=False).lower()
                for orig, n in needles:
                    if n in low:
                        hits.setdefault(orig, []).append(rid)
    return hits


_DIAGRAM_HINTS = ("diagram", "plantuml", "mermaid", "uml", "flowchart", "flow chart",
                  "sequence", "swimlane", "architecture view")


async def _plan_ops(sections: list[dict], instruction: str, only_key: str | None,
                    focus_items: list[str] | None = None,
                    diagram_sources: dict | None = None) -> list[dict]:
    # Diagram sources are hundreds of lines EACH and were dumped into every whole-doc edit —
    # the main reason this planner averaged ~36k input tokens per call. Include them only
    # when the instruction (or a focus item) plausibly targets a diagram; otherwise list the
    # ids with sources omitted, so the capability stays visible without the token bill.
    _needles = (instruction or "").lower() + " " + " ".join(focus_items or []).lower()
    wants_diagrams = any(k in _needles for k in _DIAGRAM_HINTS)
    index = _block_index(sections, only_key, diagram_sources if wants_diagrams else None)
    if diagram_sources and not wants_diagrams and not only_key:
        index += ("\n## DIAGRAMS present but sources omitted (instruction does not target them): "
                  + ", ".join(str(k) for k in diagram_sources) + "\n")
    if not index.strip():
        return []
    focus = ""
    if focus_items:
        located = _locate_items(sections, focus_items, only_key)
        lines = []
        for it in focus_items:
            ids = located.get(it)
            lines.append(f"- {it}: appears in blocks {ids}" if ids
                         else f"- {it}: not found verbatim in the document")
        focus = ("\nFOCUS — the edit concerns ONLY these items; apply the instruction above to each "
                 "(remove / correct to the required form / add where missing) and do NOT touch "
                 "anything else:\n" + "\n".join(lines) + "\n")
    # Prompt-cache split: the block index is the bulk (10-35k tokens) and is byte-stable
    # across successive edits of the same document (block IDs are persisted; text changes
    # only where an op applied) — so it rides as a cacheable SYSTEM segment while the
    # per-edit instruction stays in the user turn. Blocks-first / instruction-last also
    # means consecutive edits in a review session share the prefix up to the first change.
    from app.core.prompt_blocks import segments_for_anthropic_cache
    system = segments_for_anthropic_cache([
        (_PLANNER_SYSTEM, True),
        ("DOCUMENT BLOCKS (id → current text):\n" + wrap_untrusted(index, "BLOCKS"), True),
    ])
    user = (
        f"EDIT INSTRUCTION:\n{wrap_untrusted(instruction, 'EDIT_INSTRUCTION')}\n"
        f"{focus}\n"
        "Return the minimal ops JSON now."
    )
    try:
        # Forced tool use (was prose JSON): find/replace ops carry verbatim document text —
        # the exact content that malforms prose JSON. Field-level semantics (valid block ids,
        # dangling ops) are still enforced downstream by apply_ops + the diff gate.
        data = await call_llm_structured(
            system, user,
            schema={"type": "object",
                    "properties": {"ops": {
                        "type": "array",
                        "items": {"type": "object",
                                  "properties": {
                                      "op": {"type": "string",
                                             "enum": ["find_replace", "replace_text", "set_cell",
                                                      "insert_row", "delete_block", "insert_block",
                                                      "replace_diagram_source"]},
                                      "block_id": {"type": "string"},
                                      "find": {"type": "string"},
                                      "replace": {"type": "string"},
                                      "text": {"type": "string"},
                                      "table_id": {"type": "string"},
                                      "row_id": {"type": "string"},
                                      "column": {"type": ["string", "integer"]},
                                      "value": {"type": "string"},
                                      "after": {"type": ["string", "null"]},
                                      "cells": {"type": "array", "items": {"type": "string"}},
                                      "section_key": {"type": "string"},
                                      "field": {"type": "string"},
                                      "diagram_id": {"type": "string"},
                                      "source": {"type": "string"}},
                                  "required": ["op"]}}},
                    "required": ["ops"]},
            tool_name="record_ops", agent_name="docgen_patch_planner",
            max_tokens=MAX_PLANNER_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — fail-open: no ops → doc left unchanged
        logger.warning("[surgical_edit] planner LLM failed (%s) — no ops", e)
        return []
    ops = data.get("ops") if isinstance(data, dict) else None
    return [o for o in (ops or []) if isinstance(o, dict) and o.get("op")]


def _apply_gated(sections: list[dict], ops: list[dict]) -> tuple[list[dict], int]:
    """Apply ops one-by-one (skipping any that dangle), then run the diff gate.
    Returns (result_sections, applied_count). If the gate is violated the whole
    patch is discarded and the ORIGINAL sections are returned unchanged."""
    cur = sections
    cumulative: dict[int, set[str]] = {}
    applied = 0
    for op in ops:
        try:
            nxt, touched = apply_ops(cur, [op])
        except PatchError as e:
            logger.warning("[surgical_edit] skipping bad op %s: %s", op, e)
            continue
        cur = nxt
        for si, ids in touched.items():
            cumulative.setdefault(si, set()).update(ids)
        applied += 1

    if applied == 0:
        return sections, 0

    violations: dict[int, set[str]] = {}
    for si in range(len(cur)):
        moved = diff_sections(sections[si], cur[si]).touched
        extra = moved - cumulative.get(si, set())
        if extra:
            violations[si] = extra
    if violations:
        logger.warning("[surgical_edit] diff-gate violations %s — discarding patch", violations)
        return sections, 0
    return cur, applied


_DIAGRAM_OPS = {"replace_diagram_source"}


def _split_ops(ops: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate content-block ops (handled by the deterministic patch applier)
    from diagram ops (handled by re-rendering — they have side effects and can't
    go through the pure patch layer)."""
    content = [o for o in ops if o.get("op") not in _DIAGRAM_OPS]
    diagram = [o for o in ops if o.get("op") in _DIAGRAM_OPS]
    return content, diagram


def _rerender_diagram(job_id: str, diagram_id: str, source_type: str, source,
                      dtype_map: dict) -> str | None:
    """Re-render one diagram from edited source to its PNG. Returns the path on
    success, None on failure.

    Routes every source through `generate_diagram`, which owns the same
    mermaid → PlantUML → Pillow fallback ladder the generation path uses — so a
    box without `mmdc` (or `java`) still produces *some* PNG rather than failing.
    Callers must treat a None return as "nothing rendered" and NOT persist the
    new source, or the stored source, the on-disk PNG and the embedded diagram
    diverge silently."""
    from app.docgen.tools.diagram_generator import generate_diagram
    from app.docgen.plan_store import artifact_dir
    out_path = str(artifact_dir(job_id) / f"{diagram_id}.png")
    st = (source_type or "").lower()
    dtype = dtype_map.get(diagram_id, "flowchart")
    try:
        if st == "mermaid":
            spec = {"mermaid_source": str(source or "")}
        elif st == "plantuml":
            spec = {"plantuml_source": str(source or "")}
        else:
            spec = source if isinstance(source, dict) else None
            if spec is None:
                try:
                    spec = json.loads(source)
                except Exception:
                    spec = {}
        return generate_diagram(spec, dtype, out_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("[surgical_edit] diagram re-render failed for %s: %s", diagram_id, e)
        return None


async def surgical_edit_document(
    job_id: str,
    edit_instruction: str,
    *,
    section_heading: str | None = None,
    focus_items: list[str] | None = None,
    on_progress=None,
    output_suffix: str = "_edited",
) -> str:
    """Apply a natural-language edit to a docgen document surgically. Persists the
    updated generated_sections.json and re-assembles the .docx; returns its path.
    """
    from app.docgen.tools.docx_builder import assemble_document
    from app.docgen.plan_store import artifact_dir
    from app.docgen.config import settings

    plan = _load(job_id, "document_plan.json")
    sections: list[dict] = _load(job_id, "generated_sections.json")

    # Backfill stable IDs for documents generated before IDs existed, and persist
    # so every later edit sees the same handles.
    ensure_document_ids(sections)
    (artifact_dir(job_id) / "generated_sections.json").write_text(
        json.dumps(sections, indent=2, ensure_ascii=False), encoding="utf-8")

    if on_progress is not None:
        try:
            await on_progress(0, 1)
        except Exception:
            pass

    job_dir = Path(settings.output_dir) / job_id
    diagram_sources = _load(job_id, "generated_diagram_sources.json") if (job_dir / "generated_diagram_sources.json").exists() else {}
    try:
        gen_diagrams = _load(job_id, "generated_diagrams.json") if (job_dir / "generated_diagrams.json").exists() else {}
    except Exception:
        gen_diagrams = {}
    dtype_map = {s.get("diagram_id"): s.get("diagram_type", "flowchart")
                 for s in (plan.get("diagram_specs") or [])}

    only_key = _resolve_scope(sections, section_heading)
    ops = await _plan_ops(sections, edit_instruction, only_key,
                          focus_items=focus_items, diagram_sources=diagram_sources)
    content_ops, diagram_ops = _split_ops(ops)

    updated, applied = _apply_gated(sections, content_ops)

    diagrams_changed = False
    for op in diagram_ops:
        did = op.get("diagram_id")
        if not did or did not in diagram_sources:
            logger.warning("[surgical_edit] replace_diagram_source: unknown diagram %r", did)
            continue
        new_src = op.get("source")
        if not (str(new_src or "").strip() or isinstance(new_src, dict)):
            # Empty/missing source — skip so we don't clobber the stored source
            # with None and render a placeholder on the next edit.
            logger.warning("[surgical_edit] replace_diagram_source: empty source for %r — skipped", did)
            continue
        st = op.get("source_type") or (diagram_sources.get(did) or {}).get("source_type") or "json"
        png = _rerender_diagram(job_id, did, st, new_src, dtype_map)
        if not png:
            # Re-render failed even through the Pillow fallback. Do NOT persist the
            # new source or update the diagram — leaving source, PNG and the embedded
            # image in sync (old) rather than shipping a doc whose stored source and
            # rendered image disagree. The edit is a no-op; the caller logs it.
            logger.warning("[surgical_edit] replace_diagram_source: render failed for %r — edit skipped", did)
            continue
        diagram_sources[did] = {"source_type": st, "source": new_src}
        gen_diagrams[did] = png
        diagrams_changed = True

    logger.info("[surgical_edit] job=%s planned=%d content_applied=%d diagram_ops=%d scope=%s",
                job_id, len(ops), applied, len(diagram_ops), only_key or "whole-doc")

    (artifact_dir(job_id) / "generated_sections.json").write_text(
        json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")
    if diagrams_changed:
        (artifact_dir(job_id) / "generated_diagram_sources.json").write_text(
            json.dumps(diagram_sources, indent=2, ensure_ascii=False), encoding="utf-8")
        (artifact_dir(job_id) / "generated_diagrams.json").write_text(
            json.dumps(gen_diagrams, indent=2, ensure_ascii=False), encoding="utf-8")

    output_path = str(job_dir / f"document{output_suffix}.docx")
    final_path = assemble_document(
        plan, updated, output_path,
        diagram_specs=plan.get("diagram_specs") or [],
        generated_diagrams=gen_diagrams,
    )
    if on_progress is not None:
        try:
            await on_progress(1, 1)
        except Exception:
            pass
    logger.info("[surgical_edit] re-assembled %s", final_path)
    return final_path
