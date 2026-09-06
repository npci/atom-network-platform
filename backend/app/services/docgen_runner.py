# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""DocGen runner — bridge between the platform's existing WS handlers and the docgen pipeline's
LangGraph pipeline.

What this module owns:
  • build_initial_state()          — translate the platform change-request context
                                      → the docgen pipeline state dict
  • run_pipeline_in_thread()       — execute pipeline.run_pipeline off the WS
                                      event loop (it's CPU/IO heavy, ~3-5 min)
  • sections_to_markdown()         — flatten the docgen pipeline's structured `generated_sections`
                                      into the markdown shape the platform's UI / DB / validation expect
  • emit_stage_progress_text()     — short human-readable stage banner used as a
                                      single-chunk WS message between phases
  • get_latest_job() / set_latest_job() — in-memory map of (change_id, doc_type)
                                      → most recent job_id, used by the Revise path
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── In-memory map of latest job per (change_id, doc_type) ────────────────────
# Lost on backend restart. Used by Revise to find the job_id whose artifacts
# document_editor needs. Future v2: persist on the BRD/TechSpec/etc. row.
_LATEST_JOB: dict[tuple[str, str], str] = {}
_LATEST_JOB_LOCK = threading.Lock()


def get_latest_job(change_id: str, doc_type: str) -> Optional[str]:
    with _LATEST_JOB_LOCK:
        return _LATEST_JOB.get((change_id, doc_type))


def set_latest_job(change_id: str, doc_type: str, job_id: str) -> None:
    with _LATEST_JOB_LOCK:
        _LATEST_JOB[(change_id, doc_type)] = job_id


# ── Stage → human-readable banner for WS streaming UX ────────────────────────
_STAGE_LABELS = {
    "retrieving":          "Retrieving knowledge base context",
    "planning":            "Planning document structure",
    "generating_diagrams": "Generating UML diagrams",
    "writing":             "Writing section content",
    "reviewing":           "Validating sections",
    "assembling":          "Building .docx",
    "completed":           "Document ready",
}


def emit_stage_progress_text(stage: str) -> str:
    """Return a one-line progress banner for a given pipeline stage.

    The string is intentionally markdown-friendly so the existing ReactMarkdown
    renderer in BRD.jsx / TechSpec.jsx shows it nicely while content is still
    being generated.
    """
    label = _STAGE_LABELS.get(stage, stage.replace("_", " ").title())
    return f"\n*{label}…*\n"


# ── Build initial state from the platform change-request context ────────────────────

def build_initial_state(
    *,
    doc_type: str,                         # 'BRD' | 'TSD' | 'Circular' | 'Product Note'
    change_id: str,
    prompt: str,
    document_title: str = "",
    version_number: str = "1.0",
    classification: str = "Internal",
    audience: str = "",
    desired_outcome: str = "",
    organization_name: str = "NPCI",
    research_report: str = "",
    canvas_content: str = "",
    additional_context: str = "",
    include_diagrams: bool = True,
    use_rag: bool = True,
    job_id: Optional[str] = None,
    brd_tier_override: Optional[str] = None,  # "compact" / "standard" / "comprehensive" — overrides classifier
    proposals: Optional[dict] = None,         # structured ground-truth (apis/error_codes/flow) for section writers
    source_skeleton: str = "",                # upstream binding spine (e.g. BRD FR/flow skeleton for a TSD)
    decisions_block: str = "",                # binding human-ratified Decision Ledger block (S2/S4)
    source_flow_spec: Optional[dict] = None,  # shared machine-readable flow spec → consistent BRD/TSD diagrams
    source_xsd: str = "",                     # approved schemas verbatim → TSD reproduces real XML (S6)
    source_xsd_bundle: str = "",              # UNCHANGED schemas involved in the flow → wire sections cite real siblings
    tech_design: str = "",                    # ratified code-grounded design (classes/keys/codes) → TSD names them verbatim
    ratified_plan: str = "",                  # ratified functional plan (overview/steps/compat/assumptions) → BRD is written FROM it
) -> dict:
    """Construct the dict that `pipeline.run_pipeline()` expects.

    The platform change-requests carry rich pre-context (enhanced prompt + research +
    canvas). We fold all of that into the `prompt` field so the docgen pipeline's planner /
    section-writer have it without us re-architecting the pipeline.
    """
    job_id = job_id or uuid.uuid4().hex

    rich_prompt_parts = [prompt.strip()]
    if research_report and research_report != "No research report available.":
        rich_prompt_parts.append("\n\n--- Research Report (use as primary domain context) ---\n")
        rich_prompt_parts.append(research_report.strip())
    if canvas_content and canvas_content != "No canvas available.":
        rich_prompt_parts.append("\n\n--- Product Canvas ---\n")
        rich_prompt_parts.append(canvas_content.strip())
    if additional_context:
        rich_prompt_parts.append("\n\n--- Additional Context ---\n")
        rich_prompt_parts.append(additional_context.strip())

    rich_prompt = "\n".join(rich_prompt_parts)

    return {
        "job_id":            job_id,
        "session_id":        change_id,
        "doc_type":          doc_type,
        "prompt":            rich_prompt,
        "feature_description": prompt.strip(),
        "document_title":    document_title or f"{doc_type}: change {change_id[:8]}",
        "version_number":    version_number,
        "classification":    classification,
        "collection_name":   "upi_knowledge",
        "use_rag":           use_rag,
        # Structured ground truth + upstream binding spine. The section writers
        # read these directly (state['proposals'] populates the AUTHORITATIVE
        # block; state['source_skeleton'] binds each downstream section to the
        # upstream doc's flows). Without these the docgen writers re-author from
        # planner-compressed instructions alone — the BRD/TSD deviation cause.
        "proposals":         proposals or {},
        "source_skeleton":   source_skeleton or "",
        "decisions_block":   decisions_block or "",
        "source_flow_spec":  source_flow_spec or {},
        "source_xsd":        source_xsd or "",
        "source_xsd_bundle": source_xsd_bundle or "",
        "tech_design":       tech_design or "",
        "ratified_plan":     ratified_plan or "",
        "include_diagrams":  include_diagrams,
        "audience":          audience or "Product Managers, Tech Leads",
        "desired_outcome":   desired_outcome or f"Approved {doc_type}",
        "organization_name": organization_name,
        "document_meta":     {},
        "rag_chunks":        [],
        "rag_context":       "",
        "document_plan":     None,
        "generated_diagrams": {},
        "generated_sections": [],
        "output_path":       None,
        "status":            "pending",
        "progress":          0,
        "current_step":      "Queued",
        "error":             None,
        # BRD tier override (compact / standard / comprehensive) — when set,
        # the pipeline skips its own classifier and uses this value directly.
        "brd_tier_override": (brd_tier_override or "").strip().lower() or None,
    }


# ── Async wrapper around the sync pipeline ───────────────────────────────────

async def run_pipeline_in_thread(state: dict, on_stage=None, cancel_check=None) -> dict:
    """Execute the docgen pipeline's `pipeline.run_pipeline()` in a worker thread.

    LangGraph `run_pipeline()` is fully synchronous and blocks for minutes.
    We `asyncio.to_thread` it so the WebSocket event loop stays responsive.

    `on_stage`, if supplied, is an async callable invoked once per pipeline
    stage transition (`retrieving` → `planning` → `generating_diagrams` →
    `writing` → `reviewing` → `assembling` → `completed`).

    The nodes DO assign `state["status"]` at each transition, but the graph is
    a `StateGraph(dict)`: LangGraph hands each node a freshly-merged dict, so
    those writes never land in the dict we hold here. Polling it only ever saw
    `pending` and then the last node's value — which is why a client that
    reconnected mid-run stayed pinned on the first stage banner for the whole
    generation. We stream the graph instead (`stream_mode="values"` yields the
    full state after every superstep, and its last item equals what `invoke`
    would have returned) and publish each snapshot's status for the poll loop.

    R-9 — `cancel_check`, if supplied, is a sync callable (no args) that
    returns True when the job has been marked cancelled in the registry.
    Polled on the same 1.5 sec cadence as `on_stage`. When True, we cancel
    the worker thread (best-effort — Python doesn't allow truly killing
    a thread, so the underlying LangGraph pipeline keeps running until
    its current node completes; subsequent nodes will short-circuit if
    they also poll the cancel flag) and return a synthetic
    `{"status": "cancelled"}` final state.
    """
    from app.docgen.agents.pipeline import get_pipeline
    from app.core.observability import set_usage_context, reset_usage_context

    logger.info(
        "[docgen-runner] starting pipeline doc_type=%s job_id=%s",
        state.get("doc_type"), state.get("job_id"),
    )

    # Attribute this pipeline's LLM spend to the owning change and to a per-document
    # section (BRD / TSD / Circular / Product Note), so the Usage dashboard shows docgen
    # IN the flow split per document — rather than rolled up as 'other (non-flow)'. The
    # docgen bridge runs every call on its own thread pool; set_usage_context here is seen
    # there because asyncio.to_thread copies this context and the bridge propagates it
    # across its executor (llm_bridge._run_sync). state['session_id'] is the change_id.
    _doc = re.sub(r"[^a-z0-9]+", "_", (state.get("doc_type") or "").lower()).strip("_")
    _usage_tok = set_usage_context(
        change_request_id=state.get("session_id") or None,
        section_default="docgen_" + (_doc or "doc"),
    )
    # Written by the worker thread after each superstep, read by the poll loop
    # below. A single string assignment either side of the GIL — no lock needed.
    progress = {"status": state.get("status") or ""}

    def _run_streamed() -> dict:
        final_state = state
        for snapshot in get_pipeline().stream(state, stream_mode="values"):
            final_state = snapshot
            progress["status"] = snapshot.get("status") or ""
        return final_state

    try:
        pipeline_task = asyncio.create_task(asyncio.to_thread(_run_streamed))

        if on_stage is not None or cancel_check is not None:
            last_stage = progress["status"]
            try:
                while not pipeline_task.done():
                    await asyncio.sleep(1.5)
                    # R-9 — cooperative cancel check. We don't truly kill the
                    # worker thread (Python doesn't permit it cleanly); we
                    # simply stop awaiting it and return a cancelled marker.
                    # The thread continues until its current node returns,
                    # which is acceptable: the work is already in progress
                    # and terminating mid-LLM-call would orphan resources.
                    if cancel_check is not None:
                        try:
                            if cancel_check():
                                logger.info(
                                    "[docgen-runner] cancel_check returned True "
                                    "doc_type=%s job_id=%s — abandoning pipeline await",
                                    state.get("doc_type"), state.get("job_id"),
                                )
                                return {"status": "cancelled", "error": "Cancelled by user"}
                        except Exception:
                            logger.exception("cancel_check raised — ignoring")

                    if on_stage is not None:
                        cur = (progress["status"] or "").lower()
                        if cur and cur != last_stage:
                            last_stage = cur
                            try:
                                await on_stage(cur)
                            except Exception:
                                logger.exception("on_stage callback raised — ignoring")
            except asyncio.CancelledError:
                pipeline_task.cancel()
                raise

        final = await pipeline_task
    finally:
        reset_usage_context(_usage_tok)

    logger.info(
        "[docgen-runner] pipeline finished doc_type=%s job_id=%s status=%s",
        state.get("doc_type"), state.get("job_id"), final.get("status"),
    )
    return final


# ── Section editor (Revise path) ─────────────────────────────────────────────

async def edit_section_in_thread(
    job_id: str,
    section_heading: str,
    edit_instruction: str,
) -> str:
    """Run the section editor off the WS event loop. Returns new docx path.

    When `settings.surgical_edit` is on, routes to the patch-based editor
    (block-id targeted, diff-gated); otherwise the whole-section regenerator.
    """
    from app.docgen.config import settings
    if settings.surgical_edit:
        from app.docgen.tools.surgical_edit import surgical_edit_document
        logger.info("[docgen-runner] surgical section edit job_id=%s heading=%r", job_id, section_heading)
        return await surgical_edit_document(job_id, edit_instruction, section_heading=section_heading)

    from app.docgen.tools.document_editor import edit_document_section

    logger.info(
        "[docgen-runner] editing section job_id=%s heading=%r",
        job_id, section_heading,
    )
    new_path = await asyncio.to_thread(
        edit_document_section, job_id, section_heading, edit_instruction,
    )
    logger.info("[docgen-runner] edit complete job_id=%s new_path=%s", job_id, new_path)
    return new_path


async def edit_full_document_in_thread(
    job_id: str,
    edit_instruction: str,
    on_progress=None,
) -> str:
    """Full-document edit (every section gets the same directive applied).

    `on_progress`, if supplied, is an async callable invoked as
    `await on_progress(done, total)` whenever another section completes.
    Same shared-dict polling bridge as run_pipeline_in_thread's on_stage —
    the worker threads mutate the dict, this coroutine polls it.

    When `settings.surgical_edit` is on, routes to the patch-based editor
    (one planner call + deterministic diff-gated apply) instead of regenerating
    every section.
    """
    from app.docgen.config import settings
    if settings.surgical_edit:
        from app.docgen.tools.surgical_edit import surgical_edit_document
        logger.info("[docgen-runner] surgical full-doc edit job_id=%s", job_id)
        return await surgical_edit_document(job_id, edit_instruction, on_progress=on_progress)

    from app.docgen.tools.document_editor import edit_full_document

    logger.info("[docgen-runner] editing full doc job_id=%s", job_id)
    progress: dict = {"done": 0, "total": 0}
    edit_task = asyncio.create_task(asyncio.to_thread(
        edit_full_document, job_id, edit_instruction, progress=progress,
    ))

    if on_progress is not None:
        last_done = -1
        try:
            while not edit_task.done():
                await asyncio.sleep(2.0)
                done, total = progress["done"], progress["total"]
                if total and done != last_done:
                    last_done = done
                    try:
                        await on_progress(done, total)
                    except Exception:
                        logger.exception("on_progress callback raised — ignoring")
        except asyncio.CancelledError:
            edit_task.cancel()
            raise

    new_path = await edit_task
    logger.info("[docgen-runner] full-doc edit complete job_id=%s new_path=%s", job_id, new_path)
    return new_path


async def edit_divergent_sections_in_thread(
    job_id: str,
    edit_instruction: str,
    divergent_items: list[str],
    on_progress=None,
) -> str:
    """Targeted variant of `edit_full_document_in_thread` — re-writes only the
    sections that carry a flagged divergent item (falls back to a full-document
    edit when nothing matches). Same shared-dict progress bridge.

    When `settings.surgical_edit` is on, routes to the patch-based editor with the
    divergent item names as focus hints (resolved to block IDs), so the repair is
    a minimal diff-gated patch instead of a section regeneration.
    """
    from app.docgen.config import settings
    if settings.surgical_edit:
        from app.docgen.tools.surgical_edit import surgical_edit_document
        logger.info("[docgen-runner] surgical consistency repair job_id=%s items=%d",
                    job_id, len(divergent_items or []))
        return await surgical_edit_document(
            job_id, edit_instruction, focus_items=divergent_items, on_progress=on_progress)

    from app.docgen.tools.document_editor import edit_divergent_sections

    logger.info("[docgen-runner] editing divergent sections job_id=%s items=%d",
                job_id, len(divergent_items or []))
    progress: dict = {"done": 0, "total": 0}
    edit_task = asyncio.create_task(asyncio.to_thread(
        edit_divergent_sections, job_id, edit_instruction, divergent_items, progress=progress,
    ))

    if on_progress is not None:
        last_done = -1
        try:
            while not edit_task.done():
                await asyncio.sleep(2.0)
                done, total = progress["done"], progress["total"]
                if total and done != last_done:
                    last_done = done
                    try:
                        await on_progress(done, total)
                    except Exception:
                        logger.exception("on_progress callback raised — ignoring")
        except asyncio.CancelledError:
            edit_task.cancel()
            raise

    new_path = await edit_task
    logger.info("[docgen-runner] divergent-section edit complete job_id=%s new_path=%s", job_id, new_path)
    return new_path


# ── Markdown renderer for the platform storage / validation / UI display ────────────

def _md_cell(value) -> str:
    """Sanitize a table cell for GitHub-flavored markdown: a literal pipe or a
    newline inside a cell breaks the row, so escape the pipe and fold newlines
    into <br>. Keeps rendered tables well-formed regardless of cell content."""
    s = "" if value is None else str(value)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>").strip()


def sections_to_markdown(plan: dict, sections: list[dict]) -> str:
    """Flatten the docgen pipeline's structured `generated_sections.json` into markdown.

    The platform stores BRD / TSD / etc. as markdown strings in DB and renders them
    via ReactMarkdown. The .docx is the rich version (cover, TOC, embedded
    diagrams) — this markdown is just the textual rendition.
    """
    plan = plan or {}
    sections = sections or []
    out: list[str] = []

    title = (plan.get("title") or "").strip()
    subtitle = (plan.get("subtitle") or "").strip()
    if title:
        out.append(f"# {title}")
    if subtitle:
        out.append(f"*{subtitle}*")
    if title or subtitle:
        out.append("")

    for sec in sections:
        if sec.get("render_style") == "cover":
            # Cover-page metadata is rendered by .docx, not echoed in markdown
            continue

        level = max(1, int(sec.get("level", 1)))
        heading = (sec.get("section_heading") or "").strip()
        if heading:
            out.append("")
            out.append(f"{'#' * (level + 1)} {heading}")
            out.append("")

        # All list-fields tolerate either str entries or dicts (LLM drift)
        def _coerce_str(x):
            if isinstance(x, str):
                return x.strip()
            if isinstance(x, dict):
                return str(x.get("text") or x.get("content") or x.get("value") or "").strip()
            return str(x).strip() if x is not None else ""

        for para in sec.get("paragraphs", []) or []:
            text = _coerce_str(para)
            if text:
                out.append(text)
                out.append("")

        bullets = [t for t in (_coerce_str(b) for b in (sec.get("bullet_points") or [])) if t]
        for b in bullets:
            out.append(f"- {b}")
        if bullets:
            out.append("")

        numbered = [t for t in (_coerce_str(n) for n in (sec.get("numbered_items") or [])) if t]
        for i, n in enumerate(numbered, 1):
            out.append(f"{i}. {n}")
        if numbered:
            out.append("")

        td = sec.get("table_data")
        if isinstance(td, dict) and td.get("headers"):
            headers = td["headers"]
            out.append("| " + " | ".join(_md_cell(h) for h in headers) + " |")
            out.append("|" + "|".join(["---"] * len(headers)) + "|")
            for row in td.get("rows", []) or []:
                if isinstance(row, dict):
                    # row keyed by header name → align by header order
                    cells = [str(row.get(h, "")) for h in headers]
                else:
                    cells = [str(c) if c is not None else "" for c in (row or [])]
                while len(cells) < len(headers):
                    cells.append("")
                cells = cells[: len(headers)]
                out.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
            out.append("")

        # LLM sometimes returns code_blocks as a list of plain strings
        # instead of the expected list of {language, code} dicts. Tolerate both.
        for cb in sec.get("code_blocks", []) or []:
            if isinstance(cb, str):
                lang, code = "", cb.strip()
            elif isinstance(cb, dict):
                lang = cb.get("language", "") or ""
                code = (cb.get("code") or "").strip()
            else:
                continue
            if code:
                out.append(f"```{lang}")
                out.append(code)
                out.append("```")
                out.append("")

    return "\n".join(out).strip() + "\n"
