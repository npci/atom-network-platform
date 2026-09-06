# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Precise document section editor.

Loads an existing .docx (via generated_sections.json artifact),
regenerates only the requested section with a user edit instruction,
and re-assembles a new version of the document.

Usage:
    from app.docgen.tools.document_editor import edit_document_section
    new_path = edit_document_section(job_id, section_heading, edit_instruction)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_artifact(job_id: str, name: str) -> Any:
    """Load a JSON artifact from the job's output directory."""
    from app.docgen.config import settings
    path = Path(settings.output_dir) / job_id / name
    if not path.exists():
        raise FileNotFoundError(f"Artifact not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def edit_document_section(
    job_id: str,
    section_heading: str,
    edit_instruction: str,
    output_suffix: str = "_edited",
) -> str:
    """Regenerate a single section of an existing document.

    Args:
        job_id:            Job ID whose artifacts (plan + sections) will be loaded.
        section_heading:   Exact or case-insensitive heading of the section to edit.
        edit_instruction:  Natural language instruction describing the edit to make.
        output_suffix:     Suffix appended to document filename (e.g. "_edited").

    Returns:
        Path to the updated .docx file.

    Raises:
        FileNotFoundError: If the job artifacts are missing.
        ValueError: If the section heading is not found in the plan.
    """
    from app.docgen.config import settings
    from app.docgen.agents.pipeline import _write_section, _make_llm_json
    from app.docgen.tools.docx_builder import assemble_document

    # ── 1. Load existing artifacts ────────────────────────────────────────────
    plan = _load_artifact(job_id, "document_plan.json")
    sections: list[dict] = _load_artifact(job_id, "generated_sections.json")

    # ── 2. Find target section in plan ────────────────────────────────────────
    plan_sections = plan.get("sections", [])
    target_idx: int | None = None
    for i, ps in enumerate(plan_sections):
        if ps.get("heading", "").strip().lower() == section_heading.strip().lower():
            target_idx = i
            break

    if target_idx is None:
        available = [ps.get("heading", "") for ps in plan_sections]
        raise ValueError(
            f"Section '{section_heading}' not found in document plan. "
            f"Available headings: {available}"
        )

    section_plan = plan_sections[target_idx]
    doc_type = plan.get("doc_type", "BRD")

    # ── 3. Augment section plan with the edit instruction ─────────────────────
    # The writer sees the CURRENT content and must apply only the requested
    # change — without it, a "change 40 to 70" edit regenerated the whole
    # section from the plan instructions and drifted everywhere else.
    original_instructions = section_plan.get("content_instructions", "")
    # Resolve current content by section_key — generated_sections.json can be in a
    # different order than the plan (mirrors edit_full_document / edit_divergent_
    # sections). Index is only a last-resort fallback when there's no key match.
    _key = section_plan.get("section_key")
    current = next((s for s in sections if _key and s.get("section_key") == _key), None)
    if current is None and target_idx < len(sections):
        current = sections[target_idx]
    current_block = (
        "[CURRENT SECTION CONTENT — JSON]\n"
        f"{json.dumps(current, ensure_ascii=False)}\n\n"
        "[REVISION RULES — STRICT]\n"
        "Apply ONLY the edit instruction to the current content above. Everything "
        "the instruction does not ask to change must be reproduced VERBATIM — same "
        "wording, same order, same numbers, same table values.\n\n"
        if current else ""
    )
    augmented_plan = {
        **section_plan,
        "content_instructions": (
            f"[EDIT INSTRUCTION — FOLLOW PRECISELY]\n{edit_instruction}\n\n"
            f"{current_block}"
            f"[ORIGINAL SECTION GUIDANCE]\n{original_instructions}"
        ),
    }

    # ── 4. Regenerate only the target section ─────────────────────────────────
    logger.info(
        "[edit_document_section] Regenerating section '%s' for job %s",
        section_heading, job_id,
    )
    llm = _make_llm_json()
    rag_context = ""  # Edit doesn't use RAG — uses the instruction directly

    new_content = _write_section(
        llm,
        augmented_plan,
        rag_context,
        doc_type=doc_type,
        audience=plan.get("document_meta", {}).get("audience", ""),
        desired_outcome=plan.get("document_meta", {}).get("desired_outcome", ""),
    )

    # Force correct heading and metadata
    new_content["section_key"] = section_plan.get("section_key")
    new_content["render_style"] = section_plan.get("render_style", "body")
    new_content["level"] = section_plan.get("level", 1)
    new_content["section_heading"] = section_plan.get("heading", section_heading)
    if doc_type.strip().lower() in ("brd", "product note"):
        new_content["code_blocks"] = []

    # ── 5. Replace the section in the sections list ───────────────────────────
    # Write back by section_key (the artifact order can differ from the plan's, so
    # a plain index write would overwrite the WRONG section). Fall back to
    # plan-index, then append if the section had no prior content.
    updated_sections = list(sections)
    _wkey = new_content.get("section_key")
    slot = next(
        (j for j, s in enumerate(updated_sections) if _wkey and s.get("section_key") == _wkey),
        None,
    )
    if slot is None and target_idx < len(updated_sections):
        slot = target_idx
    if slot is None:
        updated_sections.append(new_content)
    else:
        updated_sections[slot] = new_content

    # ── 6. Persist updated sections artifact ─────────────────────────────────
    from app.docgen.plan_store import artifact_dir
    artifact_path = artifact_dir(job_id) / "generated_sections.json"
    artifact_path.write_text(
        json.dumps(updated_sections, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("[edit_document_section] Updated generated_sections.json for job %s", job_id)

    # ── 7. Re-assemble the document ───────────────────────────────────────────
    job_dir = Path(settings.output_dir) / job_id
    output_path = str(job_dir / f"document{output_suffix}.docx")

    try:
        generated_diagrams = _load_artifact(job_id, "generated_diagrams.json") if (job_dir / "generated_diagrams.json").exists() else {}
    except Exception:
        generated_diagrams = {}

    final_path = assemble_document(
        plan,
        updated_sections,
        output_path,
        diagram_specs=plan.get("diagram_specs") or [],
        generated_diagrams=generated_diagrams,
    )

    logger.info("[edit_document_section] Re-assembled document: %s", final_path)
    return final_path


def edit_full_document(
    job_id: str,
    edit_instruction: str,
    output_suffix: str = "_edited",
    progress: dict | None = None,
) -> str:
    """Regenerate all planned sections using a document-wide edit instruction.

    `progress`, if supplied, is a caller-owned dict this function mutates as
    sections complete ({"done": int, "total": int}) — the async wrapper polls
    it to surface per-section progress without blocking the worker threads.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.docgen.config import settings
    from app.docgen.agents.pipeline import _write_section, _make_llm_json, _provider
    from app.docgen.tools.docx_builder import assemble_document
    from app.docgen.plan_store import artifact_dir

    plan = _load_artifact(job_id, "document_plan.json")
    sections: list[dict] = _load_artifact(job_id, "generated_sections.json")
    plan_sections = plan.get("sections", [])
    if not plan_sections:
        raise ValueError("Document plan does not contain any sections to edit.")
    if progress is not None:
        progress["total"] = len(plan_sections)

    llm = _make_llm_json()
    doc_type = plan.get("doc_type", "BRD")
    audience = plan.get("document_meta", {}).get("audience", "")
    desired_outcome = plan.get("document_meta", {}).get("desired_outcome", "")

    def _edit_one(index: int, section_plan: dict) -> tuple[int, dict]:
        original_instructions = section_plan.get("content_instructions", "")
        # Give the writer the section's CURRENT content with copy-unless-touched
        # rules. Without it every section regenerated from scratch on each
        # revision — the requested edit got lost and unrelated content drifted.
        key = section_plan.get("section_key")
        current = next(
            (s for s in sections if key and s.get("section_key") == key), None,
        )
        if current is None and index < len(sections):
            current = sections[index]
        current_block = (
            "[CURRENT SECTION CONTENT — JSON]\n"
            f"{json.dumps(current, ensure_ascii=False)}\n\n"
            "[REVISION RULES — STRICT]\n"
            "- If the edit instruction does NOT concern this section, return the "
            "current content JSON above EXACTLY as-is — do not rephrase, reorder, "
            "add or remove anything.\n"
            "- If it DOES concern this section, apply ONLY the requested change "
            "and reproduce all other wording, numbers, and table values VERBATIM.\n\n"
            if current else ""
        )
        augmented_plan = {
            **section_plan,
            "content_instructions": (
                f"[DOCUMENT-WIDE EDIT INSTRUCTION — APPLY PRECISELY]\n{edit_instruction}\n\n"
                f"{current_block}"
                f"[ORIGINAL SECTION GUIDANCE]\n{original_instructions}"
            ),
        }

        logger.info(
            "[edit_full_document] Regenerating section '%s' (%s/%s) for job %s",
            section_plan.get("heading", f"Section {index + 1}"),
            index + 1,
            len(plan_sections),
            job_id,
        )

        new_content = _write_section(
            llm,
            augmented_plan,
            "",
            doc_type=doc_type,
            audience=audience,
            desired_outcome=desired_outcome,
        )
        new_content["section_key"] = section_plan.get("section_key")
        new_content["render_style"] = section_plan.get("render_style", "body")
        new_content["level"] = section_plan.get("level", 1)
        new_content["section_heading"] = section_plan.get("heading", f"Section {index + 1}")
        if doc_type.strip().lower() in ("brd", "product note"):
            new_content["code_blocks"] = []
        return index, new_content

    # Parallel fan-out, same worker policy as the fresh pipeline's
    # write_content — the sequential loop made a full-document revision take
    # N × one-LLM-call wall-clock (20+ min on comprehensive BRDs).
    configured = min(settings.max_parallel_sections, len(plan_sections)) or 1
    max_workers = 1 if _provider() == "openai_compat" else configured
    results: dict[int, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_edit_one, i, sp): i
            for i, sp in enumerate(plan_sections)
        }
        for fut in as_completed(futures):
            idx, content = fut.result()
            results[idx] = content
            done += 1
            if progress is not None:
                progress["done"] = done
    updated_sections = [results[i] for i in range(len(plan_sections))]

    artifact_path = artifact_dir(job_id) / "generated_sections.json"
    artifact_path.write_text(
        json.dumps(updated_sections, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("[edit_full_document] Updated generated_sections.json for job %s", job_id)

    job_dir = Path(settings.output_dir) / job_id
    output_path = str(job_dir / f"document{output_suffix}.docx")

    try:
        generated_diagrams = _load_artifact(job_id, "generated_diagrams.json") if (job_dir / "generated_diagrams.json").exists() else {}
    except Exception:
        generated_diagrams = {}

    final_path = assemble_document(
        plan,
        updated_sections,
        output_path,
        diagram_specs=plan.get("diagram_specs") or [],
        generated_diagrams=generated_diagrams,
    )
    logger.info("[edit_full_document] Re-assembled document: %s", final_path)
    return final_path


def edit_divergent_sections(
    job_id: str,
    edit_instruction: str,
    divergent_items: list[str],
    output_suffix: str = "_edited",
    progress: dict | None = None,
) -> str:
    """Targeted variant of `edit_full_document`: re-write ONLY the sections that
    carry a flagged divergent item, copying every other section verbatim.

    `edit_full_document` regenerates all N sections (N LLM calls, each a chance to
    drift). When the plan-consistency gate flags a divergence it knows the exact
    item names (e.g. `ReqSetSpendLimit`), so we can find the section(s) that mention
    them and re-write just those — faster, and lower-drift because the untouched
    sections are not round-tripped through the writer at all.

    Falls back to a full-document edit when nothing matches (or no items were given)
    so a flagged blocker is never silently left in place — this also covers
    decision/contradiction findings whose item name isn't a literal token in the text.

    `progress`, if supplied, is mutated as sections complete ({"done", "total"}).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.docgen.config import settings
    from app.docgen.agents.pipeline import _write_section, _make_llm_json, _provider
    from app.docgen.tools.docx_builder import assemble_document
    from app.docgen.plan_store import artifact_dir

    plan = _load_artifact(job_id, "document_plan.json")
    sections: list[dict] = _load_artifact(job_id, "generated_sections.json")
    plan_sections = plan.get("sections", [])
    if not plan_sections:
        raise ValueError("Document plan does not contain any sections to edit.")

    def _current_for(index: int, section_plan: dict) -> dict | None:
        key = section_plan.get("section_key")
        cur = next((s for s in sections if key and s.get("section_key") == key), None)
        if cur is None and index < len(sections):
            cur = sections[index]
        return cur

    # Which plan sections actually mention a flagged item? Match case-insensitively
    # against the section's full JSON serialization so the token is caught wherever it
    # lives — heading, prose, bullet, table cell, or code block.
    needles = [str(it).strip().lower() for it in (divergent_items or []) if str(it).strip()]
    target_indices: list[int] = []
    if needles:
        for i, sp in enumerate(plan_sections):
            cur = _current_for(i, sp)
            if cur is None:
                continue
            hay = json.dumps(cur, ensure_ascii=False).lower()
            if any(n in hay for n in needles):
                target_indices.append(i)

    if not target_indices:
        logger.info(
            "[edit_divergent_sections] no section matched items=%s for job %s — editing all sections",
            needles, job_id,
        )
        return edit_full_document(job_id, edit_instruction, output_suffix=output_suffix, progress=progress)

    if progress is not None:
        progress["total"] = len(target_indices)

    llm = _make_llm_json()
    doc_type = plan.get("doc_type", "BRD")
    audience = plan.get("document_meta", {}).get("audience", "")
    desired_outcome = plan.get("document_meta", {}).get("desired_outcome", "")

    def _edit_one(index: int) -> tuple[int, dict]:
        section_plan = plan_sections[index]
        original_instructions = section_plan.get("content_instructions", "")
        current = _current_for(index, section_plan)
        current_block = (
            "[CURRENT SECTION CONTENT — JSON]\n"
            f"{json.dumps(current, ensure_ascii=False)}\n\n"
            "[REVISION RULES — STRICT]\n"
            "- Apply ONLY the edit instruction to the current content above.\n"
            "- Reproduce everything the instruction does not touch VERBATIM — same "
            "wording, numbers, and table values.\n\n"
            if current else ""
        )
        augmented_plan = {
            **section_plan,
            "content_instructions": (
                f"[DOCUMENT-WIDE EDIT INSTRUCTION — APPLY PRECISELY]\n{edit_instruction}\n\n"
                f"{current_block}"
                f"[ORIGINAL SECTION GUIDANCE]\n{original_instructions}"
            ),
        }
        logger.info(
            "[edit_divergent_sections] Regenerating divergent section '%s' (idx %s) for job %s",
            section_plan.get("heading", f"Section {index + 1}"), index, job_id,
        )
        new_content = _write_section(
            llm,
            augmented_plan,
            "",
            doc_type=doc_type,
            audience=audience,
            desired_outcome=desired_outcome,
        )
        new_content["section_key"] = section_plan.get("section_key")
        new_content["render_style"] = section_plan.get("render_style", "body")
        new_content["level"] = section_plan.get("level", 1)
        new_content["section_heading"] = section_plan.get("heading", f"Section {index + 1}")
        if doc_type.strip().lower() in ("brd", "product note"):
            new_content["code_blocks"] = []
        return index, new_content

    # Start from the existing sections and overwrite only the divergent ones (by
    # section_key, so we honour the artifact's order rather than the plan's).
    updated_sections = list(sections)
    configured = min(settings.max_parallel_sections, len(target_indices)) or 1
    max_workers = 1 if _provider() == "openai_compat" else configured
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_edit_one, i): i for i in target_indices}
        for fut in as_completed(futures):
            plan_idx, content = fut.result()
            key = content.get("section_key")
            slot = next(
                (j for j, s in enumerate(updated_sections) if key and s.get("section_key") == key),
                None,
            )
            if slot is None and plan_idx < len(updated_sections):
                slot = plan_idx
            if slot is None:
                updated_sections.append(content)
            else:
                updated_sections[slot] = content
            done += 1
            if progress is not None:
                progress["done"] = done

    artifact_path = artifact_dir(job_id) / "generated_sections.json"
    artifact_path.write_text(
        json.dumps(updated_sections, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(
        "[edit_divergent_sections] Re-wrote %d/%d sections for job %s",
        len(target_indices), len(plan_sections), job_id,
    )

    job_dir = Path(settings.output_dir) / job_id
    output_path = str(job_dir / f"document{output_suffix}.docx")
    try:
        generated_diagrams = _load_artifact(job_id, "generated_diagrams.json") if (job_dir / "generated_diagrams.json").exists() else {}
    except Exception:
        generated_diagrams = {}

    final_path = assemble_document(
        plan,
        updated_sections,
        output_path,
        diagram_specs=plan.get("diagram_specs") or [],
        generated_diagrams=generated_diagrams,
    )
    logger.info("[edit_divergent_sections] Re-assembled document: %s", final_path)
    return final_path
