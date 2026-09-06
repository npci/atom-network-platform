# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agent API — WebSocket streaming + REST history endpoints.

WebSocket protocol (JSON messages):
  Client → Server:  {"message": "<user text>"}
  Server → Client:  {"type": "chunk",  "text": "..."}       — streaming token
                    {"type": "done",   "full": "...",
                     "ready": true, "enhanced_prompt": "..."} — turn complete
                    {"type": "error",  "detail": "..."}       — error
"""
import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.deps import DbDep, CurrentUser, AdminUser, authenticate_ws
from app.core.error_taxonomy import client_safe_detail
from app.core.config import settings as _app_settings
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.conversation import Conversation, ConversationModule, MessageRole
from app.models.research import ResearchOutput, ArtifactStatus
from app.models.canvas import ProductCanvas
from app.models.brd import BRD, BRDStatus
from app.models.approval import Approval, ApprovalStatus, ApprovalArtifactType
from app.models.user import User, UserRole
from app.agents.prompt_enhancer import (stream_enhancer_turn, normalize_enhancer_response,
                                        generate_change_title)
from app.agents.deep_researcher import stream_research_turn
from app.agents.canvas import stream_canvas_turn, generate_canvas_docx
from app.agents.xsd import stream_xsd_assessment, stream_xsd_turn
from app.agents.product_kit_agent import (
    stream_product_kit_doc,
    generate_video_script, render_video_script_markdown,
)
from app.agents.document_validator import validate as validate_doc, summarize as summarize_validation
from app.models.tech_spec import TechSpec
from app.models.xsd import XSD, XSDStatus
from app.models.product_kit import ProductKitDocument, ProductKitDocType, active_doc_types
from app.services.product_kit_query import (
    kit_docs_at_version, kit_versions, latest_kit_doc, latest_kit_docs,
)
from app.core.database import SessionLocal
from app.services.evaluation.checkpoints import CheckpointId
from app.services.evaluation.contracts import get_contract
from app.services.evaluation.policy import decide_gate, get_policy_mode
from app.services.evaluation.runner import fire_advisory_eval, run_advisory
from app.services.evaluation.store import count_runs, get_latest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _assemble_docx_safe(
    markdown: str,
    *,
    change_id: str,
    doc_type: str,
    version: int,
    subtype: str | None = None,
    cr_title: str | None = None,
) -> str | None:
    """Best-effort DOCX build. Returns file path string or None on failure.

    Kept deliberately wrapped so DOCX problems never break document generation.
    """
    try:
        from app.services.docx_assembler import build_docx_from_markdown, artifact_path
        out_path = artifact_path(change_id, doc_type, version=version, subtype=subtype)
        build_docx_from_markdown(
            markdown,
            title=cr_title or doc_type,
            subtitle=(subtype or doc_type) if subtype else (cr_title or ""),
            doc_type=doc_type,
            doc_subtype=subtype,
            version=str(version),
            output_path=out_path,
        )
        logger.info("DOCX assembled: change=%s doc_type=%s subtype=%s path=%s",
                    change_id, doc_type, subtype, out_path)
        return str(out_path)
    except Exception as e:
        logger.warning("DOCX assembly failed for change=%s doc_type=%s: %s",
                       change_id, doc_type, e)
        return None


# Blueprint TSD headings that carry the four sections the certification test-case
# engine needs as primary grounding. Interface Specification rides along so the
# Writer sees the full field-level API contract (typical: ~3-15 KB combined).
# Values are the normalised keys engine agents look up in options["tsd_sections"].
# Headings must match the ones emitted by app/docgen/document_guides.py.
_TSD_SECTION_KEYS: dict[str, str] = {
    "Control Flow & Sequence":       "control_flow",
    "Failure Handling & Resilience": "failure_handling",
    "Error & Response Handling":     "error_handling",
    "Testing & Verification":        "testing_verification",
    "Interface Specification":       "interface_spec",
}


def _engine_error_detail(exc: BaseException) -> str:
    """Operator-facing detail for an excel-engine failure.

    Engine failures are normally collapsed to "An internal error occurred" so
    tracebacks and internals never reach the client. ``ConfigurationError`` is
    the deliberate exception: it is raised only from our own precondition
    checks, its message is author-written (no paths, no exception reprs), and
    it names the remedy — e.g. which archetype the document can actually
    support. Collapsing it threw away the one actionable thing we knew, which
    is how an unsatisfiable-archetype run spent three LLM attempts and still
    told the operator nothing.
    """
    from app.excel_testcase_engine.observability import ConfigurationError

    if isinstance(exc, ConfigurationError):
        return str(exc)
    return "An internal error occurred"


async def _load_engine_scope_context(change_id: str, db: Session) -> dict:
    """Load TSD section splits for the excel testcase engine.

    Called from the cert_test_cases WS branches (single-doc + batched). Returns
    a dict ready to spread into `options` for run_workflow / run_workflow_for_ws:

        {"tsd_sections": {interface_spec: str, testing_verification: str, ...}}

    BRD/TSD-only refactor: previously this helper also loaded PM scope signals,
    BRD FRs, feature criteria, and the XSD diff. The engine now trusts the
    BRD (embedded in the brief) and the TSD as the only sources of truth.
    """
    from app.agents.context_assembler import doc_sections

    result: dict = {"tsd_sections": {}}
    try:
        _tsd_row = (
            db.query(TechSpec)
            .filter(TechSpec.change_request_id == change_id)
            .order_by(TechSpec.version.desc())
            .first()
        )
        if _tsd_row and _tsd_row.content:
            _raw_sections = doc_sections(_tsd_row.content)
            result["tsd_sections"] = {
                norm: _raw_sections[heading]
                for heading, norm in _TSD_SECTION_KEYS.items()
                if heading in _raw_sections
            }
            logger.info(
                "_load_engine_scope_context: tsd_sections change=%s keys=%s",
                change_id, sorted(result["tsd_sections"].keys()),
            )
        else:
            logger.info(
                "_load_engine_scope_context: no TSD row for change=%s",
                change_id,
            )
    except Exception:
        logger.exception(
            "_load_engine_scope_context: load tsd_sections failed change=%s",
            change_id,
        )
    return result


def _render_deck_pptx_safe(
    full_response: str,
    *,
    change_id: str,
    version: int,
) -> tuple[str, str | None]:
    """Split a product_deck LLM response and render the .pptx companion.

    Returns ``(markdown_only, pptx_path_or_None)``. The markdown has the
    trailing JSON fence stripped so the .docx renderer (downstream)
    produces a clean script. ``pptx_path`` is None when:
      * the LLM didn't emit a JSON fence (legacy / pre-D5 responses), or
      * the JSON failed schema validation, or
      * the renderer / graphviz raised.
    All three cases are non-fatal — the Product Kit ships docx-only
    with a WARN in the log; we never break Product Kit gen on a deck
    failure.
    """
    from pathlib import Path
    from app.docgen.deck.parse import split_markdown_and_json
    from app.docgen.deck.renderer import render

    markdown, outline = split_markdown_and_json(full_response)
    if outline is None:
        return markdown, None

    try:
        from app.services.docx_assembler import artifact_path
        pptx_out = artifact_path(
            change_id, "Product Kit", version=version, subtype="product_deck",
        ).with_suffix(".pptx")
        Path(pptx_out).parent.mkdir(parents=True, exist_ok=True)
        render(outline, pptx_out)
        logger.info(
            "PPTX rendered: change=%s version=%d slides=%d path=%s",
            change_id, version, len(outline.slides), pptx_out,
        )
        return markdown, str(pptx_out)
    except Exception as exc:  # noqa: BLE001 — deck failures never break Product Kit
        logger.warning(
            "PPTX render failed for change=%s version=%d: %s — shipping docx-only",
            change_id, version, exc,
        )
        return markdown, None


def _get_change_or_404(change_id: str, db: Session) -> ChangeRequest:
    cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    return cr


def _load_conversation(change_id: str, module: ConversationModule, db: Session) -> list[dict]:
    """Load conversation history for a module as Claude-API message dicts."""
    rows = (
        db.query(Conversation)
        .filter(
            Conversation.change_request_id == change_id,
            Conversation.module == module,
        )
        .order_by(Conversation.created_at)
        .all()
    )
    return [{"role": r.role.value, "content": r.content} for r in rows]


def _save_message(
    change_id: str,
    module: ConversationModule,
    role: MessageRole,
    content: str,
    user_id: str | None,
    db: Session,
) -> None:
    msg = Conversation(
        change_request_id=change_id,
        module=module,
        role=role,
        content=content,
        created_by=user_id,
    )
    db.add(msg)
    db.commit()


def _artifact_ids(*rows: object) -> list[str]:
    """Collect non-empty .id values from ORM rows."""
    ids: list[str] = []
    for row in rows:
        value = getattr(row, "id", None)
        if value:
            ids.append(str(value))
    return ids


async def _run_advisory_with_isolated_session(
    *,
    change_request_id: str,
    checkpoint_id: CheckpointId,
    source_artifacts: dict[str, dict],
    target_artifacts: dict[str, dict],
    source_artifact_ids: list[str] | None = None,
    target_artifact_ids: list[str] | None = None,
) -> None:
    """Run evaluation with a fresh DB session for task safety."""
    eval_db: Session = SessionLocal()
    try:
        await run_advisory(
            db=eval_db,
            change_request_id=change_request_id,
            checkpoint_id=checkpoint_id,
            source_artifacts=source_artifacts,
            target_artifacts=target_artifacts,
            source_artifact_ids=source_artifact_ids or [],
            target_artifact_ids=target_artifact_ids or [],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Advisory evaluation task failed checkpoint=%s change=%s error=%s",
            checkpoint_id.value,
            change_request_id,
            exc,
        )
    finally:
        eval_db.close()


def _ratified_scope_block(change_id: str, db: Session) -> str:
    """The ratified Change-Analysis SCOPE (chosen approach + functional plan) as a binding
    block. Threading this into every downstream doc is the point of planning-before-BRD:
    the whole chain flows from ONE consistent set of decisions instead of each stage
    re-deriving (and over-scoping) the feature vision. Empty on the legacy flow."""
    try:
        from app.models.change_analysis import ChangeAnalysis
        from app.models.agentic import AgenticRun
        ca = (db.query(ChangeAnalysis).filter(ChangeAnalysis.change_request_id == change_id)
              .order_by(ChangeAnalysis.version.desc()).first())
        run = (db.query(AgenticRun)
               .filter(AgenticRun.change_request_id == change_id, AgenticRun.kind.in_(("analysis", "xsd")))
               .order_by(AgenticRun.created_at.desc()).first())
        approach = ((getattr(run, "handoff_json", None) or {}).get("approach_decision") or {}).get("option") or {}
        fp = (ca.functional_plan if ca else None) or {}
        if not approach and not fp:
            return ""
        lines = ["RATIFIED CHANGE SCOPE (BINDING — stay WITHIN this ratified scope; anything broader is "
                 "future/out-of-scope and must be labeled as such, never presented as part of THIS change):"]
        if approach.get("title"):
            lines.append(f"- Chosen approach: {approach['title']}"
                         + (f" — {approach['how_it_fits']}" if approach.get("how_it_fits") else ""))
        if approach.get("target_api"):
            lines.append(f"- Target: {approach['target_api']}")
        if fp.get("overview"):
            lines.append(f"- Plan overview: {fp['overview']}")
        for s in (fp.get("steps") or [])[:12]:
            # Step dicts vary in shape — the ratified plan uses {id, title, description}, not
            # a `text` key. Falling straight to s.get("text") yielded None, and `"  • " + None`
            # raised TypeError → the WHOLE ratified-scope block was dropped (except → ""), so the
            # BRD/TSD was generated blind to the plan and then had to be auto-corrected. Resolve
            # a label across the known shapes instead.
            if isinstance(s, str):
                txt = s
            elif isinstance(s, dict):
                txt = s.get("text") or s.get("title") or s.get("description") or str(s)
            else:
                txt = str(s)
            lines.append("  • " + txt)
        # The AUTHORITATIVE wire/API surface — so no downstream doc invents an API/message the plan
        # never defined. The single most common BRD/TSD failure is coining a Req/Resp name (or a "NEW
        # API" row) for an INTERNAL operation (DB/cache read, Kafka emit on an existing topic, config
        # read, inter-service call). State the surface explicitly, including the empty/internal case.
        ta = (ca.technical_analysis if ca else None) or {}
        dmc = ta.get("data_model_changes")
        ad = ta.get("approach_decision") or {}
        if ta.get("data_model_changes_superseded_by_approach_decision"):
            # The human's approach-gate choice discarded the pre-gate surface — don't bind the doc
            # to a wire/API it no longer uses (and don't invent the new one).
            lines.append("- WIRE/API & SCHEMA SURFACE: the pre-gate surface was SUPERSEDED by the human's "
                         "chosen approach"
                         + (f" ({ad.get('approach')}"
                            + (f" {ad['target_api']}" if ad.get("target_api") else "") + ")"
                            if ad.get("approach") else "")
                         + " — describe ONLY what that approach requires; introduce NO new UPI wire message "
                           "or NPCI API the chosen approach does not, and give NO Req/Resp name to an "
                           "internal operation.")
        elif dmc:
            lines.append("- WIRE/API & SCHEMA SURFACE (the COMPLETE set — introduce NO UPI wire message, "
                         "schema, or NPCI API beyond this; an internal DB/cache/Kafka/config operation is "
                         "NOT a new API and must never be given a Req/Resp name): " + str(dmc)[:1400])
        else:
            lines.append("- WIRE/API & SCHEMA SURFACE: NONE. This change adds NO new UPI wire message, NO "
                         "schema/XSD change, and NO new NPCI API — it is PSP-INTERNAL. Every doc must say "
                         "so plainly and must NOT coin a Req/Resp API name for any internal operation.")
        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception as e:  # noqa: BLE001
        logger.warning("ratified-scope block unavailable for change=%s (%s)", change_id, e)
        return ""


def _decisions_block(change_id: str, db: Session) -> str:
    """Binding human-ratified Decision Ledger block (clarification answers + plan
    ratification) PLUS the ratified change scope — so BRD, XSD, TSD, Product Kit and
    code-gen all flow from the same planning decisions. Empty on the legacy flow."""
    try:
        from app.services.decision_ledger import build_decisions_block
        ledger = build_decisions_block(change_id, db)
    except Exception as e:  # noqa: BLE001
        logger.warning("decision ledger unavailable for change=%s (%s)", change_id, e)
        ledger = ""
    # Ratified scope FIRST: it carries the binding wire/API-surface guard (the anti-invention
    # rule), which must survive the downstream length cap on this block even when the
    # clarification ledger is long.
    return "\n\n".join(b for b in (_ratified_scope_block(change_id, db), ledger) if b)


def _ratified_plan_block(change_id: str, db: Session) -> str:
    """The RATIFIED functional plan — the human-approved solution narrative (overview, plan
    steps, backward-compatibility, assumptions) from the latest ChangeAnalysis. Fed into BRD
    generation as AUTHORITATIVE context so the BRD is written FROM the ratified plan, not only
    the enriched prompt (the enriched prompt still supplies business/research/canvas detail).
    Business altitude — no implementation identifiers (that is the TSD's `_tech_design_block`).
    Empty on the legacy flow or before a plan exists."""
    try:
        from app.models.change_analysis import ChangeAnalysis
        ca = (db.query(ChangeAnalysis).filter(ChangeAnalysis.change_request_id == change_id)
              .order_by(ChangeAnalysis.version.desc()).first())
        fp = (ca.functional_plan or {}) if ca else {}
        if not fp:
            return ""

        def _bullets(items, limit):
            out = []
            for it in (items or [])[:limit]:
                t = it if isinstance(it, str) else (it.get("text") or it.get("statement") or "")
                t = str(t).strip()
                if t:
                    out.append(f"  - {t}")
            return out

        parts: list[str] = []
        overview = str(fp.get("overview") or "").strip()
        if overview:
            parts.append(overview)
        steps = _bullets(fp.get("steps"), 30)
        if steps:
            parts.append("Plan steps:\n" + "\n".join(steps))
        compat = fp.get("compatibility")
        if isinstance(compat, str) and compat.strip():
            parts.append(f"Backward compatibility: {compat.strip()}")
        elif isinstance(compat, dict) and compat:
            cl = [f"  - {str(k).replace('_', ' ')}: {v}" for k, v in compat.items() if v]
            if cl:
                parts.append("Backward compatibility:\n" + "\n".join(cl))
        assumptions = _bullets(fp.get("assumptions"), 20)
        if assumptions:
            parts.append("Ratified assumptions:\n" + "\n".join(assumptions))
        return "\n\n".join(parts) if parts else ""
    except Exception as e:  # noqa: BLE001 — never block generation on plan assembly
        logger.warning("ratified-plan block failed for change=%s (%s)", change_id, e)
        return ""


def _flow_spec(change_id: str, db: Session) -> dict:
    """Shared machine-readable flow spec from the latest ChangeAnalysis (accuracy:
    shared-spec diagrams). Both the BRD and the TSD render their flow diagrams from
    THIS, so they can't deviate. Empty when no analysis ran → diagrams fall back to
    the per-doc blueprint description (legacy behaviour)."""
    try:
        from app.models.change_analysis import ChangeAnalysis
        ca = (db.query(ChangeAnalysis).filter(ChangeAnalysis.change_request_id == change_id)
              .order_by(ChangeAnalysis.version.desc()).first())
        return (ca.flow_spec or {}) if ca else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("flow_spec unavailable for change=%s (%s)", change_id, e)
        return {}


def _tech_design_block(change_id: str, db: Session) -> str:
    """The RATIFIED, code-grounded technical design from the latest ChangeAnalysis — concrete
    files+intents (class/method design), data-model/keyspace changes, and existing code to reuse —
    so the TSD SPECIFIES the decided implementation (real classes, methods, data structures, config
    keys, response codes) instead of hand-waving or inventing wire codes. Empty on the legacy flow."""
    try:
        from app.models.change_analysis import ChangeAnalysis
        ca = (db.query(ChangeAnalysis).filter(ChangeAnalysis.change_request_id == change_id)
              .order_by(ChangeAnalysis.version.desc()).first())
        if not ca:
            return ""
        ta = ca.technical_analysis or {}
        parts: list[str] = [
            "RATIFIED TECHNICAL DESIGN — the TSD MUST specify EXACTLY this implementation (the real "
            "classes, methods, data structures, config keys, and response codes named below). Use these "
            "identifiers VERBATIM — copy the exact method names, key strings, operations, and response "
            "codes; do NOT rename them to cleaner synonyms (if it says SET NX write SET NX; if it says a "
            "DUP_PAYMENT/decline code write that exact code, never a prettier 'DUPLICATE_DECLINE'; name the "
            "real injection method, e.g. FinController.handleReqTransferRequest). Do NOT hand-wave, do NOT invent "
            "wire/NPCI error codes the design did not define, and do NOT contradict it across sections."]
        # Read every key/field variant the analysis agent has emitted — reading only
        # `per_file_changes` silently dropped the whole section for a plan that stored its
        # list under `file_change_list`, shipping "Do NOT hand-wave" with no files behind it.
        from app.agents.plan_files import plan_file_entries
        lines = []
        for path, p in plan_file_entries(ta)[:20]:
            lines.append(f"  - [{p.get('action') or p.get('op') or 'edit'}] {path}: "
                         f"{str(p.get('intent') or '')[:300]}")
        if lines:
            parts.append("COMPONENT / CLASS DESIGN (the files this change adds/edits + each one's job — "
                         "name these in the TSD):\n" + "\n".join(lines))
        ad = ta.get("approach_decision") or {}
        if ta.get("data_model_changes_superseded_by_approach_decision"):
            # The pre-gate data model was discarded at the approach gate — specify the chosen
            # path, not the superseded structures (else the TSD details a rejected design).
            parts.append("DATA MODEL / KEYSPACE — the pre-gate data model was SUPERSEDED at the approach "
                         "gate by the human's chosen path"
                         + (f" ({ad.get('approach')}"
                            + (f" {ad['target_api']}" if ad.get("target_api") else "") + ")"
                            if ad.get("approach") else "")
                         + ". Implement per that decision; do NOT specify the superseded structures."
                         + (f" Rationale: {ad['why']}" if ad.get("why") else ""))
        elif ta.get("data_model_changes"):
            parts.append("DATA MODEL / KEYSPACE (the COMPLETE set — describe these data structures, keys, "
                         "TTLs; introduce nothing beyond them):\n" + str(ta["data_model_changes"])[:1600])
        if ta.get("reuse_findings"):
            parts.append("EXISTING CODE TO REUSE (name these real classes/methods as the integration "
                         "points):\n" + str(ta["reuse_findings"])[:1400])
        if ta.get("constraints"):
            parts.append("ENGINEERING CONSTRAINTS (honour in the design):\n" + str(ta["constraints"])[:900])
        return "\n\n".join(parts) if len(parts) > 1 else ""
    except Exception as e:  # noqa: BLE001 — best-effort; never break TSD generation
        logger.warning("tech_design_block unavailable for change=%s (%s)", change_id, e)
        return ""


def _format_xsd_change_summary(diff_record: dict) -> str:
    """A compact element-level summary of the realized schema change (added/modified/
    deprecated per file), prepended to the source schema so the EXACT change is always
    visible to the TSD even when the full schema body is truncated downstream."""
    if not diff_record:
        return ""

    def _names(items):
        out = []
        for i in (items or []):
            out.append(str(i.get("name") or i.get("path") or i) if isinstance(i, dict) else str(i))
        return ", ".join(out[:25])

    lines = ["<!-- REALIZED SCHEMA CHANGE (this change ONLY — the TSD must not describe schema beyond this) -->"]
    for path, rec in diff_record.items():
        if not isinstance(rec, dict):
            continue
        parts = [f"{label}: {_names(rec.get(key))}"
                 for label, key in (("added", "new"), ("modified", "modified"), ("deprecated", "deprecated"))
                 if rec.get(key)]
        if parts:
            lines.append(f"  {path}: " + "; ".join(parts))
    return ("\n".join(lines) + "\n\n") if len(lines) > 1 else ""


def _latest_xsd_content(change_id: str, db: Session) -> str:
    """Latest approved/generated XSD content for a change (accuracy S6) — so the TSD
    and partner docs cite the REAL schema, not a re-derivation.

    Primary: the legacy ``xsds`` doc-table row. Fallback: the agentic Phase-A run's
    FROZEN realized files (``handoff_json.xsd_files``). The fallback is essential —
    an agentic XSD change lands as a git diff on the run, NEVER in the doc table, so
    without it the TSD generator receives an empty ``source_xsd`` and re-derives the
    schema from the BRD's vision (the "Chinese-whispers" gap where the TSD invents
    APIs/fields the XSD never created). Grounding the TSD in the realized files keeps
    the chain coherent: the TSD can only describe what was actually schema-modeled."""
    try:
        from app.models.xsd import XSD
        row = (db.query(XSD).filter(XSD.change_request_id == change_id)
               .order_by(XSD.version.desc()).first())
        if row and (row.content or "").strip():
            return row.content
    except Exception as e:  # noqa: BLE001
        logger.warning("XSD doc-table lookup failed for change=%s (%s)", change_id, e)
    # Fallback: the agentic XSD run's realized schema files (the ACTUAL change).
    try:
        from app.models.agentic import AgenticRun
        run = (db.query(AgenticRun)
               .filter(AgenticRun.change_request_id == change_id, AgenticRun.kind == "xsd")
               .order_by(AgenticRun.created_at.desc()).first())
        h = (getattr(run, "handoff_json", None) or {}) if run else {}
        files = h.get("xsd_files") or []
        if files:
            body = "\n\n".join(
                f"<!-- {f.get('path', '(file)')} — realized by agentic Phase A -->\n{f.get('content', '')}"
                for f in files if (f.get("content") or "").strip()
            )
            # Prepend the durable, compact change record (xsd_scope.diff_record) so the precise
            # realized change survives downstream truncation of the full schema body.
            summary = _format_xsd_change_summary((h.get("xsd_scope") or {}).get("diff_record") or {})
            return summary + body
    except Exception as e:  # noqa: BLE001
        logger.warning("XSD agentic-run fallback failed for change=%s (%s)", change_id, e)
    return ""


# ── Stage order — versioned (accuracy S5 reorder) ────────────────────────────
# v2 = XSD BEFORE the Tech Spec (so the TSD is authored against approved real schemas)
# and is now THE default flow (workflow_version defaults to 2). v1 = the historical
# order (XSD after the Tech Spec) — kept only so any legacy v1 row still in flight keeps
# the order it actually executed; no new change is ever created on v1. A missing/null
# version is treated as v2.
_STATUS_FLOW_V1 = [s for s in ChangeStatus]  # enum declaration order (unchanged)
_STATUS_FLOW_V2 = [
    ChangeStatus.PROMPT_ENHANCEMENT, ChangeStatus.RESEARCH, ChangeStatus.CANVAS,
    ChangeStatus.CLARIFICATION, ChangeStatus.BRD,
    ChangeStatus.XSD, ChangeStatus.TECH_SPEC,         # ← swapped vs v1
    ChangeStatus.PRODUCT_KIT, ChangeStatus.COMPLETED,
]


def _status_flow(workflow_version: int | None) -> list[ChangeStatus]:
    return _STATUS_FLOW_V2 if (workflow_version or 2) >= 2 else _STATUS_FLOW_V1


def _next_status(current: ChangeStatus, workflow_version: int | None) -> ChangeStatus | None:
    flow = _status_flow(workflow_version)
    try:
        i = flow.index(current)
    except ValueError:
        return None
    return flow[i + 1] if i + 1 < len(flow) else None


def _post_brd_status(workflow_version: int | None) -> ChangeStatus:
    """Stage a change advances to once the BRD is approved: XSD on v2 (default), Tech Spec on legacy v1."""
    return ChangeStatus.XSD if (workflow_version or 2) >= 2 else ChangeStatus.TECH_SPEC


_TRANSITION_GATE_CHECKPOINTS: dict[tuple[ChangeStatus, ChangeStatus], CheckpointId] = {
    # Phase 7 — Phase A full gate coverage. Each entry below evaluates the
    # artifact produced at the from-stage (e.g. on PROMPT_ENHANCEMENT -> RESEARCH
    # we check INITIAL_TO_PROMPT_ENHANCED, which scores the enhanced prompt).
    (ChangeStatus.PROMPT_ENHANCEMENT, ChangeStatus.RESEARCH):       CheckpointId.INITIAL_TO_PROMPT_ENHANCED,
    (ChangeStatus.RESEARCH,           ChangeStatus.CANVAS):         CheckpointId.PROMPT_TO_RESEARCH,
    (ChangeStatus.CANVAS,             ChangeStatus.CLARIFICATION):  CheckpointId.RESEARCH_TO_CANVAS,
    (ChangeStatus.CLARIFICATION,      ChangeStatus.BRD):            CheckpointId.CANVAS_TO_CLARIFICATION,
    (ChangeStatus.BRD,                ChangeStatus.TECH_SPEC):      CheckpointId.CLARIFICATION_TO_BRD,
    # Phase 2 — already-shipped first wave (v1 order: BRD→TSD→XSD→PRODUCT_KIT).
    (ChangeStatus.TECH_SPEC,          ChangeStatus.XSD):            CheckpointId.BRD_TO_TECH_SPEC,
    (ChangeStatus.XSD,                ChangeStatus.PRODUCT_KIT):    CheckpointId.TECH_SPEC_TO_XSD,
    # Accuracy S5 reorder (v2: BRD→XSD→TSD→PRODUCT_KIT). Distinct keys from the v1 entries above,
    # so v1 changes never hit them. Each scores the FROM-stage artifact, reusing the
    # existing contracts: BRD→XSD scores the BRD; TSD→PRODUCT_KIT scores BRD↔TSD.
    # XSD→TECH_SPEC is intentionally ungated for now (no contract scores an XSD with no
    # TSD yet) — advisory only, a dedicated BRD_PLAN_TO_XSD contract is the S5-tail refinement.
    (ChangeStatus.BRD,                ChangeStatus.XSD):            CheckpointId.CLARIFICATION_TO_BRD,
    (ChangeStatus.XSD,                ChangeStatus.TECH_SPEC):      None,  # v2: ungated, advisory only
    (ChangeStatus.TECH_SPEC,          ChangeStatus.PRODUCT_KIT):    CheckpointId.BRD_TO_TECH_SPEC,
}


def _enforce_eval_gate_for_transition(
    *,
    db: Session,
    change_id: str,
    current_status: ChangeStatus,
    next_status: ChangeStatus,
    acknowledged_verdict_id: str | None,
) -> dict | None:
    checkpoint_id = _TRANSITION_GATE_CHECKPOINTS.get((current_status, next_status))
    if checkpoint_id is None:
        return None

    contract = get_contract(checkpoint_id)
    effective_policy = get_policy_mode(db, checkpoint_id, fallback=contract.policy_mode)
    latest_verdict = get_latest(db, change_id, checkpoint_id)
    run_count = count_runs(db, change_id, checkpoint_id, include_overrides=False)
    retries_used = max(run_count - 1, 0)

    decision = decide_gate(
        checkpoint_id=checkpoint_id,
        policy_mode=effective_policy,
        verdict=latest_verdict,
        acknowledged_verdict_id=acknowledged_verdict_id,
        retry_allowed=contract.retry_allowed,
        retries_used=retries_used,
        override_allowed=bool(contract.override_allowed_roles),
    )
    if decision.blocked:
        raise HTTPException(status_code=409, detail=decision.to_debug_dict())
    return decision.to_debug_dict()


def _eval_gate_allows_transition(
    *,
    db: Session,
    change_id: str,
    current_status: ChangeStatus,
    next_status: ChangeStatus,
) -> tuple[bool, dict | None]:
    """Non-raising eval gate check for non-/advance code paths.

    Returns (allowed, gate_detail). When the gate would block (hard_gate FAIL,
    soft_gate WARN without ack, etc.), `allowed` is False and `gate_detail`
    is the same payload the /advance endpoint would 409 with. Callers
    (approval auto-advance, dev auto-approve, dev_skip_approvals) use this
    to refuse status promotion silently so the user must hit /advance
    explicitly and deal with the gate modal (override / retry).
    """
    try:
        info = _enforce_eval_gate_for_transition(
            db=db,
            change_id=change_id,
            current_status=current_status,
            next_status=next_status,
            acknowledged_verdict_id=None,
        )
        return True, info
    except HTTPException as exc:
        if exc.status_code == 409:
            return False, exc.detail
        raise


# ── REST: get conversation history ───────────────────────────────────────────

@router.get("/changes/{change_id}/conversation/{module}")
def get_conversation(change_id: str, module: str, db: DbDep, _: CurrentUser):
    """Return the full conversation history for a module."""
    try:
        mod = ConversationModule(module)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown module: {module}")
    _get_change_or_404(change_id, db)
    rows = (
        db.query(Conversation)
        .filter(
            Conversation.change_request_id == change_id,
            Conversation.module == mod,
        )
        .order_by(Conversation.created_at)
        .all()
    )
    return [
        {
            "id":      r.id,
            "role":    r.role.value,
            "content": r.content,
            "ts":      r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ── REST: advance change status ───────────────────────────────────────────────

class AdvanceRequest(BaseModel):
    enhanced_prompt: str | None = None
    # Skip-step button (UI-driven): when true AND app_env != production, the
    # advance bypasses workflow gates (currently the CLARIFICATION→BRD gate).
    # Defaults to false — clients omitting the field get the prior behaviour.
    force_skip: bool = False
    # Required when a soft-gated checkpoint emits WARN.
    eval_acknowledged_verdict_id: str | None = None


# ── REST: client-side UI config (dev-mode toggle for skip buttons) ────────────


@router.get("/config/ui")
def get_ui_config():
    """Public client config for the React app.

    Currently exposes `dev_mode` so the frontend can decide whether to
    render the per-step "Skip" buttons. Anonymous on purpose — the config
    has no secrets and the SPA loads it before authenticating. Adding new
    UI flags here is the clean pattern; avoid stuffing them into /auth/me
    (which forces a re-login to pick up changes).
    """
    from app.core.domain.contract import repo_roles_of
    from app.core.domain.registry import get_active_pack

    # Repo topology declared by the active domain pack. The SPA's repo-selection
    # screens derive their rule from this instead of hardcoding UPI's core+app
    # pair. EMPTY IS MEANINGFUL, not a failure: it means the domain declares no
    # topology, and both sides then fall back to "at least one repo" — the
    # single-repo default — with the UI showing a "no topology configured"
    # notice. Keep these semantics identical to
    # `app.agents.repo_scope.validate_selection`, the authoritative server-side
    # gate; this payload only lets the client pre-empt the same 400.
    try:
        roles = [r.model_dump() for r in repo_roles_of(get_active_pack())]
    except Exception:  # noqa: BLE001 — a broken pack must not blank the whole SPA config
        logger.exception("config/ui: could not resolve repo_roles; sending []")
        roles = []

    # The roles a partner can certify FOR, from the active pack's cert
    # vocabulary — the SPA's certification-dispatch role picker derives its
    # choices from this instead of hardcoding any domain's party list. Empty
    # is meaningful: the domain scopes no roles, and dispatch runs unscoped.
    try:
        from app.core.domain.contract import cert_vocabulary_of

        cert_roles = [{"key": k, "label": lbl}
                      for k, lbl in cert_vocabulary_of(get_active_pack()).parties()]
    except Exception:  # noqa: BLE001 — same rule as repo_roles above
        logger.exception("config/ui: could not resolve cert_roles; sending []")
        cert_roles = []

    return {
        "dev_mode": (_app_settings.app_env or "").lower() != "production",
        "app_env":  _app_settings.app_env or "development",
        # Configurable external link for the sidebar's "NPCI Simulator"
        # button. Default localhost; overridden via env or admin Config UI
        # on host-mode / staging / prod deployments.
        "authority_simulator_url": _app_settings.authority_simulator_url or "http://localhost:5173",
        "repo_roles": roles,
        "cert_roles": cert_roles,
    }


@router.post("/changes/{change_id}/advance")
def advance_status(change_id: str, payload: AdvanceRequest, db: DbDep, user: CurrentUser):
    """Advance the change request to the next stage.

    Gating rule (Sprint 5): moving from CLARIFICATION → BRD requires the
    latest Clarification row to have status in ('answered', 'skipped').

    Skip-step button (2026-05-05): the UI exposes a "Skip step" button on
    every step page when dev mode is on. Clicking it calls this endpoint
    with `?skip=true` (or `force_skip` in body), which advances the status
    one step WITHOUT enforcing the CLARIFICATION→BRD gate. Skipped advances
    emit a WARNING log so they're visible in audit. Skipping is gated to
    non-production environments via `_app_settings.app_env != "production"`.
    """
    cr = _get_change_or_404(change_id, db)

    next_status = _next_status(cr.status, getattr(cr, "workflow_version", 2))
    if next_status is None:
        raise HTTPException(status_code=400, detail="Already at final stage")

    # Skip-step intent — surfaced from the UI button in non-prod. Tolerated
    # via either query string `?skip=true` or `payload.force_skip=true`.
    is_skip = bool(getattr(payload, "force_skip", False))
    if is_skip and (_app_settings.app_env or "").lower() == "production":
        # In prod, skip is silently downgraded to a normal advance — keeps
        # the API surface stable while preventing accidental skips when env
        # flips. The button shouldn't be rendered in prod anyway, but defence
        # in depth.
        is_skip = False
        logger.warning(
            "Skip-step request ignored in production: change=%s status=%s",
            change_id, cr.status.value,
        )

    # Gate: CLARIFICATION must be resolved before advancing to BRD.
    # The skip-step button bypasses this gate because the caller explicitly
    # acknowledged they're skipping the stage (they pressed Skip, not Next).
    # Accuracy flow: a ratified Change-Analysis plan satisfies the clarification gate
    # (the analysis stage subsumes clarification). Checked first so the new flow
    # doesn't require a legacy Clarification row.
    _analysis_satisfies = False
    if cr.status == ChangeStatus.CLARIFICATION and next_status == ChangeStatus.BRD and not is_skip:
        try:
            from app.models.change_analysis import ChangeAnalysis
            _ca = (db.query(ChangeAnalysis)
                   .filter(ChangeAnalysis.change_request_id == change_id)
                   .order_by(ChangeAnalysis.version.desc()).first())
            _analysis_satisfies = bool(_ca and _ca.status == "ratified")
        except Exception:  # noqa: BLE001
            _analysis_satisfies = False

    if (
        cr.status == ChangeStatus.CLARIFICATION
        and next_status == ChangeStatus.BRD
        and not is_skip
        and not _analysis_satisfies
    ):
        from app.models.clarification import Clarification
        latest = (
            db.query(Clarification)
            .filter(Clarification.change_request_id == change_id)
            .order_by(Clarification.version.desc())
            .first()
        )
        if not latest:
            # Legacy clarification is gone — the agentic Change Analysis is the path now.
            raise HTTPException(
                status_code=400,
                detail="Complete the Change Analysis (answer the questions and ratify the plan), "
                       "or skip the step, before proceeding to BRD.",
            )
        if latest.status not in ("answered", "skipped"):
            remaining = len(latest.blocking_gap_keys or [])
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Clarification incomplete — {remaining} blocking question(s) still need answers. "
                    f"Submit answers before advancing to BRD."
                ),
            )

    gate_info = _enforce_eval_gate_for_transition(
        db=db,
        change_id=change_id,
        current_status=cr.status,
        next_status=next_status,
        acknowledged_verdict_id=payload.eval_acknowledged_verdict_id,
    )

    if is_skip:
        logger.warning(
            "Skip-step (UI button): change=%s user=%s %s → %s",
            change_id, getattr(user, "username", "?"),
            cr.status.value, next_status.value,
        )
    else:
        logger.info("Status advanced: change=%s %s → %s", change_id, cr.status.value, next_status.value)
    cr.status = next_status

    if payload.enhanced_prompt:
        cr.enhanced_prompt = payload.enhanced_prompt
        # Same title regeneration as the WS enhance path (this REST route is the
        # enhancement-acceptance fallback): the title becomes AI-generated and
        # value-neutral so a user-typed value can't outlive later ratification.
        #
        # Runs on the HOST event loop via anyio.from_thread (this sync route executes in
        # Starlette's `to_thread` worker, so that call is available and blocks this thread
        # until the coroutine completes). NOT asyncio.run: every provider client in
        # core/llm.py is a module-level @lru_cache AsyncAnthropic/AsyncOpenAI, and its
        # httpx pool binds to the loop that first used it. asyncio.run would drive that
        # shared client from a second loop and then CLOSE that loop — poisoning the cached
        # client for the whole process, not merely failing here. Fail-open.
        try:
            from functools import partial as _partial

            import anyio.from_thread as _from_thread
            _new_title = _from_thread.run(
                _partial(generate_change_title, payload.enhanced_prompt,
                         fallback=cr.initial_prompt))
            if _new_title and _new_title != (cr.title or ""):
                logger.info("advance: title regenerated change=%s %r → %r",
                            change_id, cr.title, _new_title)
                cr.title = _new_title
        except Exception as e:  # noqa: BLE001 — the typed title stays
            logger.warning("advance: title regeneration failed (%s)", e)

    db.commit()
    return {
        "status": next_status.value,
        "id": cr.id,
        "skipped": is_skip,
        "gate": gate_info,
    }


# ── REST: get research output ─────────────────────────────────────────────────

@router.get("/changes/{change_id}/research")
def get_research(change_id: str, db: DbDep, _: CurrentUser):
    """Return the latest research output for a change request."""
    _get_change_or_404(change_id, db)
    row = (
        db.query(ResearchOutput)
        .filter(ResearchOutput.change_request_id == change_id)
        .order_by(ResearchOutput.version.desc())
        .first()
    )
    if not row:
        return {"report": None, "version": 0}
    return {
        "id":      row.id,
        "report":  row.combined_report,
        "version": row.version,
        "status":  row.status.value,
    }


# ── WebSocket: Prompt Enhancer ────────────────────────────────────────────────

@router.websocket("/ws/changes/{change_id}/enhance")
async def ws_enhance(websocket: WebSocket, change_id: str):
    """
    WebSocket endpoint for the Prompt Enhancer.

    The client sends {"message": "<user text>"} and receives streaming chunks
    until a {"type": "done", ...} message closes the turn.
    """
    await websocket.accept()
    # Attribute every LLM call in this change-pipeline handler to the change so the Usage
    # dashboard groups it under the right flow (not 'other'). Task-local contextvar — each
    # WS connection is its own asyncio task, so this never leaks across connections.
    try:
        from app.core.observability import set_usage_context as _set_usage_ctx
        _set_usage_ctx(change_request_id=change_id)
    except Exception:
        pass
    logger.info("WS enhance connected: change=%s", change_id)
    db: Session = SessionLocal()
    user = None

    try:
        # Authenticate via token in first message
        auth_msg = await websocket.receive_text()
        auth_data = json.loads(auth_msg)
        token = auth_data.get("token", "")
        user = authenticate_ws(websocket, db, token)
        if not user:
            logger.warning("WS enhance auth failed: change=%s", change_id)
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            return
        logger.info("WS enhance auth ok: change=%s user=%s", change_id, user.username)

        cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        if not cr:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Not found"}))
            return

        # Send existing conversation so frontend can restore state
        history = _load_conversation(change_id, ConversationModule.PROMPT_ENHANCER, db)
        await websocket.send_text(json.dumps({
            "type": "history",
            "messages": history,
            "ready": cr.enhanced_prompt is not None,
            "enhanced_prompt": cr.enhanced_prompt,
        }))
        logger.info("WS enhance history sent: change=%s messages=%d", change_id, len(history))

        # R-4 — surface any active job for this (change_id, enhance) so the
        # client can show the resume banner and request chunk replay.
        from app.services import job_registry
        active = job_registry.get_active_jobs(
            db, change_request_id=change_id, module="enhance",
        )
        if active:
            await websocket.send_text(json.dumps({"type": "active_jobs", "jobs": active}))

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            # R-4 — replay protocol (mirrors R-3 BRD).
            if data.get("type") == "replay_request":
                rep_job_id = (data.get("job_id") or "").strip()
                rep_since  = int(data.get("since_seq") or 0)
                if not rep_job_id:
                    continue
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await websocket.send_text(json.dumps({
                    "type":     "replay",
                    "job_id":   rep_job_id,
                    "since_seq": rep_since,
                    "chunks":   [{"seq": s, "text": t} for (s, t) in chunks],
                    "count":    len(chunks),
                }))
                continue

            user_msg = (data.get("message") or "").strip()
            if not user_msg:
                continue
            logger.info("WS enhance user message: change=%s len=%d", change_id, len(user_msg))

            # Save user message
            _save_message(
                change_id, ConversationModule.PROMPT_ENHANCER,
                MessageRole.USER, user_msg, user.id, db,
            )

            # Reload history (includes the message we just saved)
            history = _load_conversation(change_id, ConversationModule.PROMPT_ENHANCER, db)
            # The last entry is the user message we just added — pass everything before it
            prior_history = history[:-1]

            # A prompt already exists → this turn is a refinement request, not an
            # answer to a clarifying question. Read it before the turn so the
            # rewrite below can't flip the flag mid-stream.
            refining = cr.enhanced_prompt is not None

            # R-4 — register the durable job before streaming.
            registry_job_id = job_registry.create_job(
                db,
                change_request_id=change_id,
                module="enhance",
                started_by_user_id=user.id,
                metadata={"trigger": user_msg[:120]},
            )
            await websocket.send_text(json.dumps({
                "type":   "job_id",
                "job_id": registry_job_id,
                "module": "enhance",
            }))
            enhance_milestones = [
                (0,    "Enhancing prompt",             10),
                (800,  "Adding context & constraints", 50),
                (2000, "Finalizing enhanced prompt",   85),
            ]
            stage_idx = job_registry.advance_stage_by_chars(
                db, registry_job_id, 0, enhance_milestones, 0,
            )

            # Stream assistant response
            logger.info("WS enhance streaming started: change=%s", change_id)
            full_response = ""
            try:
                from app.services.source_material import source_block
                async for chunk in stream_enhancer_turn(prior_history, user_msg,
                                                        source_material=source_block(cr),
                                                        refining=refining):
                    full_response += chunk
                    await job_registry.ws_send_chunk(websocket, registry_job_id, chunk)
                    stage_idx = job_registry.advance_stage_by_chars(
                        db, registry_job_id, len(full_response), enhance_milestones, stage_idx,
                    )
                logger.info("WS enhance streaming done: change=%s response_len=%d", change_id, len(full_response))
            except Exception as exc:
                logger.exception("WS enhance streaming failed")
                job_registry.fail_job(db, registry_job_id, error=str(exc))
                await websocket.send_text(json.dumps({
                    "type": "error", "detail": "An internal error occurred", "job_id": registry_job_id,
                }))
                continue

            full_response, enhanced = normalize_enhancer_response(full_response)

            # Save assistant response
            _save_message(
                change_id, ConversationModule.PROMPT_ENHANCER,
                MessageRole.ASSISTANT, full_response, None, db,
            )

            # Check if prompt is ready
            if enhanced:
                logger.info("WS enhance prompt ready: change=%s prompt_len=%d", change_id, len(enhanced))
                cr.enhanced_prompt = enhanced
                # The title becomes AI-GENERATED at this point (value-neutral, from the
                # enhanced ask). The user-typed title is a hallucination-injection vector:
                # it survives as the BRD heading / MR slug / cert feature_name long after
                # clarification supersedes any value it embeds (the BT/80 incident's
                # surviving "80" was exactly this). Fail-open — on LLM failure the typed
                # title stays.
                _new_title = await generate_change_title(enhanced, fallback=cr.initial_prompt)
                if _new_title and _new_title != (cr.title or ""):
                    logger.info("WS enhance: title regenerated change=%s %r → %r",
                                change_id, cr.title, _new_title)
                    cr.title = _new_title
                db.commit()

                # Phase 7: advisory eval on the enhanced prompt
                fire_advisory_eval(
                    change_request_id=change_id,
                    checkpoint_id=CheckpointId.INITIAL_TO_PROMPT_ENHANCED,
                    source_artifacts={
                        "initial_prompt": {
                            "type": "initial_prompt",
                            "content": cr.initial_prompt or "",
                        },
                    },
                    target_artifacts={
                        "enhanced_prompt": {
                            "type": "enhanced_prompt",
                            "content": enhanced,
                        },
                    },
                )

            job_registry.complete_job(
                db, registry_job_id,
                result={"markdown_chars": len(full_response), "ready": bool(enhanced)},
                final_stage="Prompt enhanced" if enhanced else "Awaiting more detail",
            )
            await websocket.send_text(json.dumps({
                "type":             "done",
                "full":             full_response,
                "ready":            enhanced is not None,
                "enhanced_prompt":  enhanced,
                "job_id":           registry_job_id,
            }))

    except WebSocketDisconnect:
        logger.info("WS enhance disconnected: change=%s", change_id)
    except Exception as e:
        logger.exception("WS enhance error: change=%s", change_id)
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": "An internal error occurred"}))
        except Exception:
            pass
    finally:
        db.close()


# ── WebSocket: Deep Researcher ────────────────────────────────────────────────

@router.websocket("/ws/changes/{change_id}/research")
async def ws_research(websocket: WebSocket, change_id: str):
    """
    WebSocket endpoint for the Deep Researcher.

    First message must be {"token": "..."} for auth.
    Subsequent messages: {"message": "<feedback or 'start'>"}.

    On first call, the client sends "start" (or the enriched prompt) to trigger
    the initial research.  On subsequent calls, the user sends feedback text.
    """
    await websocket.accept()
    # Attribute every LLM call in this change-pipeline handler to the change so the Usage
    # dashboard groups it under the right flow (not 'other'). Task-local contextvar — each
    # WS connection is its own asyncio task, so this never leaks across connections.
    try:
        from app.core.observability import set_usage_context as _set_usage_ctx
        _set_usage_ctx(change_request_id=change_id)
    except Exception:
        pass
    logger.info("WS research connected: change=%s", change_id)
    db: Session = SessionLocal()
    user = None

    try:
        # Auth
        auth_msg = await websocket.receive_text()
        auth_data = json.loads(auth_msg)
        token = auth_data.get("token", "")
        user = authenticate_ws(websocket, db, token)
        if not user:
            logger.warning("WS research auth failed: change=%s", change_id)
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            return
        logger.info("WS research auth ok: change=%s user=%s", change_id, user.username)

        cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        if not cr:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Not found"}))
            return

        # Send existing research conversation
        history = _load_conversation(change_id, ConversationModule.RESEARCHER, db)
        await websocket.send_text(json.dumps({"type": "history", "messages": history}))
        logger.info("WS research history sent: change=%s messages=%d", change_id, len(history))

        # R-4 — surface any active job + accept replay protocol
        from app.services import job_registry
        active = job_registry.get_active_jobs(
            db, change_request_id=change_id, module="research",
        )
        if active:
            await websocket.send_text(json.dumps({"type": "active_jobs", "jobs": active}))

        # Uploaded source BRD rides along as rich input (seed, not substitute) so the
        # researcher validates the PM's actual requirements instead of assuming them.
        from app.services.source_material import source_block
        enriched_prompt = (cr.enhanced_prompt or cr.initial_prompt) + source_block(cr)

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data.get("type") == "replay_request":
                rep_job_id = (data.get("job_id") or "").strip()
                rep_since  = int(data.get("since_seq") or 0)
                if not rep_job_id:
                    continue
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await websocket.send_text(json.dumps({
                    "type": "replay", "job_id": rep_job_id, "since_seq": rep_since,
                    "chunks": [{"seq": s, "text": t} for (s, t) in chunks],
                    "count": len(chunks),
                }))
                continue

            user_msg = (data.get("message") or "").strip()
            if not user_msg:
                continue
            logger.info("WS research user message: change=%s len=%d", change_id, len(user_msg))

            # "start" trigger → use enriched prompt as the actual message
            actual_msg = enriched_prompt if user_msg.lower() == "start" else user_msg

            # Save user turn (store actual_msg for history, not "start")
            _save_message(
                change_id, ConversationModule.RESEARCHER,
                MessageRole.USER, actual_msg, user.id, db,
            )

            # Reload history excluding the new message
            history = _load_conversation(change_id, ConversationModule.RESEARCHER, db)
            prior_history = history[:-1]

            # R-4 — register the durable job before streaming.
            registry_job_id = job_registry.create_job(
                db,
                change_request_id=change_id,
                module="research",
                subtype="generate" if user_msg.lower() == "start" else "refine",
                started_by_user_id=user.id,
                metadata={"trigger": user_msg[:120]},
            )
            await websocket.send_text(json.dumps({
                "type": "job_id", "job_id": registry_job_id, "module": "research",
            }))
            research_milestones = [
                (0,     "Researching",            5),
                (4000,  "Gathering sources",      20),
                (12000, "Synthesizing findings",  45),
                (25000, "Composing report",       75),
                (40000, "Finalizing report",      90),
            ]
            stage_idx = job_registry.advance_stage_by_chars(
                db, registry_job_id, 0, research_milestones, 0,
            )

            # Stream research report
            logger.info("WS research streaming started: change=%s", change_id)
            full_response = ""
            try:
                async for chunk in stream_research_turn(
                    enriched_prompt, prior_history, actual_msg, db
                ):
                    full_response += chunk
                    await job_registry.ws_send_chunk(websocket, registry_job_id, chunk)
                    stage_idx = job_registry.advance_stage_by_chars(
                        db, registry_job_id, len(full_response), research_milestones, stage_idx,
                    )
            except Exception as exc:
                logger.exception("WS research streaming failed")
                # A failure mid-stream may have left the session's transaction
                # aborted (e.g. a swallowed DB error upstream). Clear it before
                # any recovery write so fail_job — and the next loop iteration —
                # run on a usable session instead of cascading InFailedSqlTransaction.
                db.rollback()
                job_registry.fail_job(db, registry_job_id, error=str(exc))
                await websocket.send_text(json.dumps({
                    "type": "error", "detail": "An internal error occurred", "job_id": registry_job_id,
                }))
                continue

            logger.info("WS research streaming done: change=%s response_len=%d", change_id, len(full_response))

            # Save assistant response
            _save_message(
                change_id, ConversationModule.RESEARCHER,
                MessageRole.ASSISTANT, full_response, None, db,
            )

            # Upsert ResearchOutput
            existing = (
                db.query(ResearchOutput)
                .filter(ResearchOutput.change_request_id == change_id)
                .order_by(ResearchOutput.version.desc())
                .first()
            )
            if existing:
                existing.combined_report = full_response
                existing.version += 1
            else:
                db.add(ResearchOutput(
                    change_request_id=change_id,
                    combined_report=full_response,
                    version=1,
                    status=ArtifactStatus.DRAFT,
                ))
            db.commit()

            # Phase 7: advisory eval on the research summary
            research_row = (
                db.query(ResearchOutput)
                .filter(ResearchOutput.change_request_id == change_id)
                .order_by(ResearchOutput.version.desc())
                .first()
            )
            fire_advisory_eval(
                change_request_id=change_id,
                checkpoint_id=CheckpointId.PROMPT_TO_RESEARCH,
                source_artifacts={
                    "enhanced_prompt": {
                        "type": "enhanced_prompt",
                        "content": cr.enhanced_prompt or cr.initial_prompt or "",
                    },
                },
                target_artifacts={
                    "research_summary": {
                        "type": "research_summary",
                        "content": full_response,
                    },
                },
                target_artifact_ids=_artifact_ids(research_row),
            )

            job_registry.complete_job(
                db, registry_job_id,
                result={"markdown_chars": len(full_response)},
                final_stage="Research ready",
            )
            await websocket.send_text(json.dumps({
                "type": "done",
                "full": full_response,
                "job_id": registry_job_id,
            }))

    except WebSocketDisconnect:
        logger.info("WS research disconnected: change=%s", change_id)
    except Exception as e:
        logger.exception("WS research error: change=%s", change_id)
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": "An internal error occurred"}))
        except Exception:
            pass
    finally:
        db.close()


# ── REST: canvas ──────────────────────────────────────────────────────────────

def _provenance(row) -> dict:
    """Generate-or-Upload provenance fields for an artifact row, for the UI badge."""
    src = getattr(row, "source", None)
    uploaded_at = getattr(row, "uploaded_at", None)
    return {
        "source":            src.value if hasattr(src, "value") else (src or "generated"),
        "original_filename": getattr(row, "original_filename", None),
        "uploaded_at":       uploaded_at.isoformat() if uploaded_at else None,
    }


def _has_generated_version(model, change_id: str, db, doc_type_enum=None) -> bool:
    """True if a GENERATED version of this artifact exists — drives the
    "Revert to generated" affordance after an upload."""
    from app.models.document_source import DocumentSource
    q = db.query(model.id).filter(
        model.change_request_id == change_id,
        model.source == DocumentSource.GENERATED,
    )
    if doc_type_enum is not None:
        q = q.filter(model.doc_type == doc_type_enum)
    return q.first() is not None


@router.get("/changes/{change_id}/canvas")
def get_canvas(change_id: str, db: DbDep, _: CurrentUser):
    _get_change_or_404(change_id, db)
    row = (db.query(ProductCanvas)
           .filter(ProductCanvas.change_request_id == change_id)
           .order_by(ProductCanvas.version.desc()).first())
    if not row:
        return {"content": None, "version": 0}
    return {"id": row.id, "content": row.content, "version": row.version, "status": row.status.value,
            **_provenance(row)}


@router.get("/changes/{change_id}/canvas/download")
def download_canvas_docx(change_id: str, db: DbDep, _: CurrentUser):
    """Download the latest Product Canvas as a .docx file."""
    cr = _get_change_or_404(change_id, db)
    row = (db.query(ProductCanvas)
           .filter(ProductCanvas.change_request_id == change_id)
           .order_by(ProductCanvas.version.desc()).first())
    if not row or not row.content:
        raise HTTPException(status_code=404, detail="Canvas not generated yet")

    title = cr.title or "UPI Feature"
    docx_bytes = generate_canvas_docx(title, row.content)
    filename = f"product_canvas_v{row.version}.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── REST: BRD ─────────────────────────────────────────────────────────────────

@router.get("/changes/{change_id}/brd")
def get_brd(change_id: str, db: DbDep, _: CurrentUser):
    _get_change_or_404(change_id, db)
    row = (db.query(BRD)
           .filter(BRD.change_request_id == change_id)
           .order_by(BRD.version.desc()).first())
    if not row:
        return {"content": None, "version": 0, "status": "draft"}
    return {"id": row.id, "content": row.content, "version": row.version, "status": row.status.value,
            **_provenance(row),
            "has_generated_version": _has_generated_version(BRD, change_id, db)}


@router.get("/changes/{change_id}/brd/versions")
def list_brd_versions(change_id: str, db: DbDep, _: CurrentUser):
    """Every persisted BRD version, newest first — metadata only. The history picker
    lists these and fetches a chosen version's content on demand."""
    _get_change_or_404(change_id, db)
    rows = (db.query(BRD)
            .filter(BRD.change_request_id == change_id)
            .order_by(BRD.version.desc()).all())
    return {"versions": [
        {"id": r.id, "version": r.version, "status": r.status.value,
         "has_docx": bool(r.docx_path),
         "created_at": r.created_at.isoformat() if r.created_at else None,
         "updated_at": r.updated_at.isoformat() if r.updated_at else None,
         **_provenance(r)}
        for r in rows
    ]}


@router.get("/changes/{change_id}/brd/versions/{version}")
def get_brd_version(change_id: str, version: int, db: DbDep, _: CurrentUser):
    """Full content of one specific BRD version (for the history viewer)."""
    _get_change_or_404(change_id, db)
    row = (db.query(BRD)
           .filter(BRD.change_request_id == change_id, BRD.version == version)
           .first())
    if not row:
        raise HTTPException(status_code=404, detail="BRD version not found")
    return {"id": row.id, "content": row.content, "version": row.version,
            "status": row.status.value, **_provenance(row)}


class SubmitBRDRequest(BaseModel):
    brd_id: str


@router.post("/changes/{change_id}/brd/submit")
def submit_brd(change_id: str, payload: SubmitBRDRequest, db: DbDep, _: CurrentUser):
    """Submit BRD for approval — creates pending approval records for all reviewer roles."""
    _get_change_or_404(change_id, db)
    brd = db.query(BRD).filter(BRD.id == payload.brd_id).first()
    if not brd:
        raise HTTPException(status_code=404, detail="BRD not found")

    # G2: don't let a BRD go to approval with unresolved plan-reconciliation conflicts
    # (approving it would sign off a doc whose conflicts might still change it).
    from app.agents.upload_reconciler import has_unresolved_reconciliation, overturns_needs_ack
    if has_unresolved_reconciliation(db, change_id, "brd"):
        raise HTTPException(status_code=409,
                            detail="Resolve the uploaded-BRD reconciliation conflicts before submitting for approval.")
    # §8.1 soft gate: an accepted change the code check flagged as overturning a ratified
    # decision must be acknowledged before the plan is re-versioned to it.
    if overturns_needs_ack(db, change_id, "brd"):
        raise HTTPException(status_code=409,
                            detail="One of your accepted changes overturns a ratified plan decision — "
                                   "acknowledge it in the code-check panel before submitting for approval.")

    # Delete stale approvals for this BRD (re-submission after revision)
    db.query(Approval).filter(
        Approval.artifact_type == ApprovalArtifactType.BRD,
        Approval.artifact_id == brd.id,
    ).delete()

    # DEV-ONLY shortcut: skip approval-row creation entirely and mark the BRD
    # as APPROVED so downstream stages unblock without manual reviewer clicks.
    # Refused in production by config (dev_skip_approvals must be False).
    if (
        getattr(_app_settings, "dev_skip_approvals", False)
        and (_app_settings.app_env or "").lower() != "production"
    ):
        brd.status = BRDStatus.APPROVED
        from app.agents.upload_reconciler import apply_reconciliation_on_brd_approval
        apply_reconciliation_on_brd_approval(db, change_id)
        cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        gate_blocked: dict | None = None
        if cr and cr.status == ChangeStatus.BRD:
            # Phase 7 — honour the eval gate even on the dev shortcut. If the
            # gate would block, the BRD stays APPROVED but the change does
            # not advance; user must hit /advance explicitly to deal with it.
            _post_brd = _post_brd_status(getattr(cr, "workflow_version", 2))
            allowed, gate_detail = _eval_gate_allows_transition(
                db=db,
                change_id=cr.id,
                current_status=ChangeStatus.BRD,
                next_status=_post_brd,
            )
            if allowed:
                cr.status = _post_brd
            else:
                gate_blocked = gate_detail
                logger.warning(
                    "BRD dev_skip_approvals auto-advance blocked by eval gate: change=%s",
                    change_id,
                )
        db.commit()
        logger.info("BRD submit (dev_skip_approvals): auto-approved change=%s brd_id=%s", change_id, brd.id)
        return {
            "status": "approved",
            "brd_id": brd.id,
            "dev_skip_approvals": True,
            "auto_advance_blocked_by_gate": gate_blocked,
        }

    brd.status = BRDStatus.SUBMITTED

    # One pending approval per reviewer ROLE — the slot belongs to the role, not
    # to a specific user. Any user assigned that role and currently switched to it
    # (active role == reviewer_role) may fill the slot; approver_id is stamped on
    # response to record WHO approved. See POST /approvals/{id}/respond.
    reviewer_roles = [UserRole.PRODUCT_MANAGER, UserRole.TECH_LEAD,
                      UserRole.INFOSEC_REVIEWER, UserRole.RISK_REVIEWER]

    for role in reviewer_roles:
        db.add(Approval(
            artifact_type=ApprovalArtifactType.BRD,
            artifact_id=brd.id,
            approver_id=None,                 # role slot — claimed on approval
            reviewer_role=role.value,
            status=ApprovalStatus.PENDING,
        ))

    db.commit()
    logger.info("BRD submitted: change=%s brd_id=%s", change_id, brd.id)
    return {"status": "submitted", "brd_id": brd.id}


@router.get("/changes/{change_id}/brd/approvals")
def get_brd_approvals(change_id: str, db: DbDep, _: CurrentUser):
    """Return approvals for the latest BRD of this change request."""
    brd = (db.query(BRD)
           .filter(BRD.change_request_id == change_id)
           .order_by(BRD.version.desc()).first())
    if not brd:
        return {"approvals": [], "brd_status": "draft"}

    approvals = db.query(Approval).filter(
        Approval.artifact_type == ApprovalArtifactType.BRD,
        Approval.artifact_id == brd.id,
    ).all()

    result = []
    for a in approvals:
        reviewer = db.query(User).filter(User.id == a.approver_id).first() if a.approver_id else None
        # Derive display name: prefer DB user, fall back to role label
        role_label = (a.reviewer_role or "").replace("_", " ").title()
        reviewer_name = (reviewer.full_name or reviewer.username) if reviewer else f"{role_label} (unassigned)"
        reviewer_role = (reviewer.role.value if reviewer else a.reviewer_role) or "unknown"
        result.append({
            "id":            a.id,
            "approver_id":   a.approver_id,
            "reviewer_name": reviewer_name,
            "reviewer_role": reviewer_role,
            "status":        a.status.value,
            "comments":      a.comments,
            "responded_at":  a.responded_at.isoformat() if a.responded_at else None,
        })
    return {"approvals": result, "brd_status": brd.status.value, "brd_id": brd.id}


class ApprovalDecision(BaseModel):
    status: str   # "approved" | "rejected"
    comments: str | None = None


@router.post("/approvals/{approval_id}/respond")
def respond_approval(approval_id: str, payload: ApprovalDecision, db: DbDep, user: CurrentUser):
    """Reviewer submits approve/reject decision."""
    approval = db.query(Approval).filter(Approval.id == approval_id).first()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")

    # Allow if: explicitly assigned to this user, OR role matches, OR user is admin
    role_match = approval.reviewer_role and user.role.value == approval.reviewer_role
    is_assigned = approval.approver_id == user.id
    is_admin = user.role.value == "admin"
    if not (is_assigned or role_match or is_admin):
        raise HTTPException(status_code=403, detail="Not your approval to respond to")

    # Assign approver_id if it was a placeholder
    if not approval.approver_id:
        approval.approver_id = user.id

    try:
        approval.status = ApprovalStatus(payload.status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid status")

    approval.comments = payload.comments
    from datetime import datetime, timezone
    approval.responded_at = datetime.now(timezone.utc)
    logger.info("Approval response: change=%s role=%s status=%s", approval.artifact_id, approval.reviewer_role, payload.status)
    db.commit()

    # Check if BRD is fully approved
    brd = db.query(BRD).filter(BRD.id == approval.artifact_id).first()
    auto_advance_blocked_by_gate: dict | None = None
    if brd:
        all_approvals = db.query(Approval).filter(
            Approval.artifact_type == ApprovalArtifactType.BRD,
            Approval.artifact_id == brd.id,
        ).all()
        if all(a.status == ApprovalStatus.APPROVED for a in all_approvals):
            brd.status = BRDStatus.APPROVED
            from app.agents.upload_reconciler import apply_reconciliation_on_brd_approval
            apply_reconciliation_on_brd_approval(db, brd.change_request_id)
            # Advance change request to tech_spec — Phase 7: honour the eval
            # gate. Approval succeeds either way; if the gate would block,
            # status stays at BRD and the gate detail is returned so the UI
            # can surface "BRD approved but blocked by eval — go to /advance
            # to override or retry".
            cr = db.query(ChangeRequest).filter(ChangeRequest.id == brd.change_request_id).first()
            if cr and cr.status == ChangeStatus.BRD:
                _post_brd = _post_brd_status(getattr(cr, "workflow_version", 2))
                allowed, gate_detail = _eval_gate_allows_transition(
                    db=db,
                    change_id=cr.id,
                    current_status=ChangeStatus.BRD,
                    next_status=_post_brd,
                )
                if allowed:
                    cr.status = _post_brd
                else:
                    auto_advance_blocked_by_gate = gate_detail
                    logger.warning(
                        "BRD approval auto-advance blocked by eval gate: change=%s approval=%s",
                        cr.id, approval.id,
                    )
            db.commit()

    return {
        "status": approval.status.value,
        "auto_advance_blocked_by_gate": auto_advance_blocked_by_gate,
    }


@router.get("/approvals/pending")
def get_pending_approvals(db: DbDep, user: CurrentUser):
    """Return all pending approvals actionable by the current user.

    Includes:
    - Approvals explicitly assigned to this user
    - Role-matched placeholder approvals (reviewer_role == user.role)
    - All unassigned placeholders if user is admin
    """
    from sqlalchemy import or_

    is_admin = user.role.value == "admin"

    conditions = [Approval.approver_id == user.id]
    if user.role.value != "admin":
        conditions.append(
            (Approval.approver_id == None) & (Approval.reviewer_role == user.role.value)  # noqa: E711
        )
    if is_admin:
        # Admin can see all unassigned placeholders
        conditions.append(Approval.approver_id == None)  # noqa: E711

    approvals = db.query(Approval).filter(
        Approval.status == ApprovalStatus.PENDING,
        or_(*conditions),
    ).all()

    result = []
    for a in approvals:
        if a.artifact_type == ApprovalArtifactType.BRD:
            brd = db.query(BRD).filter(BRD.id == a.artifact_id).first()
            if brd:
                cr = db.query(ChangeRequest).filter(
                    ChangeRequest.id == brd.change_request_id).first()
                role_label = (a.reviewer_role or "").replace("_", " ").title()
                result.append({
                    "id":            a.id,
                    "status":        a.status.value,
                    "artifact_type": a.artifact_type.value,
                    "artifact_id":   a.artifact_id,
                    "change_id":     brd.change_request_id,
                    "change_title":  cr.title if cr else "Unknown",
                    "reviewer_role": a.reviewer_role or "unknown",
                    "brd_version":   brd.version,
                    "brd_content":   brd.content,
                    "submitted_at":  a.created_at.isoformat() if a.created_at else None,
                })
    return {"approvals": result}


# ── WebSocket: Canvas ─────────────────────────────────────────────────────────

@router.websocket("/ws/changes/{change_id}/canvas")
async def ws_canvas(websocket: WebSocket, change_id: str):
    await websocket.accept()
    # Attribute every LLM call in this change-pipeline handler to the change so the Usage
    # dashboard groups it under the right flow (not 'other'). Task-local contextvar — each
    # WS connection is its own asyncio task, so this never leaks across connections.
    try:
        from app.core.observability import set_usage_context as _set_usage_ctx
        _set_usage_ctx(change_request_id=change_id)
    except Exception:
        pass
    logger.info("WS canvas connected: change=%s", change_id)
    db: Session = SessionLocal()
    try:
        auth_msg = await websocket.receive_text()
        token = json.loads(auth_msg).get("token", "")
        user = authenticate_ws(websocket, db, token)
        if not user:
            logger.warning("WS canvas auth failed: change=%s", change_id)
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            return
        logger.info("WS canvas auth ok: change=%s user=%s", change_id, user.username)

        cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        if not cr:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Not found"}))
            return

        history = _load_conversation(change_id, ConversationModule.CANVAS, db)
        await websocket.send_text(json.dumps({"type": "history", "messages": history}))
        logger.info("WS canvas history sent: change=%s messages=%d", change_id, len(history))

        # R-4 — surface active jobs + accept replay protocol
        from app.services import job_registry
        active = job_registry.get_active_jobs(
            db, change_request_id=change_id, module="canvas",
        )
        if active:
            await websocket.send_text(json.dumps({"type": "active_jobs", "jobs": active}))

        # Uploaded source BRD rides along as rich input (seed, not substitute).
        from app.services.source_material import source_block
        enriched_prompt = (cr.enhanced_prompt or cr.initial_prompt) + source_block(cr)
        research = (db.query(ResearchOutput)
                    .filter(ResearchOutput.change_request_id == change_id)
                    .order_by(ResearchOutput.version.desc()).first())
        research_report = research.combined_report if research else "No research report available."

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data.get("type") == "replay_request":
                rep_job_id = (data.get("job_id") or "").strip()
                rep_since  = int(data.get("since_seq") or 0)
                if not rep_job_id:
                    continue
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await websocket.send_text(json.dumps({
                    "type": "replay", "job_id": rep_job_id, "since_seq": rep_since,
                    "chunks": [{"seq": s, "text": t} for (s, t) in chunks],
                    "count": len(chunks),
                }))
                continue

            user_msg = (data.get("message") or "").strip()
            if not user_msg:
                continue

            actual_msg = "Generate the product canvas." if user_msg.lower() == "start" else user_msg
            logger.info("WS canvas user message: change=%s len=%d", change_id, len(user_msg))

            _save_message(change_id, ConversationModule.CANVAS, MessageRole.USER, actual_msg, user.id, db)

            history = _load_conversation(change_id, ConversationModule.CANVAS, db)
            prior_history = history[:-1]

            # R-4 — durable job
            registry_job_id = job_registry.create_job(
                db,
                change_request_id=change_id,
                module="canvas",
                subtype="generate" if user_msg.lower() == "start" else "refine",
                started_by_user_id=user.id,
                metadata={"trigger": user_msg[:120]},
            )
            await websocket.send_text(json.dumps({
                "type": "job_id", "job_id": registry_job_id, "module": "canvas",
            }))
            canvas_milestones = [
                (0,     "Generating canvas",             5),
                (3000,  "Drafting personas & problems", 25),
                (9000,  "Detailing solution & metrics", 55),
                (18000, "Finalizing canvas",             85),
            ]
            stage_idx = job_registry.advance_stage_by_chars(
                db, registry_job_id, 0, canvas_milestones, 0,
            )

            logger.info("WS canvas streaming started: change=%s", change_id)
            full_response = ""
            try:
                async for chunk in stream_canvas_turn(enriched_prompt, research_report, prior_history, actual_msg):
                    full_response += chunk
                    await job_registry.ws_send_chunk(websocket, registry_job_id, chunk)
                    stage_idx = job_registry.advance_stage_by_chars(
                        db, registry_job_id, len(full_response), canvas_milestones, stage_idx,
                    )
            except Exception as exc:
                logger.exception("WS canvas streaming failed")
                job_registry.fail_job(db, registry_job_id, error=str(exc))
                await websocket.send_text(json.dumps({
                    "type": "error", "detail": "An internal error occurred", "job_id": registry_job_id,
                }))
                continue
            logger.info("WS canvas streaming done: change=%s response_len=%d", change_id, len(full_response))

            _save_message(change_id, ConversationModule.CANVAS, MessageRole.ASSISTANT, full_response, None, db)

            existing = (db.query(ProductCanvas)
                        .filter(ProductCanvas.change_request_id == change_id)
                        .order_by(ProductCanvas.version.desc()).first())
            if existing:
                existing.content = full_response
                existing.version += 1
            else:
                db.add(ProductCanvas(change_request_id=change_id, content=full_response, version=1))
            db.commit()

            # Phase 7: advisory eval on the product canvas
            canvas_row = (
                db.query(ProductCanvas)
                .filter(ProductCanvas.change_request_id == change_id)
                .order_by(ProductCanvas.version.desc())
                .first()
            )
            research_for_canvas = (
                db.query(ResearchOutput)
                .filter(ResearchOutput.change_request_id == change_id)
                .order_by(ResearchOutput.version.desc())
                .first()
            )
            fire_advisory_eval(
                change_request_id=change_id,
                checkpoint_id=CheckpointId.RESEARCH_TO_CANVAS,
                source_artifacts={
                    "research_summary": {
                        "type": "research_summary",
                        "content": (research_for_canvas.combined_report if research_for_canvas else ""),
                    },
                },
                target_artifacts={
                    "product_canvas": {
                        "type": "product_canvas",
                        "content": full_response,
                    },
                },
                source_artifact_ids=_artifact_ids(research_for_canvas),
                target_artifact_ids=_artifact_ids(canvas_row),
            )

            validation = summarize_validation(validate_doc(full_response, doc_type="canvas"))
            job_registry.complete_job(
                db, registry_job_id,
                result={"markdown_chars": len(full_response), "validation": validation},
                final_stage="Canvas ready",
            )
            await websocket.send_text(json.dumps({
                "type": "done", "full": full_response, "validation": validation,
                "job_id": registry_job_id,
            }))

    except WebSocketDisconnect:
        logger.info("WS canvas disconnected: change=%s", change_id)
    except Exception as e:
        logger.exception("WS canvas error: change=%s", change_id)
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": "An internal error occurred"}))
        except Exception:
            pass
    finally:
        db.close()


# ── WebSocket: BRD ────────────────────────────────────────────────────────────

@router.websocket("/ws/changes/{change_id}/brd")
async def ws_brd(websocket: WebSocket, change_id: str):
    await websocket.accept()
    # Attribute every LLM call in this change-pipeline handler to the change so the Usage
    # dashboard groups it under the right flow (not 'other'). Task-local contextvar — each
    # WS connection is its own asyncio task, so this never leaks across connections.
    try:
        from app.core.observability import set_usage_context as _set_usage_ctx
        _set_usage_ctx(change_request_id=change_id)
    except Exception:
        pass
    logger.info("WS brd connected: change=%s", change_id)
    db: Session = SessionLocal()

    # R-3 — durable agent-job tracking. Module label matches the frontend
    # JobsContext / useResumableJob convention.
    from app.services import job_registry

    try:
        auth_msg = await websocket.receive_text()
        token = json.loads(auth_msg).get("token", "")
        user = authenticate_ws(websocket, db, token)
        if not user:
            logger.warning("WS brd auth failed: change=%s", change_id)
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            return
        logger.info("WS brd auth ok: change=%s user=%s", change_id, user.username)

        cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        if not cr:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Not found"}))
            return

        history = _load_conversation(change_id, ConversationModule.BRD, db)
        await websocket.send_text(json.dumps({"type": "history", "messages": history}))
        logger.info("WS brd history sent: change=%s messages=%d", change_id, len(history))

        # R-3 — surface any in-flight job for this (change_id, brd) so the
        # client (useResumableJob) can show the resume banner and request
        # chunk replay. The list is at most one entry today (we don't allow
        # parallel BRDs on the same change), but we use the multi-job
        # protocol shape for forward compatibility with Product Kit "all"
        # mode in R-5.
        active = job_registry.get_active_jobs(
            db, change_request_id=change_id, module="brd",
        )
        if active:
            await websocket.send_text(json.dumps({
                "type": "active_jobs",
                "jobs": active,
            }))
            logger.info("WS brd: surfaced %d active job(s)", len(active))

        # Uploaded source BRD rides along as rich input — the generated BRD stays canonical,
        # but it is grounded in the PM's document instead of assumptions.
        from app.services.source_material import source_block
        enriched_prompt = (cr.enhanced_prompt or cr.initial_prompt) + source_block(cr)
        research = (db.query(ResearchOutput)
                    .filter(ResearchOutput.change_request_id == change_id)
                    .order_by(ResearchOutput.version.desc()).first())
        research_report = research.combined_report if research else "No research report available."
        canvas = (db.query(ProductCanvas)
                  .filter(ProductCanvas.change_request_id == change_id)
                  .order_by(ProductCanvas.version.desc()).first())
        canvas_content = canvas.content if canvas else "No canvas available."

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            # R-3 — replay protocol. Client sends {type:'replay_request',
            # job_id, since_seq} on reconnect to catch up on chunks emitted
            # while the WS was disconnected. We respond with one
            # {type:'replay', ...} message and continue waiting for the
            # next message — the user's actual feedback / 'start' command.
            if data.get("type") == "replay_request":
                rep_job_id = (data.get("job_id") or "").strip()
                rep_since  = int(data.get("since_seq") or 0)
                if not rep_job_id:
                    continue
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await websocket.send_text(json.dumps({
                    "type":     "replay",
                    "job_id":   rep_job_id,
                    "since_seq": rep_since,
                    "chunks":   [{"seq": s, "text": t} for (s, t) in chunks],
                    "count":    len(chunks),
                }))
                logger.info("WS brd: replayed %d chunks for job=%s since_seq=%d",
                            len(chunks), rep_job_id, rep_since)
                continue

            user_msg = (data.get("message") or "").strip()
            if not user_msg:
                continue

            # Optional UI override: client can send brd_tier in the same
            # message payload to force compact / standard / comprehensive
            # for this generation. Empty / unset / "auto" → LLM classifier picks.
            ws_brd_tier_override = (data.get("brd_tier") or "").strip().lower()
            if ws_brd_tier_override not in ("compact", "standard", "comprehensive"):
                ws_brd_tier_override = ""

            # Special: auto-revise from reviewer comments
            if user_msg == "__auto_revise__":
                current_brd = (db.query(BRD)
                               .filter(BRD.change_request_id == change_id)
                               .order_by(BRD.version.desc()).first())
                if not current_brd:
                    await websocket.send_text(json.dumps({"type": "error", "detail": "No BRD to revise"}))
                    continue

                rejected_approvals = db.query(Approval).filter(
                    Approval.artifact_type == ApprovalArtifactType.BRD,
                    Approval.artifact_id == current_brd.id,
                    Approval.status == ApprovalStatus.REJECTED,
                ).all()
                reviewer_comments = []
                for a in rejected_approvals:
                    rv = db.query(User).filter(User.id == a.approver_id).first()
                    reviewer_comments.append({
                        "reviewer": rv.full_name or rv.username if rv else "Reviewer",
                        "role":     rv.role.value if rv else "unknown",
                        "comments": a.comments or "(no comment)",
                    })

                # R-3 — track the auto-revision as a job too.
                rev_job_id = job_registry.create_job(
                    db,
                    change_request_id=change_id,
                    module="brd",
                    subtype="auto_revise",
                    started_by_user_id=user.id,
                    metadata={"trigger": "reviewer_comments",
                              "reviewer_count": len(reviewer_comments)},
                )
                await websocket.send_text(json.dumps({
                    "type":   "job_id",
                    "job_id": rev_job_id,
                    "module": "brd",
                    "subtype": "auto_revise",
                }))
                full_response = ""
                try:
                    job_registry.update_job(
                        db, rev_job_id,
                        current_stage="Revising from reviewer comments",
                    )
                    # Reviewer comments become a docgen revision instruction — the BRD
                    # is regenerated through the pipeline (the only generation path).
                    _rc = "\n".join(
                        f"- [{c['role']}] {c['reviewer']}: {c['comments']}"
                        for c in reviewer_comments
                    ) or "- (reviewers rejected without specific written comments)"
                    _revise_instruction = (
                        "Revise the BRD to address the following reviewer comments. Add a "
                        "'## Revision Notes' section at the top summarising how each comment "
                        "was addressed, and keep every other section intact and improved:\n" + _rc
                    )
                    from app.services.docgen_runner import (
                        build_initial_state, run_pipeline_in_thread,
                        edit_full_document_in_thread, sections_to_markdown,
                        get_latest_job, set_latest_job,
                    )
                    from app.docgen.plan_store import artifact_dir as _adir
                    import os as _os_rev
                    _rev_docx = None
                    _prior_job = get_latest_job(change_id, "BRD")
                    if _prior_job and _os_rev.path.exists(_adir(_prior_job) / "generated_sections.json"):
                        # Prior docgen artifact exists → in-place section edit.
                        _rev_docx = await edit_full_document_in_thread(_prior_job, _revise_instruction)
                        _sec = json.loads((_adir(_prior_job) / "generated_sections.json").read_text(encoding="utf-8"))
                        _pln = json.loads((_adir(_prior_job) / "document_plan.json").read_text(encoding="utf-8"))
                        full_response = sections_to_markdown(_pln, _sec)
                    else:
                        # No prior docgen artifact (e.g. an uploaded BRD) → fresh pipeline run.
                        _state = build_initial_state(
                            doc_type="BRD",
                            change_id=change_id,
                            prompt=enriched_prompt,
                            document_title=(f"BRD: {cr.title}" if cr.title
                                            else f"BRD: {cr.initial_prompt[:60]}"),
                            audience="Product Managers, Tech Leads, InfoSec, Risk",
                            desired_outcome="Approved BRD",
                            research_report=research_report,
                            canvas_content=canvas_content,
                            additional_context="## PRIOR BRD (revise this)\n"
                            + (current_brd.content or "")[:30000] + "\n\n" + _revise_instruction,
                            include_diagrams=True,
                            use_rag=True,
                            decisions_block=_decisions_block(change_id, db),
                            ratified_plan=_ratified_plan_block(change_id, db),
                            source_flow_spec=_flow_spec(change_id, db),
                        )
                        _final = await run_pipeline_in_thread(_state)
                        # run_pipeline_in_thread RETURNS a failed/cancelled state
                        # (it does not raise), so guard here — otherwise an empty/
                        # partial BRD would be persisted as a successful revision.
                        if _final.get("status") in ("failed", "cancelled"):
                            raise RuntimeError(
                                f"docgen pipeline {_final.get('status')}: "
                                f"{_final.get('error') or 'no output produced'}")
                        full_response = sections_to_markdown(
                            _final.get("document_plan") or {},
                            _final.get("generated_sections") or [],
                        )
                        _rev_docx = _final.get("output_path")
                        if _final.get("job_id"):
                            set_latest_job(change_id, "BRD", _final["job_id"])
                    await job_registry.ws_send_chunk(websocket, rev_job_id, full_response)

                    # Save as new version
                    _new_brd = BRD(
                        change_request_id=change_id,
                        content=full_response,
                        version=current_brd.version + 1,
                        status=BRDStatus.DRAFT,
                    )
                    if _rev_docx:
                        _new_brd.docx_path = _rev_docx
                    db.add(_new_brd)
                    db.commit()
                    validation = summarize_validation(validate_doc(full_response, doc_type="brd"))
                    job_registry.complete_job(
                        db, rev_job_id,
                        result={"markdown_chars": len(full_response), "revised": True},
                        final_stage="Revision complete",
                    )
                    await websocket.send_text(json.dumps({
                        "type": "done", "full": full_response, "revised": True,
                        "validation": validation, "job_id": rev_job_id,
                    }))
                except Exception as exc:
                    logger.exception("WS brd auto-revise failed: change=%s", change_id)
                    job_registry.fail_job(db, rev_job_id, error=str(exc))
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "detail": "An internal error occurred",
                        "job_id": rev_job_id,
                    }))
                continue

            actual_msg = "Generate the BRD." if user_msg.lower() == "start" else user_msg
            logger.info("WS brd user message: change=%s len=%d", change_id, len(user_msg))

            _save_message(change_id, ConversationModule.BRD, MessageRole.USER, actual_msg, user.id, db)

            history = _load_conversation(change_id, ConversationModule.BRD, db)
            prior_history = history[:-1]

            # Load (or lazily build) the cached context — gives us taxonomy + structured proposals.
            proposals_block = ""
            proposals_dict: dict = {}
            try:
                from app.services.context_cache import get_or_build
                from app.agents.proposals_extractor import format_for_prompt
                ctx = await get_or_build(change_id, db)
                if ctx and ctx.proposals:
                    proposals_block = format_for_prompt(ctx.proposals)
                    proposals_dict = ctx.proposals
                    logger.info(
                        "WS brd: proposals confidence=%s taxonomy=%s",
                        ctx.proposals_confidence, ctx.taxonomy_primary,
                    )
            except Exception as e:
                logger.warning("WS brd: context_cache unavailable (%s) — proceeding without proposals", e)

            # Load PM clarification answers (Sprint 5) — injected last as authoritative
            clarification_answers = ""
            try:
                from app.services.clarification_loader import load_answers_block
                clarification_answers = load_answers_block(change_id, db)
                if clarification_answers:
                    logger.info("WS brd: clarification answers loaded (%d chars)", len(clarification_answers))
            except Exception as e:
                logger.warning("WS brd: clarification_loader failed (%s) — proceeding without answers", e)

            # ── Docgen pipeline dispatch ────────────────────────────────────
            # All BRD generation routes through the LangGraph docgen pipeline.
            # First user message → fresh pipeline run; subsequent messages →
            # fast `edit_full_document` against the latest job.
            full_response = ""
            docgen_job_id: str | None = None
            docgen_docx_override: str | None = None

            # R-3 — register the durable agent job BEFORE we start generating.
            # The frontend's useResumableJob picks up `job_id` from the WS
            # message we send right after creation, so a navigate-away mid-turn
            # leaves a recoverable job behind in `agent_jobs` + Redis.
            registry_job_id = job_registry.create_job(
                db,
                change_request_id=change_id,
                module="brd",
                subtype="generate" if actual_msg == "Generate the BRD." else "refine",
                started_by_user_id=user.id,
                metadata={
                    "trigger":         actual_msg[:120],
                },
            )
            await websocket.send_text(json.dumps({
                "type":    "job_id",
                "job_id":  registry_job_id,
                "module":  "brd",
                "subtype": "generate" if actual_msg == "Generate the BRD." else "refine",
            }))

            from app.services.docgen_runner import (
                build_initial_state, run_pipeline_in_thread,
                edit_full_document_in_thread, sections_to_markdown,
                emit_stage_progress_text, get_latest_job, set_latest_job,
            )
            from app.docgen.plan_store import artifact_dir
            import os as _os

            prior_job_id = get_latest_job(change_id, "BRD")
            # ── Dispatch: fresh pipeline for generation, in-place edit for
            # refine turns. The earlier always-fresh dispatch (Option A)
            # regenerated the whole document on every user message — and
            # the revision intent it folded into `additional_context` never
            # reaches the section writers (the BRD planner short-circuits
            # to the blueprint plan), so a targeted edit like "change 40 to
            # 70" was silently dropped while unrelated content drifted.
            # Refine turns now go through edit_full_document, which patches
            # the existing sections in place: each writer sees its CURRENT
            # content and must reproduce it verbatim unless the instruction
            # touches it. Tradeoff: a refine keeps the prior document's
            # section structure — structural completeness still comes from
            # the fresh path on generation.
            is_revise = (
                actual_msg != "Generate the BRD."
                and prior_job_id is not None
                and _os.path.exists(artifact_dir(prior_job_id) / "generated_sections.json")
            )

            if is_revise:
                logger.info("WS brd: docgen edit on prior job_id=%s", prior_job_id)
                job_registry.update_job(
                    db, registry_job_id,
                    current_stage="Applying revision",
                )
                await job_registry.ws_send_chunk(
                    websocket, registry_job_id,
                    emit_stage_progress_text("writing") + "\nApplying revision to all sections…\n",
                )

                # Per-section progress: feeds the UI banner AND bumps the
                # job row's updated_at so the orphan-sweeper can't kill a
                # healthy long revision (it sweeps jobs idle >30 min).
                async def _revise_progress(done: int, total: int) -> None:
                    label = f"Revising sections ({done}/{total})"
                    try:
                        job_registry.update_job(
                            db, registry_job_id, current_stage=label,
                        )
                        await websocket.send_text(json.dumps({
                            "type":          "progress",
                            "job_id":        registry_job_id,
                            "current_stage": label,
                        }))
                    except Exception:
                        pass

                try:
                    new_docx_path = await edit_full_document_in_thread(
                        prior_job_id, actual_msg, on_progress=_revise_progress,
                    )
                except Exception as e:
                    logger.exception("WS brd: docgen edit failed")
                    job_registry.fail_job(db, registry_job_id, error=str(e),
                                          final_stage="Revision failed")
                    await websocket.send_text(json.dumps({
                        "type": "error", "detail": "An internal error occurred",
                        "job_id": registry_job_id,
                    }))
                    continue
                sections_path = artifact_dir(prior_job_id) / "generated_sections.json"
                plan_path = artifact_dir(prior_job_id) / "document_plan.json"
                edited_sections = json.loads(sections_path.read_text(encoding="utf-8"))
                edited_plan = json.loads(plan_path.read_text(encoding="utf-8"))
                full_response = sections_to_markdown(edited_plan, edited_sections)
                docgen_job_id = prior_job_id
                docgen_docx_override = new_docx_path
            else:
                logger.info("WS brd: docgen fresh pipeline run")
                # Stream stage banners up front so the UI doesn't look
                # frozen during the long synchronous pipeline (~3-5 min).
                job_registry.update_job(
                    db, registry_job_id,
                    current_stage="Retrieving knowledge base context",
                )
                await job_registry.ws_send_chunk(
                    websocket, registry_job_id,
                    emit_stage_progress_text("retrieving"),
                )

                # Fold any non-trivial user text + the prior doc into
                # additional_context so the writer can bias toward the
                # user's intent without skipping structural sections.
                extra_parts: list[str] = []
                if clarification_answers:
                    extra_parts.append(clarification_answers)
                if actual_msg and actual_msg != "Generate the BRD.":
                    extra_parts.append(
                        "## USER REVISION INTENT\n"
                        "Treat the following as feedback for this regeneration. "
                        "Address it explicitly in the relevant section(s). "
                        "Do NOT skip any blueprint section — produce a complete "
                        "BRD with every required section even if the feedback only "
                        "touches a subset.\n\n" + actual_msg
                    )
                # Carry prior BRD body forward as soft continuity context
                # — the writer uses it to preserve good prior content where
                # the user didn't ask for changes.
                try:
                    prior_brd_row = (db.query(BRD)
                                     .filter(BRD.change_request_id == change_id)
                                     .order_by(BRD.version.desc()).first())
                    if prior_brd_row and (prior_brd_row.content or "").strip():
                        extra_parts.append(
                            "## PRIOR DRAFT (continuity reference)\n"
                            "The following is the most recent BRD draft for this change "
                            "request. Use it as a CONTINUITY reference for sections the "
                            "user did not ask to change — preserve good content verbatim "
                            "where appropriate. ALWAYS produce the full blueprint structure; "
                            "do not omit sections just because the prior draft was missing them.\n\n"
                            + prior_brd_row.content[:30000]
                        )
                except Exception as _e:
                    logger.warning("WS brd: could not load prior BRD for continuity: %s", _e)

                state = build_initial_state(
                    doc_type="BRD",
                    change_id=change_id,
                    prompt=enriched_prompt,
                    document_title=(f"BRD: {cr.title}" if cr.title
                                    else f"BRD: {cr.initial_prompt[:60]}"),
                    audience="Product Managers, Tech Leads, InfoSec, Risk",
                    desired_outcome="Approved BRD",
                    research_report=research_report,
                    canvas_content=canvas_content,
                    additional_context="\n\n---\n\n".join(extra_parts),
                    include_diagrams=True,
                    use_rag=True,
                    brd_tier_override=(ws_brd_tier_override or None),
                    proposals=proposals_dict,
                    decisions_block=_decisions_block(change_id, db),
                    ratified_plan=_ratified_plan_block(change_id, db),
                    source_flow_spec=_flow_spec(change_id, db),
                )

                # R-3 — every pipeline stage transition both:
                #   (a) sends the banner chunk over the WS (existing UX), and
                #   (b) updates `current_stage` on the agent_jobs row +
                #       broadcasts via the {type:"progress", ...} message
                #       so reconnected clients picking up the WS later see
                #       the right banner.
                _stage_labels = {
                    "retrieving":          "Retrieving knowledge base context",
                    "planning":            "Planning document structure",
                    "generating_diagrams": "Generating UML diagrams",
                    "writing":             "Writing section content",
                    "reviewing":           "Validating sections",
                    "assembling":          "Building .docx",
                }

                async def _emit_stage(stage: str) -> None:
                    label = _stage_labels.get(stage, stage)
                    # Registry + Redis first, live socket last. The `progress`
                    # frame raises once the originating client has navigated
                    # away; keeping it inside the same try as the durable
                    # writes meant every later stage was lost, so a resumed
                    # page sat on the first banner until the run finished.
                    try:
                        job_registry.update_job(
                            db, registry_job_id,
                            current_stage=label,
                        )
                        await job_registry.ws_send_chunk(
                            websocket, registry_job_id,
                            emit_stage_progress_text(stage),
                        )
                    except Exception:
                        pass
                    try:
                        await websocket.send_text(json.dumps({
                            "type":          "progress",
                            "job_id":        registry_job_id,
                            "current_stage": label,
                        }))
                    except Exception:
                        pass

                # R-9 — cooperative cancel: check the registry every poll
                # tick. Returning True from cancel_check abandons the
                # pipeline await and returns a synthetic cancelled state.
                def _check_cancel() -> bool:
                    try:
                        return job_registry.is_cancelled(db, registry_job_id)
                    except Exception:
                        return False

                final_state = await run_pipeline_in_thread(
                    state, on_stage=_emit_stage, cancel_check=_check_cancel,
                )

                # R-9 — handle cooperative cancel: skip fail_job (the
                # cancel API already wrote `cancelled` to the registry),
                # just inform the client and bail out of this turn.
                if final_state.get("status") == "cancelled":
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "detail": "Cancelled by user",
                        "job_id": registry_job_id,
                        "cancelled": True,
                    }))
                    continue

                if final_state.get("status") == "failed":
                    logger.warning("WS brd: docgen pipeline failed err=%s",
                                   final_state.get("error"))
                    job_registry.fail_job(
                        db, registry_job_id,
                        error=str(final_state.get("error") or "pipeline failed"),
                        final_stage="Pipeline failed",
                    )
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "detail": "An internal error occurred",
                        "job_id": registry_job_id,
                    }))
                    continue

                plan = final_state.get("document_plan") or {}
                sections = final_state.get("generated_sections") or []
                full_response = sections_to_markdown(plan, sections)
                docgen_job_id = final_state.get("job_id")
                docgen_docx_override = final_state.get("output_path")
                if docgen_job_id:
                    set_latest_job(change_id, "BRD", docgen_job_id)

                logger.info(
                    "WS brd: docgen complete change=%s job=%s len=%d docx=%s",
                    change_id, docgen_job_id, len(full_response),
                    bool(docgen_docx_override),
                )

            # Send the assembled markdown as a single chunk so the UI's
            # ReactMarkdown renderer has the same text it would have
            # streamed in the legacy path. Mirror to the chunk buffer.
            await job_registry.ws_send_chunk(websocket, registry_job_id, full_response)

            # Plan-consistency enforcement — keep the BRD's technical surface within the ratified
            # plan. If it invented a wire-message/schema/endpoint (a BLOCKER), let the LLM correct
            # it up to MAX_REPAIR_ATTEMPTS times BEFORE we persist, so the saved doc + .docx + eval all see the
            # corrected text. No hard block: after the budget the banner surfaces any residue.
            doc_consistency = None
            try:
                from app.agents.plan_contract import build_plan_contract
                from app.agents.doc_consistency import enforce_plan_consistency, reconcile_doc_to_plan, MAX_REPAIR_ATTEMPTS
                _pc = build_plan_contract(db, change_id)
                if _pc:
                    async def _repair_brd(instruction: str, attempt: int, current_content: str, items: list[str]) -> str:
                        nonlocal docgen_docx_override
                        await job_registry.ws_send_chunk(
                            websocket, registry_job_id,
                            f"\n⟳ BRD diverges from the ratified plan — auto-correcting (attempt {attempt}/{MAX_REPAIR_ATTEMPTS})…\n")
                        if docgen_job_id:
                            from app.services.docgen_runner import (
                                edit_divergent_sections_in_thread as _edit_doc,
                                sections_to_markdown as _secs_to_md,
                            )
                            from app.docgen.plan_store import artifact_dir as _adir
                            _new_docx = await _edit_doc(docgen_job_id, instruction, items)
                            _sec = json.loads((_adir(docgen_job_id) / "generated_sections.json").read_text(encoding="utf-8"))
                            _pln = json.loads((_adir(docgen_job_id) / "document_plan.json").read_text(encoding="utf-8"))
                            docgen_docx_override = _new_docx
                            return _secs_to_md(_pln, _sec)
                        return await reconcile_doc_to_plan(
                            doc_kind="BRD", doc_content=current_content,
                            plan_contract=_pc, instruction=instruction)

                    _enf = await enforce_plan_consistency(
                        doc_kind="BRD", doc_content=full_response,
                        plan_contract=_pc, repair_fn=_repair_brd)
                    full_response = _enf["content"]
                    doc_consistency = _enf["consistency"]
                    if _enf["repaired"]:
                        # Status line ONLY — never re-send the body. The chunk buffer is
                        # append-only on BOTH paths (live WS does `buffer += text`; REST replay
                        # does `chunks.map(...).join('')`), so pushing the corrected document
                        # again renders a SECOND copy under the first and the reader sees
                        # "multiple versions" mid-generation. The `done` payload carries the
                        # authoritative `full`, which the client swaps in wholesale — which is
                        # why the finished view was already correct while the in-flight one wasn't.
                        await job_registry.ws_send_chunk(
                            websocket, registry_job_id,
                            "\n✓ Corrected to match the ratified plan — the final document "
                            "replaces this preview when generation completes.\n")
                    if doc_consistency.get("findings"):
                        logger.warning("BRD consistency: change=%s has_blocker=%s findings=%d repaired=%s",
                                       change_id, doc_consistency.get("has_blocker"),
                                       len(doc_consistency["findings"]), _enf["repaired"])
            except Exception as e:  # noqa: BLE001 — never block generation on the consistency machinery
                logger.warning("BRD consistency enforcement failed: %s", e)

            _save_message(change_id, ConversationModule.BRD, MessageRole.ASSISTANT, full_response, None, db)

            existing = (db.query(BRD)
                        .filter(BRD.change_request_id == change_id)
                        .order_by(BRD.version.desc()).first())
            if existing and existing.status == BRDStatus.DRAFT:
                # Persist every regeneration as its OWN versioned row instead of
                # overwriting the existing draft's content — otherwise prior versions
                # are lost and can never be viewed. Mirrors the reviewer auto-revise
                # path, which already inserts a new row. A freshly generated row is
                # GENERATED-provenance by default, so a prior UPLOADED draft is kept
                # intact as an earlier version rather than being mutated in place.
                db.add(BRD(change_request_id=change_id, content=full_response,
                           version=existing.version + 1, status=BRDStatus.DRAFT))
                from app.models.document_source import DocumentSource
                if existing.source == DocumentSource.UPLOADED:
                    # G1: the uploaded BRD has been superseded by a generated version —
                    # clear its open reconciliation so stale conflicts don't block
                    # downstream. The uploaded row itself is KEPT (as an earlier version),
                    # so only its reconciliation state is retired — not its provenance.
                    from app.agents.upload_reconciler import supersede_open_reconciliations
                    supersede_open_reconciliations(db, change_id, "brd")
            else:
                db.add(BRD(change_request_id=change_id, content=full_response, version=1, status=BRDStatus.DRAFT))
            db.commit()

            brd = (db.query(BRD).filter(BRD.change_request_id == change_id)
                   .order_by(BRD.version.desc()).first())

            # Phase 7: advisory eval on the BRD against canvas + clarifications
            from app.models.clarification import Clarification as _ClarForEval
            canvas_for_brd = (
                db.query(ProductCanvas)
                .filter(ProductCanvas.change_request_id == change_id)
                .order_by(ProductCanvas.version.desc())
                .first()
            )
            clar_for_brd = (
                db.query(_ClarForEval)
                .filter(_ClarForEval.change_request_id == change_id)
                .order_by(_ClarForEval.version.desc())
                .first()
            )
            fire_advisory_eval(
                change_request_id=change_id,
                checkpoint_id=CheckpointId.CLARIFICATION_TO_BRD,
                source_artifacts={
                    "product_canvas": {
                        "type": "product_canvas",
                        "content": (canvas_for_brd.content if canvas_for_brd else ""),
                    },
                    "clarification_thread": {
                        "type": "clarification_thread",
                        "questions": (clar_for_brd.questions if clar_for_brd else []) or [],
                        "answers":   (clar_for_brd.answers if clar_for_brd else {}) or {},
                    },
                },
                target_artifacts={
                    "brd_document": {
                        "type": "brd",
                        "content": full_response,
                    },
                },
                source_artifact_ids=_artifact_ids(canvas_for_brd, clar_for_brd),
                target_artifact_ids=_artifact_ids(brd),
            )

            validation = summarize_validation(validate_doc(full_response, doc_type="brd"))

            # When docgen owns assembly the .docx already exists on disk —
            # honour that path rather than re-running the legacy assembler.
            if docgen_docx_override:
                docx_path = docgen_docx_override
            else:
                docx_path = _assemble_docx_safe(
                    full_response, change_id=change_id, doc_type="BRD",
                    version=brd.version, cr_title=cr.title or cr.initial_prompt[:80],
                )
            if docx_path:
                brd.docx_path = docx_path
                db.commit()

            # R-3 — mark the durable job complete.
            job_registry.complete_job(
                db, registry_job_id,
                result={
                    "brd_id":         brd.id,
                    "version":        brd.version,
                    "markdown_chars": len(full_response),
                    "docx_path":      docx_path,
                    "docgen_job_id":  docgen_job_id,
                    "validation":     validation,
                },
                final_stage="BRD ready",
            )

            done_payload = {
                "type": "done",
                "full": full_response,
                "brd_id": brd.id,
                "validation": validation,
                "docx_available": bool(docx_path),
                "job_id": registry_job_id,    # R-3 — durable job id for the resume protocol
                "doc_consistency": doc_consistency,
            }
            if docgen_job_id:
                # Surfaced for STEP 7 frontend section-wise edit dropdown.
                done_payload["docgen_job_id"] = docgen_job_id
            await websocket.send_text(json.dumps(done_payload))

    except WebSocketDisconnect:
        logger.info("WS brd disconnected: change=%s", change_id)
    except Exception as e:
        logger.exception("WS brd error: change=%s", change_id)
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": "An internal error occurred"}))
        except Exception:
            pass
    finally:
        db.close()


# ── REST: Tech Spec ───────────────────────────────────────────────────────────

@router.get("/changes/{change_id}/tech-spec")
def get_tech_spec(change_id: str, db: DbDep, _: CurrentUser):
    _get_change_or_404(change_id, db)
    row = (db.query(TechSpec)
           .filter(TechSpec.change_request_id == change_id)
           .order_by(TechSpec.version.desc()).first())
    if not row:
        return {"content": None, "version": 0, "status": "draft"}
    return {"id": row.id, "content": row.content, "version": row.version, "status": row.status.value,
            **_provenance(row),
            "has_generated_version": _has_generated_version(TechSpec, change_id, db)}


# ── REST: XSD ────────────────────────────────────────────────────────────────

@router.get("/changes/{change_id}/xsd")
def get_xsd(change_id: str, db: DbDep, _: CurrentUser):
    _get_change_or_404(change_id, db)
    row = (db.query(XSD)
           .filter(XSD.change_request_id == change_id)
           .order_by(XSD.version.desc()).first())
    if not row:
        return {"content": None, "version": 0, "status": "draft", "is_required": None}
    return {
        "id":          row.id,
        "content":     row.content,
        "version":     row.version,
        "status":      row.status.value,
        "is_required": row.is_required,
        **_provenance(row),
    }


@router.post("/changes/{change_id}/xsd/assess")
async def assess_xsd(change_id: str, db: DbDep, _: CurrentUser):
    """Run the XSD requirement assessment and return the full assessment text."""
    cr = _get_change_or_404(change_id, db)
    wfv = getattr(cr, "workflow_version", 2)

    tech_spec = (db.query(TechSpec)
                 .filter(TechSpec.change_request_id == change_id)
                 .order_by(TechSpec.version.desc()).first())
    brd = (db.query(BRD)
           .filter(BRD.change_request_id == change_id)
           .order_by(BRD.version.desc()).first())

    if not brd or not brd.content:
        raise HTTPException(status_code=400, detail="BRD not found")
    # v1: XSD comes AFTER the TSD, so the TSD must exist. v2 (reorder): XSD comes
    # BEFORE the TSD — assess against the BRD + ratified decisions instead.
    if wfv < 2 and (not tech_spec or not tech_spec.content):
        raise HTTPException(status_code=400, detail="Tech Spec not generated yet")

    assessment_basis = (tech_spec.content if (tech_spec and tech_spec.content)
                        else _decisions_block(change_id, db))
    full = ""
    async for chunk in stream_xsd_assessment(assessment_basis, brd.content):
        full += chunk

    # Determine if required from assessment text
    is_required = "**REQUIRED**" in full and "NOT REQUIRED" not in full.split("**REQUIRED**")[0]

    # Upsert XSD record with assessment + is_required flag
    existing = (db.query(XSD)
                .filter(XSD.change_request_id == change_id)
                .order_by(XSD.version.desc()).first())
    if existing:
        existing.is_required = is_required
        existing.content = full        # store assessment as initial content
    else:
        db.add(XSD(
            change_request_id=change_id,
            content=full,
            version=1,
            is_required=is_required,
            status=XSDStatus.DRAFT,
        ))
    db.commit()

    # Fire advisory eval for TECH_SPEC_TO_XSD so the XSD page shows a verdict
    # immediately after assessment (even before any XSD is generated).
    try:
        xsd_row = (db.query(XSD)
                   .filter(XSD.change_request_id == change_id)
                   .order_by(XSD.version.desc()).first())
        xsd_decision = "REQUIRED" if is_required else "NOT_REQUIRED"
        eval_source_artifacts: dict[str, dict] = {
            "tech_spec_document": {
                "type": "tech_spec",
                "content": tech_spec.content,
            }
        }
        eval_target_artifacts: dict[str, dict] = {
            "xsd_assessment_decision": {
                "type": "xsd_assessment",
                "decision": xsd_decision,
                "xsd_decision": xsd_decision,
                "schema_content": "",  # assessment step produces decision only
                "content": full,        # keep assessment text for critic context
            }
        }
        asyncio.create_task(
            _run_advisory_with_isolated_session(
                change_request_id=change_id,
                checkpoint_id=CheckpointId.TECH_SPEC_TO_XSD,
                source_artifacts=eval_source_artifacts,
                target_artifacts=eval_target_artifacts,
                source_artifact_ids=_artifact_ids(tech_spec),
                target_artifact_ids=_artifact_ids(xsd_row),
            )
        )
    except Exception as exc:  # noqa: BLE001
        # Never break assessment UX on eval scheduling issues.
        logger.warning("XSD assess: failed to schedule eval change=%s error=%s", change_id, exc)

    logger.info("XSD assessment: change=%s required=%s", change_id, is_required)
    return {"assessment": full, "is_required": is_required}


# ── WebSocket: Tech Spec ──────────────────────────────────────────────────────

@router.websocket("/ws/changes/{change_id}/tech-spec")
async def ws_tech_spec(websocket: WebSocket, change_id: str):
    await websocket.accept()
    # Attribute every LLM call in this change-pipeline handler to the change so the Usage
    # dashboard groups it under the right flow (not 'other'). Task-local contextvar — each
    # WS connection is its own asyncio task, so this never leaks across connections.
    try:
        from app.core.observability import set_usage_context as _set_usage_ctx
        _set_usage_ctx(change_request_id=change_id)
    except Exception:
        pass
    logger.info("WS tech-spec connected: change=%s", change_id)
    db: Session = SessionLocal()
    try:
        auth_msg = await websocket.receive_text()
        token = json.loads(auth_msg).get("token", "")
        user = authenticate_ws(websocket, db, token)
        if not user:
            logger.warning("WS tech-spec auth failed: change=%s", change_id)
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            return
        logger.info("WS tech-spec auth ok: change=%s user=%s", change_id, user.username)

        cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        if not cr:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Not found"}))
            return

        # Downstream gate: block the Tech Spec while an uploaded-BRD reconciliation
        # is unresolved (the BRD's plan conflicts must be settled first).
        from app.agents.upload_reconciler import has_unresolved_reconciliation
        if has_unresolved_reconciliation(db, change_id, "brd"):
            await websocket.send_text(json.dumps({"type": "error",
                "detail": "Resolve the uploaded-BRD reconciliation conflicts before generating the Tech Spec."}))
            return

        history = _load_conversation(change_id, ConversationModule.TECH_SPEC, db)
        await websocket.send_text(json.dumps({"type": "history", "messages": history}))
        logger.info("WS tech-spec history sent: change=%s messages=%d", change_id, len(history))

        # R-4 — surface active jobs + accept replay protocol
        from app.services import job_registry
        active = job_registry.get_active_jobs(
            db, change_request_id=change_id, module="tech_spec",
        )
        if active:
            await websocket.send_text(json.dumps({"type": "active_jobs", "jobs": active}))

        # Uploaded source BRD rides along as rich input (seed, not substitute).
        from app.services.source_material import source_block
        enriched_prompt = (cr.enhanced_prompt or cr.initial_prompt) + source_block(cr)

        research = (db.query(ResearchOutput)
                    .filter(ResearchOutput.change_request_id == change_id)
                    .order_by(ResearchOutput.version.desc()).first())
        research_report = research.combined_report if research else "No research report available."

        canvas = (db.query(ProductCanvas)
                  .filter(ProductCanvas.change_request_id == change_id)
                  .order_by(ProductCanvas.version.desc()).first())
        canvas_content = canvas.content if canvas else "No canvas available."

        brd = (db.query(BRD)
               .filter(BRD.change_request_id == change_id)
               .order_by(BRD.version.desc()).first())
        brd_content = brd.content if brd else "No BRD available."

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data.get("type") == "replay_request":
                rep_job_id = (data.get("job_id") or "").strip()
                rep_since  = int(data.get("since_seq") or 0)
                if not rep_job_id:
                    continue
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await websocket.send_text(json.dumps({
                    "type": "replay", "job_id": rep_job_id, "since_seq": rep_since,
                    "chunks": [{"seq": s, "text": t} for (s, t) in chunks],
                    "count": len(chunks),
                }))
                continue

            user_msg = (data.get("message") or "").strip()
            if not user_msg:
                continue

            actual_msg = "Generate the Technical Specification." if user_msg.lower() == "start" else user_msg
            logger.info("WS tech-spec user message: change=%s len=%d", change_id, len(user_msg))

            # Compulsory ordering: the TSD is authored AGAINST the approved schemas, so the
            # XSD stage must have produced its verdict/artifact first — without this gate the
            # TSD silently re-derives the schema from the BRD's vision (the Chinese-whispers
            # gap _latest_xsd_content documents). v2 flows only: legacy v1 rows executed
            # TSD→XSD and would deadlock behind it.
            if (getattr(cr, "workflow_version", 2) or 2) >= 2:
                try:
                    from app.services.xsd_context import xsd_stage_complete
                    _xsd_ok, _xsd_reason = xsd_stage_complete(change_id, db)
                except Exception as e:  # noqa: BLE001 — gate must fail open, never wedge the TSD
                    logger.warning("WS tech-spec: XSD gate check failed change=%s (%s)", change_id, e)
                    _xsd_ok, _xsd_reason = True, ""
                if not _xsd_ok:
                    logger.info("WS tech-spec: blocked pending XSD stage change=%s", change_id)
                    await websocket.send_text(json.dumps({"type": "error", "detail": _xsd_reason}))
                    continue

            _save_message(change_id, ConversationModule.TECH_SPEC, MessageRole.USER, actual_msg, user.id, db)

            history = _load_conversation(change_id, ConversationModule.TECH_SPEC, db)
            prior_history = history[:-1]

            # Load (or lazily build) the cached context — gives us taxonomy + structured proposals.
            proposals_block = ""
            proposals_dict: dict = {}
            try:
                from app.services.context_cache import get_or_build
                from app.agents.proposals_extractor import format_for_prompt
                ctx = await get_or_build(change_id, db)
                if ctx and ctx.proposals:
                    proposals_block = format_for_prompt(ctx.proposals)
                    proposals_dict = ctx.proposals
                    logger.info(
                        "WS tech-spec: proposals confidence=%s taxonomy=%s",
                        ctx.proposals_confidence, ctx.taxonomy_primary,
                    )
            except Exception as e:
                logger.warning("WS tech-spec: context_cache unavailable (%s) — proceeding without proposals", e)

            # PM clarification answers — authoritative
            clarification_answers = ""
            try:
                from app.services.clarification_loader import load_answers_block
                clarification_answers = load_answers_block(change_id, db)
                if clarification_answers:
                    logger.info("WS tech-spec: clarification answers loaded (%d chars)", len(clarification_answers))
            except Exception as e:
                logger.warning("WS tech-spec: clarification_loader failed (%s)", e)

            # ── Docgen pipeline dispatch ────────────────────────────────────
            # All TSD generation routes through the LangGraph docgen pipeline.
            # Additional_context includes the approved BRD content (the TSD
            # builds on it).
            full_response = ""
            docgen_job_id: str | None = None
            docgen_docx_override: str | None = None
            _tsd_xsd = ""          # changed schema(s) — authoritative
            _tsd_xsd_bundle = ""   # unchanged schemas involved in the flow

            # R-4 — durable agent job for the entire turn.
            registry_job_id = job_registry.create_job(
                db,
                change_request_id=change_id,
                module="tech_spec",
                subtype="generate" if actual_msg == "Generate the Technical Specification." else "refine",
                started_by_user_id=user.id,
                metadata={
                    "trigger":         actual_msg[:120],
                },
            )
            await websocket.send_text(json.dumps({
                "type": "job_id", "job_id": registry_job_id, "module": "tech_spec",
            }))

            from app.services.docgen_runner import (
                build_initial_state, run_pipeline_in_thread,
                edit_full_document_in_thread, sections_to_markdown,
                emit_stage_progress_text, get_latest_job, set_latest_job,
            )
            from app.docgen.plan_store import artifact_dir
            import os as _os

            prior_job_id = get_latest_job(change_id, "TSD")
            # Always-fresh dispatch (Option A) — see ws_brd for rationale.
            is_revise = False  # forced off

            if is_revise:
                logger.info("WS tech-spec: docgen edit on prior job_id=%s", prior_job_id)
                job_registry.update_job(db, registry_job_id, current_stage="Applying revision")
                await job_registry.ws_send_chunk(
                    websocket, registry_job_id,
                    emit_stage_progress_text("writing") + "\nApplying revision to all sections…\n",
                )
                try:
                    new_docx_path = await edit_full_document_in_thread(prior_job_id, actual_msg)
                except Exception as e:
                    logger.exception("WS tech-spec: docgen edit failed")
                    job_registry.fail_job(db, registry_job_id, error=str(e),
                                          final_stage="Revision failed")
                    await websocket.send_text(json.dumps({
                        "type": "error", "detail": "An internal error occurred",
                        "job_id": registry_job_id,
                    }))
                    continue
                sections_path = artifact_dir(prior_job_id) / "generated_sections.json"
                plan_path = artifact_dir(prior_job_id) / "document_plan.json"
                edited_sections = json.loads(sections_path.read_text(encoding="utf-8"))
                edited_plan = json.loads(plan_path.read_text(encoding="utf-8"))
                full_response = sections_to_markdown(edited_plan, edited_sections)
                docgen_job_id = prior_job_id
                docgen_docx_override = new_docx_path
            else:
                logger.info("WS tech-spec: docgen fresh pipeline run")
                job_registry.update_job(
                    db, registry_job_id,
                    current_stage="Retrieving knowledge base context",
                )
                await job_registry.ws_send_chunk(
                    websocket, registry_job_id,
                    emit_stage_progress_text("retrieving"),
                )

                # TSD's primary input is the approved BRD, with research /
                # canvas / clarifications all folded into additional_context.
                additional_ctx_parts = []
                if brd_content and brd_content != "No BRD available.":
                    additional_ctx_parts.append("--- Approved BRD ---\n" + brd_content)
                # Accuracy S6/S5: on the reordered (v2) flow the XSD is approved
                # BEFORE the TSD — feed the REAL approved schemas so the TSD documents
                # decided reality instead of inventing XML. No-op on v1 (no XSD yet).
                _tsd_xsd = _latest_xsd_content(change_id, db)
                if _tsd_xsd.strip():
                    additional_ctx_parts.append(
                        "--- APPROVED XSD SCHEMAS (AUTHORITATIVE — reproduce these real "
                        "element/field/namespace names; do NOT invent XML) ---\n" + _tsd_xsd)
                # The UNCHANGED schemas the flow rides on (siblings/imports of the changed
                # one) — the wire sections derive their XML samples + field tables from
                # these instead of re-inventing existing message structure.
                try:
                    from app.services.xsd_context import build_involved_xsd_bundle
                    _tsd_xsd_bundle = build_involved_xsd_bundle(change_id, db)
                except Exception as e:  # noqa: BLE001 — grounding is best-effort, never blocks
                    logger.warning("WS tech-spec: involved-XSD bundle failed change=%s (%s)", change_id, e)
                if clarification_answers:
                    additional_ctx_parts.append("--- PM Clarifications ---\n" + clarification_answers)
                if actual_msg and actual_msg != "Generate the Technical Specification.":
                    additional_ctx_parts.append(
                        "## USER REVISION INTENT\n"
                        "Treat the following as feedback for this regeneration. "
                        "Address it explicitly. Do NOT skip any blueprint section.\n\n"
                        + actual_msg
                    )
                try:
                    prior_tsd_row = (db.query(TechSpec)
                                     .filter(TechSpec.change_request_id == change_id)
                                     .order_by(TechSpec.version.desc()).first())
                    if prior_tsd_row and (prior_tsd_row.content or "").strip():
                        additional_ctx_parts.append(
                            "## PRIOR TSD DRAFT (continuity reference)\n"
                            "Preserve good prior content where the user did not "
                            "request changes. Always produce the full blueprint structure.\n\n"
                            + prior_tsd_row.content[:30000]
                        )
                except Exception as _e:
                    logger.warning("WS tech-spec: prior TSD load failed: %s", _e)
                additional_context = "\n\n".join(additional_ctx_parts)

                # Bind every TSD section writer to the BRD's actual flows
                # (FRs + architecture). Without this the writers only see
                # planner-compressed instructions — the BRD/TSD deviation cause.
                from app.services.doc_skeleton import brd_flow_skeleton
                tsd_source_skeleton = (
                    brd_flow_skeleton(brd_content)
                    if brd_content and brd_content != "No BRD available." else ""
                )
                state = build_initial_state(
                    doc_type="TSD",
                    change_id=change_id,
                    prompt=enriched_prompt,
                    document_title=(f"Technical Specification: {cr.title}" if cr.title
                                    else f"TSD: {cr.initial_prompt[:60]}"),
                    audience="Tech Leads, Architects, InfoSec, Risk",
                    desired_outcome="Approved Technical Specification",
                    research_report=research_report,
                    canvas_content=canvas_content,
                    additional_context=additional_context,
                    # Dedicated, UNTRUNCATED design block (classes/methods/keys/codes) so the TSD
                    # names the real implementation verbatim — bypasses the feature_prompt[:3000] slice.
                    tech_design=_tech_design_block(change_id, db),
                    include_diagrams=True,
                    use_rag=True,
                    proposals=proposals_dict,
                    source_skeleton=tsd_source_skeleton,
                    decisions_block=_decisions_block(change_id, db),
                    source_flow_spec=_flow_spec(change_id, db),
                    source_xsd=_tsd_xsd,
                    source_xsd_bundle=_tsd_xsd_bundle,
                )

                # Deterministic API-spec injection — field dictionaries for the wire
                # APIs this change touches are RENDERED from API Registry rows in the
                # docgen pipeline (write_content), not model-generated. Registry misses
                # simply fall back to the existing LLM interface_spec behaviour.
                #
                # The CORE set derives from RATIFIED/stable inputs only — approved XSD,
                # involved-schema bundle, ratified flow spec. LLM proposals are excluded:
                # they vary run-to-run and made the TSD's registry-section set
                # nondeterministic (QA finding I2). Any registry API the writer merely
                # MENTIONS is swept in post-write by the pipeline (QA finding I1), so
                # every generated doc satisfies the registry eval check by construction.
                try:
                    from app.services.api_registry_ingest import (
                        derive_involved_api_names, registry_specs_all)
                    _all_specs = registry_specs_all(db)
                    _core = [n for n in derive_involved_api_names(
                        _tsd_xsd, _tsd_xsd_bundle,
                        json.dumps(state.get("source_flow_spec") or {}, default=str),
                    ) if n in _all_specs]
                    state["api_registry_specs_by_name"] = _all_specs
                    state["api_registry_core_names"] = _core
                    if _core:
                        logger.info("WS tech-spec: registry-backed API specs (core): %s", _core)
                except Exception as e:  # noqa: BLE001 — registry is additive, never blocks the TSD
                    logger.warning("WS tech-spec: API-registry spec injection failed: %s", e)
                    state["api_registry_specs_by_name"] = {}
                    state["api_registry_core_names"] = []

                _stage_labels = {
                    "retrieving":          "Retrieving knowledge base context",
                    "planning":            "Planning document structure",
                    "generating_diagrams": "Generating UML diagrams",
                    "writing":             "Writing section content",
                    "reviewing":           "Validating sections",
                    "assembling":          "Building .docx",
                }

                async def _emit_stage(stage: str) -> None:
                    label = _stage_labels.get(stage, stage)
                    # Registry + Redis first, live socket last — see the BRD
                    # handler for why the ordering matters on a resumed page.
                    try:
                        job_registry.update_job(db, registry_job_id, current_stage=label)
                        await job_registry.ws_send_chunk(
                            websocket, registry_job_id,
                            emit_stage_progress_text(stage),
                        )
                    except Exception:
                        pass
                    try:
                        await websocket.send_text(json.dumps({
                            "type":          "progress",
                            "job_id":        registry_job_id,
                            "current_stage": label,
                        }))
                    except Exception:
                        pass

                # R-9 — cooperative cancel: check the registry every poll
                # tick. Returning True from cancel_check abandons the
                # pipeline await and returns a synthetic cancelled state.
                def _check_cancel() -> bool:
                    try:
                        return job_registry.is_cancelled(db, registry_job_id)
                    except Exception:
                        return False

                final_state = await run_pipeline_in_thread(
                    state, on_stage=_emit_stage, cancel_check=_check_cancel,
                )

                # R-9 — handle cooperative cancel: skip fail_job (the
                # cancel API already wrote `cancelled` to the registry),
                # just inform the client and bail out of this turn.
                if final_state.get("status") == "cancelled":
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "detail": "Cancelled by user",
                        "job_id": registry_job_id,
                        "cancelled": True,
                    }))
                    continue

                if final_state.get("status") == "failed":
                    logger.warning("WS tech-spec: docgen pipeline failed err=%s",
                                   final_state.get("error"))
                    job_registry.fail_job(
                        db, registry_job_id,
                        error=str(final_state.get("error") or "pipeline failed"),
                        final_stage="Pipeline failed",
                    )
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "detail": "An internal error occurred",
                        "job_id": registry_job_id,
                    }))
                    continue

                plan = final_state.get("document_plan") or {}
                sections = final_state.get("generated_sections") or []
                full_response = sections_to_markdown(plan, sections)
                docgen_job_id = final_state.get("job_id")
                docgen_docx_override = final_state.get("output_path")
                if docgen_job_id:
                    set_latest_job(change_id, "TSD", docgen_job_id)

                logger.info(
                    "WS tech-spec: docgen complete change=%s job=%s len=%d docx=%s",
                    change_id, docgen_job_id, len(full_response),
                    bool(docgen_docx_override),
                )

            # Send the assembled markdown as a single chunk so the UI's
            # ReactMarkdown renderer sees the same text the legacy path
            # would have streamed. Mirror to chunk buffer.
            await job_registry.ws_send_chunk(websocket, registry_job_id, full_response)

            # Plan-consistency enforcement — keep the TSD's technical surface within the ratified
            # plan; auto-correct a blocker up to MAX_REPAIR_ATTEMPTS times BEFORE persistence. Runs ahead of the
            # W0-C BRD↔TSD repair below (separate concern). No hard block.
            doc_consistency = None
            try:
                from app.agents.plan_contract import build_plan_contract
                from app.agents.doc_consistency import enforce_plan_consistency, reconcile_doc_to_plan, MAX_REPAIR_ATTEMPTS
                _pc = build_plan_contract(db, change_id)
                if _pc:
                    async def _repair_tsd(instruction: str, attempt: int, current_content: str, items: list[str]) -> str:
                        nonlocal docgen_docx_override
                        await job_registry.ws_send_chunk(
                            websocket, registry_job_id,
                            f"\n⟳ TSD diverges from the ratified plan — auto-correcting (attempt {attempt}/{MAX_REPAIR_ATTEMPTS})…\n")
                        if docgen_job_id:
                            from app.services.docgen_runner import (
                                edit_divergent_sections_in_thread as _edit_doc,
                                sections_to_markdown as _secs_to_md,
                            )
                            from app.docgen.plan_store import artifact_dir as _adir
                            _new_docx = await _edit_doc(docgen_job_id, instruction, items)
                            _sec = json.loads((_adir(docgen_job_id) / "generated_sections.json").read_text(encoding="utf-8"))
                            _pln = json.loads((_adir(docgen_job_id) / "document_plan.json").read_text(encoding="utf-8"))
                            docgen_docx_override = _new_docx
                            return _secs_to_md(_pln, _sec)
                        return await reconcile_doc_to_plan(
                            doc_kind="TSD", doc_content=current_content,
                            plan_contract=_pc, instruction=instruction)

                    _enf = await enforce_plan_consistency(
                        doc_kind="TSD", doc_content=full_response,
                        plan_contract=_pc, repair_fn=_repair_tsd)
                    full_response = _enf["content"]
                    doc_consistency = _enf["consistency"]
                    if _enf["repaired"]:
                        # Status line only — see the BRD path: re-sending the body appends a
                        # second copy to the append-only chunk buffer. `done.full` is authoritative.
                        await job_registry.ws_send_chunk(
                            websocket, registry_job_id,
                            "\n✓ Corrected to match the ratified plan — the final document "
                            "replaces this preview when generation completes.\n")
                    if doc_consistency.get("findings"):
                        logger.warning("TSD consistency: change=%s has_blocker=%s findings=%d repaired=%s",
                                       change_id, doc_consistency.get("has_blocker"),
                                       len(doc_consistency["findings"]), _enf["repaired"])
            except Exception as e:  # noqa: BLE001 — never block generation on the consistency machinery
                logger.warning("TSD consistency enforcement failed: %s", e)

            # Cross-document BRD↔TSD gate (advisory): flag functional-requirement
            # ids the TSD invents or drops relative to the BRD. Never blocks —
            # surfaces a banner so the author/certifier sees the divergence.
            # We also remember whether the TSD came out BRD-consistent: a clean result
            # here means the doc faithfully realises its approved BRD, and the eval-driven
            # auto-repair below must NOT rewrite it on the strength of an LLM/format-shape
            # FAIL alone (see the repair gate for why).
            _tsd_consistent_with_brd = False
            try:
                from app.agents.cross_doc_consistency import check_cross_doc
                if brd_content and brd_content != "No BRD available.":
                    _xd = check_cross_doc(brd_content, full_response)
                    _findings = _xd.get("findings") or []
                    _tsd_consistent_with_brd = bool(_xd.get("consistent")) and not _findings
                    # Always log that the gate ran (even when clean) so there is runtime
                    # evidence — not only when it finds a divergence.
                    logger.info("TSD cross-doc check ran: change=%s findings=%d consistent=%s",
                                change_id, len(_findings), _xd.get("consistent"))
                    if _findings:
                        _items = "; ".join(f"{f['item']} ({f['kind']})" for f in _findings)[:400]
                        logger.warning("TSD cross-doc: change=%s findings=%d items=%s",
                                       change_id, len(_findings), _items)
                        await job_registry.ws_send_chunk(
                            websocket, registry_job_id,
                            f"\n⚠ BRD↔TSD check: {len(_findings)} FR mismatch(es) — {_items}\n")
            except Exception as e:  # noqa: BLE001 — advisory only
                logger.warning("TSD cross-doc check failed: %s", e)

            # XSD↔TSD tag tripwire (advisory, never blocks): every XML element tag in the
            # TSD's samples must exist in the schemas it was grounded on. With the
            # input-side grounding above this stays silent; if the plumbing regresses it
            # names the invented tags instead of a PM discovering them post-approval.
            try:
                from app.services.xsd_context import xml_tag_tripwire
                if _tsd_xsd.strip() or _tsd_xsd_bundle.strip():
                    _unknown_tags = xml_tag_tripwire(full_response, _tsd_xsd, _tsd_xsd_bundle)
                    if _unknown_tags:
                        logger.warning("XSD↔TSD tripwire: change=%s unknown_tags=%s",
                                       change_id, _unknown_tags[:20])
                        await job_registry.ws_send_chunk(
                            websocket, registry_job_id,
                            f"\n⚠ XSD↔TSD check: {len(_unknown_tags)} XML tag(s) in the TSD samples "
                            f"are not defined in the approved/involved schemas — "
                            f"{', '.join(_unknown_tags[:12])}\n")
            except Exception as e:  # noqa: BLE001 — advisory only
                logger.warning("XSD↔TSD tripwire failed: %s", e)

            _save_message(change_id, ConversationModule.TECH_SPEC, MessageRole.ASSISTANT, full_response, None, db)

            existing = (db.query(TechSpec)
                        .filter(TechSpec.change_request_id == change_id)
                        .order_by(TechSpec.version.desc()).first())
            # ADR-0005 / SDLC review gap 4 — auto-approve on generate. No explicit
            # "approve the TSD" UI action exists yet (unlike BRD, which has
            # submit_brd/respond_approval); until one ships, generating a TSD IS
            # today's de-facto approval — this just makes that state EXPLICIT and
            # version-lockable for agentic_tsd_approval_gate, instead of leaving
            # every TSD stuck at status=DRAFT forever. Set False once a real TSD
            # approval flow exists.
            _auto_approve = getattr(_app_settings, "agentic_tsd_auto_approve_on_generate", True)
            if existing:
                existing.content = full_response
                existing.version += 1
                if _auto_approve:
                    existing.status = ArtifactStatus.APPROVED
                    existing.approved_by = None
                    from datetime import datetime as _dt, timezone as _tz
                    existing.approved_at = _dt.now(_tz.utc)
            else:
                _new_ts = TechSpec(change_request_id=change_id, content=full_response, version=1)
                if _auto_approve:
                    _new_ts.status = ArtifactStatus.APPROVED
                    from datetime import datetime as _dt, timezone as _tz
                    _new_ts.approved_at = _dt.now(_tz.utc)
                db.add(_new_ts)
            db.commit()

            ts_row = (db.query(TechSpec).filter(TechSpec.change_request_id == change_id)
                      .order_by(TechSpec.version.desc()).first())

            eval_source_artifacts: dict[str, dict] = {}
            if brd and brd.content:
                eval_source_artifacts["brd_document"] = {
                    "type": "brd",
                    "content": brd.content,
                }
            eval_target_artifacts = {
                "tech_spec_document": {
                    "type": "tech_spec",
                    "content": full_response,
                }
            }

            # W0-C — close the BRD→TSD consistency loop. ONLY the docgen path can
            # auto-repair (it has a job_id to edit), so only there do we pay for a
            # synchronous (blocking) eval; every other path keeps the original
            # non-blocking fire-and-forget eval. On a genuine content FAIL we run
            # ONE bounded repair via the docgen edit path, then record a fresh
            # verdict against the repaired content. A missing-artifact FAIL is an
            # infra problem, not a deviation — it never triggers a repair.
            repair_eligible = bool(docgen_job_id)
            if not repair_eligible:
                asyncio.create_task(
                    _run_advisory_with_isolated_session(
                        change_request_id=change_id,
                        checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
                        source_artifacts=eval_source_artifacts,
                        target_artifacts=eval_target_artifacts,
                        source_artifact_ids=_artifact_ids(brd),
                        target_artifact_ids=_artifact_ids(ts_row),
                    )
                )
            else:
                verdict_row = None
                _eval_db = SessionLocal()
                try:
                    verdict_row = await run_advisory(
                        db=_eval_db,
                        change_request_id=change_id,
                        checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
                        source_artifacts=eval_source_artifacts,
                        target_artifacts=eval_target_artifacts,
                        source_artifact_ids=_artifact_ids(brd),
                        target_artifact_ids=_artifact_ids(ts_row),
                    )
                except Exception as _eval_exc:
                    logger.warning("WS tech-spec: synchronous eval failed (%s)", _eval_exc)
                finally:
                    _eval_db.close()

                # An eval FAIL only earns an auto-repair when the deterministic BRD↔TSD
                # gate ALSO saw a divergence. If cross-doc says the TSD is consistent with
                # its approved BRD, a FAIL here is not a real deviation — it is format/shape
                # noise the section editor cannot act on (e.g. the deterministic
                # mandatory-section check keys on literal "## Overview" markdown headers the
                # docgen renderer never emits, or a critic that could not run). Rewriting a
                # ratified-consistent doc on that basis is the churn bug: a content_applied=0
                # no-op pass plus a phantom TechSpec version. The verdict is still recorded +
                # bannered (advisory) — it just never auto-overrides a doc that already
                # matches its approved source. Schema (S6) and ledger (S8) findings below are
                # orthogonal, deterministic and actionable, so they still repair on their own.
                _is_content_fail = (
                    verdict_row is not None
                    and verdict_row.verdict == "FAIL"
                    and "MISSING_REQUIRED_ARTIFACT" not in (verdict_row.hard_fail_codes or [])
                    and bool(brd and brd.content)
                    and not _tsd_consistent_with_brd
                )
                # S6 — also repair when the TSD's XML blocks don't match the approved
                # schemas (v2: real XSDs exist before the TSD). No-op on v1 (no XSD yet).
                _xml_findings = []
                try:
                    from app.services.xsd_validation import validate_xml_blocks
                    _xc = _latest_xsd_content(change_id, db)
                    if _xc.strip():
                        _xml_findings = validate_xml_blocks(full_response, [_xc])
                except Exception as _xv_exc:  # noqa: BLE001
                    logger.warning("WS tech-spec: xml-validation skipped (%s)", _xv_exc)
                # S8 — flag any ratified decision the TSD ignored, and repair it too.
                _ledger_findings = []
                try:
                    from app.services.ledger_coverage import ledger_coverage_findings
                    _ledger_findings = ledger_coverage_findings(db, change_id, full_response)
                except Exception as _lc_exc:  # noqa: BLE001
                    logger.warning("WS tech-spec: ledger-coverage skipped (%s)", _lc_exc)
                _findings = (list(verdict_row.hard_fail_codes or []) + [
                    r if isinstance(r, str) else (r.get("message") or str(r))
                    for r in (verdict_row.reasons_json or [])
                ] if _is_content_fail else []) + list(_xml_findings) + list(_ledger_findings)
                # Gate the banner + auto-repair on ACTUAL findings, not merely a fail
                # verdict: an eval that errored (e.g. critic unreachable) can set
                # _is_content_fail with no concrete findings — firing the "flagged
                # deviations" banner + a repair pass then is misleading and wasteful.
                if _findings:
                    _findings_text = "; ".join(_findings)[:1500]
                    repair_instruction = (
                        "CONSISTENCY REPAIR — this Technical Spec deviated from the approved BRD "
                        "and/or the approved schemas. Correct ONLY these issues and keep everything "
                        "else identical; do NOT introduce new flows, fields, or requirements; "
                        "reproduce the approved XSD element/field names exactly: " + _findings_text
                    )
                    try:
                        from app.services.docgen_runner import (
                            edit_full_document_in_thread as _edit_doc,
                            sections_to_markdown as _secs_to_md,
                        )
                        from app.docgen.plan_store import artifact_dir as _adir
                        await job_registry.ws_send_chunk(
                            websocket, registry_job_id,
                            "\n⟳ Consistency check flagged BRD↔TSD deviations — applying one auto-repair pass…\n",
                        )
                        _pre_repair = full_response          # snapshot BEFORE the repair rewrites it
                        _new_docx = await _edit_doc(docgen_job_id, repair_instruction)
                        _sec = json.loads((_adir(docgen_job_id) / "generated_sections.json").read_text(encoding="utf-8"))
                        _pln = json.loads((_adir(docgen_job_id) / "document_plan.json").read_text(encoding="utf-8"))
                        full_response = _secs_to_md(_pln, _sec)
                        # Canonical-fidelity guard — a repair may fix its flagged issue but must NEVER
                        # drop or re-case a ratified canonical value (a fixed enum / cap / code). If a
                        # value present before the repair is gone after it, the repair over-reached:
                        # revert to the pre-repair text so a deliberately-fixed value always wins over
                        # an auto-rewrite (this is the invariant that lets us trust the repair in prod).
                        from app.agents.plan_contract import canonical_values as _canon
                        _dropped = [v for v in _canon(db, change_id)
                                    if v and v in _pre_repair and v not in full_response]
                        if _dropped:
                            full_response = _pre_repair
                            logger.warning("WS tech-spec: auto-repair REVERTED — would have dropped "
                                           "ratified value(s) %s", _dropped[:5])
                            await job_registry.ws_send_chunk(
                                websocket, registry_job_id,
                                "\n↩ Auto-repair reverted — it would have dropped ratified value(s): "
                                + ", ".join(_dropped[:5]) + ". Keeping the approved content.\n")
                            # No body re-send: the banner says what happened and the stream already
                            # holds this exact (pre-repair) text — resending duplicates it on screen.
                        else:
                            docgen_docx_override = _new_docx
                            if ts_row:
                                ts_row.content = full_response       # repaired content...
                                ts_row.version += 1                  # ...is a new version (audit trail)
                                db.commit()
                            eval_target_artifacts["tech_spec_document"]["content"] = full_response
                            await job_registry.ws_send_chunk(
                                websocket, registry_job_id,
                                "\n✓ Auto-repair applied — the final document replaces this "
                                "preview when generation completes.\n")
                            logger.info("WS tech-spec: auto-repair applied for change=%s", change_id)
                            # Re-evaluate the repaired content for the record — the only
                            # case where a second eval pass is justified (content changed).
                            asyncio.create_task(
                                _run_advisory_with_isolated_session(
                                    change_request_id=change_id,
                                    checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
                                    source_artifacts=eval_source_artifacts,
                                    target_artifacts=eval_target_artifacts,
                                    source_artifact_ids=_artifact_ids(brd),
                                    target_artifact_ids=_artifact_ids(ts_row),
                                )
                            )
                    except Exception as _repair_exc:
                        logger.warning("WS tech-spec: auto-repair failed (%s) — keeping original", _repair_exc)

            validation = summarize_validation(validate_doc(full_response, doc_type="tech_spec"))

            # When docgen owns assembly the .docx already exists on disk —
            # honour that path rather than re-running the legacy assembler.
            if docgen_docx_override:
                docx_path = docgen_docx_override
            else:
                docx_path = _assemble_docx_safe(
                    full_response, change_id=change_id, doc_type="Technical Specification",
                    version=ts_row.version if ts_row else 1,
                    cr_title=cr.title or cr.initial_prompt[:80],
                )
            if docx_path and ts_row:
                ts_row.docx_path = docx_path
                db.commit()

            # R-4 — mark the durable job complete.
            job_registry.complete_job(
                db, registry_job_id,
                result={
                    "ts_id":          ts_row.id if ts_row else None,
                    "version":        ts_row.version if ts_row else None,
                    "markdown_chars": len(full_response),
                    "docx_path":      docx_path,
                    "docgen_job_id":  docgen_job_id,
                    "validation":     validation,
                },
                final_stage="Tech Spec ready",
            )

            done_payload = {
                "type": "done",
                "full": full_response,
                "validation": validation,
                "docx_available": bool(docx_path),
                "job_id": registry_job_id,
                "doc_consistency": doc_consistency,
            }
            if docgen_job_id:
                # Surfaced for STEP 7 frontend section-wise edit dropdown.
                done_payload["docgen_job_id"] = docgen_job_id
            await websocket.send_text(json.dumps(done_payload))

    except WebSocketDisconnect:
        logger.info("WS tech-spec disconnected: change=%s", change_id)
    except Exception as e:
        logger.exception("WS tech-spec error: change=%s", change_id)
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": "An internal error occurred"}))
        except Exception:
            pass
    finally:
        db.close()


# ── WebSocket: XSD ────────────────────────────────────────────────────────────

@router.websocket("/ws/changes/{change_id}/xsd")
async def ws_xsd(websocket: WebSocket, change_id: str):
    await websocket.accept()
    # Attribute every LLM call in this change-pipeline handler to the change so the Usage
    # dashboard groups it under the right flow (not 'other'). Task-local contextvar — each
    # WS connection is its own asyncio task, so this never leaks across connections.
    try:
        from app.core.observability import set_usage_context as _set_usage_ctx
        _set_usage_ctx(change_request_id=change_id)
    except Exception:
        pass
    logger.info("WS xsd connected: change=%s", change_id)
    db: Session = SessionLocal()
    try:
        auth_msg = await websocket.receive_text()
        token = json.loads(auth_msg).get("token", "")
        user = authenticate_ws(websocket, db, token)
        if not user:
            logger.warning("WS xsd auth failed: change=%s", change_id)
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            return
        logger.info("WS xsd auth ok: change=%s user=%s", change_id, user.username)

        cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        if not cr:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Not found"}))
            return

        # Downstream gate: block the XSD while an uploaded-BRD reconciliation is
        # unresolved (the BRD's plan conflicts must be settled first).
        from app.agents.upload_reconciler import has_unresolved_reconciliation
        if has_unresolved_reconciliation(db, change_id, "brd"):
            await websocket.send_text(json.dumps({"type": "error",
                "detail": "Resolve the uploaded-BRD reconciliation conflicts before generating the XSD."}))
            return

        history = _load_conversation(change_id, ConversationModule.XSD, db)
        await websocket.send_text(json.dumps({"type": "history", "messages": history}))
        logger.info("WS xsd history sent: change=%s messages=%d", change_id, len(history))

        # R-4 — surface active jobs + accept replay protocol
        from app.services import job_registry
        active = job_registry.get_active_jobs(
            db, change_request_id=change_id, module="xsd",
        )
        if active:
            await websocket.send_text(json.dumps({"type": "active_jobs", "jobs": active}))

        tech_spec = (db.query(TechSpec)
                     .filter(TechSpec.change_request_id == change_id)
                     .order_by(TechSpec.version.desc()).first())
        tech_spec_content = (tech_spec.content if tech_spec
                             else (_decisions_block(change_id, db) or "No tech spec available."))

        brd = (db.query(BRD)
               .filter(BRD.change_request_id == change_id)
               .order_by(BRD.version.desc()).first())
        brd_content = brd.content if brd else "No BRD available."

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data.get("type") == "replay_request":
                rep_job_id = (data.get("job_id") or "").strip()
                rep_since  = int(data.get("since_seq") or 0)
                if not rep_job_id:
                    continue
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await websocket.send_text(json.dumps({
                    "type": "replay", "job_id": rep_job_id, "since_seq": rep_since,
                    "chunks": [{"seq": s, "text": t} for (s, t) in chunks],
                    "count": len(chunks),
                }))
                continue

            user_msg = (data.get("message") or "").strip()
            if not user_msg:
                continue

            actual_msg = "Generate the XSD changes." if user_msg.lower() == "start" else user_msg
            logger.info("WS xsd user message: change=%s len=%d", change_id, len(user_msg))

            _save_message(change_id, ConversationModule.XSD, MessageRole.USER, actual_msg, user.id, db)

            history = _load_conversation(change_id, ConversationModule.XSD, db)
            prior_history = history[:-1]

            # R-4 — durable job
            registry_job_id = job_registry.create_job(
                db,
                change_request_id=change_id,
                module="xsd",
                subtype="generate" if user_msg.lower() == "start" else "refine",
                started_by_user_id=user.id,
                metadata={"trigger": user_msg[:120]},
            )
            await websocket.send_text(json.dumps({
                "type": "job_id", "job_id": registry_job_id, "module": "xsd",
            }))
            xsd_milestones = [
                (0,     "Analyzing message structure",  5),
                (2000,  "Generating XSD",              30),
                (6000,  "Adding constraints & types",  65),
                (12000, "Finalizing schema",           90),
            ]
            stage_idx = job_registry.advance_stage_by_chars(
                db, registry_job_id, 0, xsd_milestones, 0,
            )

            logger.info("WS xsd streaming started: change=%s", change_id)
            full_response = ""
            try:
                async for chunk in stream_xsd_turn(tech_spec_content, brd_content, prior_history, actual_msg):
                    full_response += chunk
                    await job_registry.ws_send_chunk(websocket, registry_job_id, chunk)
                    stage_idx = job_registry.advance_stage_by_chars(
                        db, registry_job_id, len(full_response), xsd_milestones, stage_idx,
                    )
            except Exception as exc:
                logger.exception("WS xsd streaming failed")
                job_registry.fail_job(db, registry_job_id, error=str(exc))
                await websocket.send_text(json.dumps({
                    "type": "error", "detail": "An internal error occurred", "job_id": registry_job_id,
                }))
                continue
            logger.info("WS xsd streaming done: change=%s response_len=%d", change_id, len(full_response))

            _save_message(change_id, ConversationModule.XSD, MessageRole.ASSISTANT, full_response, None, db)

            existing = (db.query(XSD)
                        .filter(XSD.change_request_id == change_id)
                        .order_by(XSD.version.desc()).first())
            if existing:
                existing.content = full_response
                existing.version += 1
            else:
                db.add(XSD(change_request_id=change_id, content=full_response, version=1, is_required=True))
            db.commit()

            xsd_row = (db.query(XSD)
                       .filter(XSD.change_request_id == change_id)
                       .order_by(XSD.version.desc()).first())
            xsd_decision = "REQUIRED" if (xsd_row.is_required if xsd_row else True) else "NOT_REQUIRED"

            eval_source_artifacts: dict[str, dict] = {}
            if tech_spec and tech_spec.content:
                eval_source_artifacts["tech_spec_document"] = {
                    "type": "tech_spec",
                    "content": tech_spec.content,
                }
            eval_target_artifacts = {
                "xsd_assessment_decision": {
                    "type": "xsd_assessment",
                    "decision": xsd_decision,
                    "xsd_decision": xsd_decision,
                    "schema_content": full_response,
                    "content": full_response,
                }
            }
            asyncio.create_task(
                _run_advisory_with_isolated_session(
                    change_request_id=change_id,
                    checkpoint_id=CheckpointId.TECH_SPEC_TO_XSD,
                    source_artifacts=eval_source_artifacts,
                    target_artifacts=eval_target_artifacts,
                    source_artifact_ids=_artifact_ids(tech_spec),
                    target_artifact_ids=_artifact_ids(xsd_row),
                )
            )

            validation = summarize_validation(validate_doc(full_response, doc_type="xsd"))
            job_registry.complete_job(
                db, registry_job_id,
                result={"markdown_chars": len(full_response), "validation": validation},
                final_stage="XSD ready",
            )
            await websocket.send_text(json.dumps({
                "type": "done", "full": full_response, "validation": validation,
                "job_id": registry_job_id,
            }))

    except WebSocketDisconnect:
        logger.info("WS xsd disconnected: change=%s", change_id)
    except Exception as e:
        logger.exception("WS xsd error: change=%s", change_id)
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": "An internal error occurred"}))
        except Exception:
            pass
    finally:
        db.close()


# ── REST: Product Kit ─────────────────────────────────────────────────────────

# Generatable doc types = the enum MINUS retired ones (see app.models.product_kit).
# Deriving this from the enum alone silently kept `product_doc` alive: the bulk generator
# falls back to `list(VALID_DOC_TYPES)` when the client omits a selection, so "generate
# all" produced a retired document that dispatch then shipped next to its replacement.
VALID_DOC_TYPES = active_doc_types()


def _settings_enabled(value, default: bool = False) -> bool:
    """Parse bool-ish settings that may come from env or DB config strings."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


@router.get("/changes/{change_id}/product-kit")
def get_product_kit_all(
    change_id: str, db: DbDep, _: CurrentUser, negotiation_version: int | None = None,
):
    """Return status of all 9 Product Kit documents.

    Defaults to the latest kit snapshot. Pass ?negotiation_version=N to view an
    earlier snapshot — `available_versions` lists what can be requested.
    """
    cr = _get_change_or_404(change_id, db)
    current_ver = getattr(cr, "negotiation_version", 1) or 1
    versions = kit_versions(db, change_id) or [current_ver]
    selected = negotiation_version if negotiation_version in versions else versions[-1]
    docs = (
        kit_docs_at_version(db, change_id, selected)
        if negotiation_version is not None
        else latest_kit_docs(db, change_id)
    )
    doc_map = {d.doc_type.value: d for d in docs}
    result = []
    for dt in ProductKitDocType:
        doc = doc_map.get(dt.value)
        result.append({
            "doc_type": dt.value,
            "has_content": bool(doc and doc.content),
            "version":     doc.version if doc else 0,
            "negotiation_version": doc.negotiation_version if doc else None,
            "status":      doc.status.value if doc else "draft",
            "id":          doc.id if doc else None,
            "source":      (doc.source.value if doc and hasattr(doc.source, "value") else "generated"),
        })
    # negotiation_version is the partner-facing kit snapshot version (v1/v2/v3),
    # bumped on each kit revision — mirrors what the partner platform shows.
    return {
        "documents": result,
        "negotiation_version": selected,
        "current_version": current_ver,
        "available_versions": versions,
    }


@router.get("/changes/{change_id}/product-kit/{doc_type}")
def get_product_kit_doc(
    change_id: str, doc_type: str, db: DbDep, _: CurrentUser,
    negotiation_version: int | None = None,
):
    """Return a specific Product Kit document — latest by default, or the
    highest version within ?negotiation_version=N for an earlier snapshot."""
    if doc_type not in VALID_DOC_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown doc_type: {doc_type}")
    _get_change_or_404(change_id, db)
    q = db.query(ProductKitDocument).filter(
        ProductKitDocument.change_request_id == change_id,
        ProductKitDocument.doc_type == ProductKitDocType(doc_type),
    )
    if negotiation_version is not None:
        q = q.filter(ProductKitDocument.negotiation_version == negotiation_version)
    doc = q.order_by(ProductKitDocument.version.desc()).first()
    if not doc:
        return {"content": None, "version": 0, "status": "draft", "doc_type": doc_type}
    return {
        "id":       doc.id,
        "content":  doc.content,
        "version":  doc.version,
        "negotiation_version": doc.negotiation_version,
        "status":   doc.status.value,
        "doc_type": doc_type,
        **_provenance(doc),
        "has_generated_version": _has_generated_version(
            ProductKitDocument, change_id, db, ProductKitDocType(doc_type)),
    }


@router.post("/changes/{change_id}/product-kit/complete")
def complete_product_kit(change_id: str, db: DbDep, _: CurrentUser):
    """Mark the Product Kit stage complete and advance change to 'completed'."""
    cr = _get_change_or_404(change_id, db)
    if cr.status not in (ChangeStatus.PRODUCT_KIT, ChangeStatus.XSD):
        raise HTTPException(status_code=400, detail="Not in product_kit stage")
    cr.status = ChangeStatus.COMPLETED
    db.commit()
    logger.info("Product kit completed: change=%s", change_id)
    return {"status": "completed"}


# ── WebSocket: Product Kit ────────────────────────────────────────────────────

@router.websocket("/ws/changes/{change_id}/product-kit/{doc_type}")
async def ws_product_kit(websocket: WebSocket, change_id: str, doc_type: str):
    if doc_type not in VALID_DOC_TYPES:
        await websocket.close(code=4000)
        return

    await websocket.accept()
    # Attribute every LLM call in this change-pipeline handler to the change so the Usage
    # dashboard groups it under the right flow (not 'other'). Task-local contextvar — each
    # WS connection is its own asyncio task, so this never leaks across connections.
    try:
        from app.core.observability import set_usage_context as _set_usage_ctx
        _set_usage_ctx(change_request_id=change_id)
    except Exception:
        pass
    logger.info("WS product-kit connected: change=%s doc_type=%s", change_id, doc_type)
    db: Session = SessionLocal()
    try:
        auth_msg = await websocket.receive_text()
        token = json.loads(auth_msg).get("token", "")
        user = authenticate_ws(websocket, db, token)
        if not user:
            logger.warning("WS product-kit auth failed: change=%s doc_type=%s", change_id, doc_type)
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            return
        logger.info("WS product-kit auth ok: change=%s user=%s doc_type=%s", change_id, user.username, doc_type)

        cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        if not cr:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Not found"}))
            return

        # Load conversation history filtered by doc_type (stored in metadata_)
        all_conv = (
            db.query(Conversation)
            .filter(
                Conversation.change_request_id == change_id,
                Conversation.module == ConversationModule.PRODUCT_KIT,
            )
            .order_by(Conversation.created_at)
            .all()
        )
        history_rows = [r for r in all_conv if (r.metadata_ or {}).get("doc_type") == doc_type]
        history = [{"role": r.role.value, "content": r.content} for r in history_rows]
        await websocket.send_text(json.dumps({"type": "history", "messages": history}))
        logger.info("WS product-kit history sent: change=%s doc_type=%s messages=%d", change_id, doc_type, len(history))

        # R-5 — surface active jobs scoped to this (change, product_kit, doc_type) tuple.
        # Subtype filter happens server-side via metadata_-style query: we fetch all
        # 'product_kit' jobs for this change and filter by .subtype == doc_type.
        from app.services import job_registry
        all_pk_active = job_registry.get_active_jobs(
            db, change_request_id=change_id, module="product_kit",
        )
        active = [j for j in all_pk_active if j.get("subtype") == doc_type]
        if active:
            await websocket.send_text(json.dumps({"type": "active_jobs", "jobs": active}))

        # Load context artifacts
        enriched_prompt = cr.enhanced_prompt or cr.initial_prompt

        research = (db.query(ResearchOutput)
                    .filter(ResearchOutput.change_request_id == change_id)
                    .order_by(ResearchOutput.version.desc()).first())
        research_report = research.combined_report if research else "No research report available."

        canvas = (db.query(ProductCanvas)
                  .filter(ProductCanvas.change_request_id == change_id)
                  .order_by(ProductCanvas.version.desc()).first())
        canvas_content = canvas.content if canvas else "No canvas available."

        brd = (db.query(BRD)
               .filter(BRD.change_request_id == change_id)
               .order_by(BRD.version.desc()).first())
        brd_content = brd.content if brd else "No BRD available."

        tech_spec = (db.query(TechSpec)
                     .filter(TechSpec.change_request_id == change_id)
                     .order_by(TechSpec.version.desc()).first())
        tech_spec_content = (tech_spec.content if tech_spec
                             else (_decisions_block(change_id, db) or "No tech spec available."))

        def _save_pk_message(role: MessageRole, content: str, uid=None):
            db.add(Conversation(
                change_request_id=change_id,
                module=ConversationModule.PRODUCT_KIT,
                role=role,
                content=content,
                metadata_={"doc_type": doc_type},
                created_by=uid,
            ))
            db.commit()

        while True:
            # WHY catch RuntimeError: when a long generation outlives the WS
            # connection, the next receive_text() can fire on an already-
            # closed transport and raise RuntimeError ("WebSocket is not
            # connected") instead of WebSocketDisconnect. Treat both as a
            # graceful exit so the handler unwinds without a noisy traceback.
            try:
                raw = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError) as exc:
                logger.info(
                    "WS product-kit recv after disconnect: change=%s doc_type=%s reason=%s",
                    change_id, doc_type, exc,
                )
                return
            data = json.loads(raw)

            # R-5 — replay protocol
            if data.get("type") == "replay_request":
                rep_job_id = (data.get("job_id") or "").strip()
                rep_since  = int(data.get("since_seq") or 0)
                if not rep_job_id:
                    continue
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await websocket.send_text(json.dumps({
                    "type": "replay", "job_id": rep_job_id, "since_seq": rep_since,
                    "chunks": [{"seq": s, "text": t} for (s, t) in chunks],
                    "count": len(chunks),
                }))
                continue

            user_msg = (data.get("message") or "").strip()
            if not user_msg:
                continue

            doc_labels = {
                "product_doc": "product document",
                "product_deck": "product deck",
                "promo_video": "promotional video script",
                "explainer_video": "explainer video script",
                "faq": "FAQ document",
                "cert_test_cases": "certification test case document",
                "circular": "NPCI technical circular",
                "manifest": "manifest file",
                "prototype_screens": "prototype screens HTML",
                # Docgen merge (Session 20+) — Product Note is the new
                # 11th doc type, generated through the LangGraph pipeline.
                "product_note": "product note",
            }
            label = doc_labels.get(doc_type, doc_type)
            actual_msg = f"Generate the {label}." if user_msg.lower() == "start" else user_msg
            logger.info("WS product-kit user message: change=%s doc_type=%s len=%d", change_id, doc_type, len(user_msg))

            _save_pk_message(MessageRole.USER, actual_msg, user.id)

            # Reload history for this doc_type
            all_conv2 = (
                db.query(Conversation)
                .filter(
                    Conversation.change_request_id == change_id,
                    Conversation.module == ConversationModule.PRODUCT_KIT,
                )
                .order_by(Conversation.created_at)
                .all()
            )
            history_rows2 = [r for r in all_conv2 if (r.metadata_ or {}).get("doc_type") == doc_type]
            prior_history = [{"role": r.role.value, "content": r.content} for r in history_rows2[:-1]]

            # Load cached proposals + PM answers (Sprint 3 + 5)
            proposals_block = ""
            clarification_answers = ""
            try:
                from app.services.context_cache import get_or_build
                from app.agents.proposals_extractor import format_for_prompt
                ctx = await get_or_build(change_id, db)
                if ctx and ctx.proposals:
                    proposals_block = format_for_prompt(ctx.proposals)
            except Exception as e:
                logger.warning("WS product-kit: context_cache unavailable (%s)", e)
            try:
                from app.services.clarification_loader import load_answers_block
                clarification_answers = load_answers_block(change_id, db)
            except Exception as e:
                logger.warning("WS product-kit: clarification_loader failed (%s)", e)

            # ── Docgen pipeline dispatch ────────────────────────────────────
            # Only `circular` and `product_note` use the docgen pipeline.
            # Every other Product Kit doc type uses the `stream_product_kit_doc`
            # agent, so the other 9 doc types (product_doc, product_deck,
            # promo_video, explainer_video, faq, cert_test_cases, manifest,
            # prototype_screens) are unaffected.
            full_response = ""
            video_script_obj = None  # set for promo_video/explainer_video
            docgen_job_id: str | None = None
            docgen_docx_override: str | None = None
            docgen_eligible = doc_type in ("circular", "product_note")

            # R-5 — durable agent job for this Product Kit turn. `subtype`
            # carries the specific doc_type so the sidebar tray and resume
            # banner can show the right label.
            registry_job_id = job_registry.create_job(
                db,
                change_request_id=change_id,
                module="product_kit",
                subtype=doc_type,
                started_by_user_id=user.id,
                metadata={
                    "trigger":         actual_msg[:120],
                    "docgen_pipeline": docgen_eligible,
                    "label":           label,
                },
            )
            await websocket.send_text(json.dumps({
                "type":    "job_id",
                "job_id":  registry_job_id,
                "module":  "product_kit",
                "subtype": doc_type,
            }))

            if docgen_eligible:
                from app.services.docgen_runner import (
                    build_initial_state, run_pipeline_in_thread,
                    edit_full_document_in_thread, sections_to_markdown,
                    emit_stage_progress_text, get_latest_job, set_latest_job,
                )
                from app.docgen.plan_store import artifact_dir
                import os as _os

                # Map UI doc_type to (docgen_doc_type, latest_job_key) — keyed
                # consistently with _DOCGEN_KEYS in the REST endpoints.
                _docgen_dispatch = {
                    "circular":     ("Circular",     "Circular/circular"),
                    "product_note": ("Product Note", "Product Note/product_note"),
                }
                docgen_doc_type, job_key = _docgen_dispatch[doc_type]

                prior_job_id = get_latest_job(change_id, job_key)
                trigger_phrase = f"Generate the {label}."
                # Always-fresh dispatch (Option A) — see ws_brd for rationale.
                is_revise = False  # forced off

                if is_revise:
                    logger.info(
                        "WS product-kit (%s): docgen edit on prior job_id=%s",
                        doc_type, prior_job_id,
                    )
                    job_registry.update_job(
                        db, registry_job_id,
                        current_stage="Applying revision",
                    )
                    await job_registry.ws_send_chunk(
                        websocket, registry_job_id,
                        emit_stage_progress_text("writing") + "\nApplying revision to all sections…\n",
                    )
                    try:
                        new_docx_path = await edit_full_document_in_thread(prior_job_id, actual_msg)
                    except Exception as e:
                        logger.exception("WS product-kit (%s): docgen edit failed", doc_type)
                        job_registry.fail_job(db, registry_job_id, error=str(e),
                                              final_stage="Revision failed")
                        await websocket.send_text(json.dumps({
                            "type": "error", "detail": "An internal error occurred",
                            "job_id": registry_job_id,
                        }))
                        continue
                    sections_path = artifact_dir(prior_job_id) / "generated_sections.json"
                    plan_path = artifact_dir(prior_job_id) / "document_plan.json"
                    edited_sections = json.loads(sections_path.read_text(encoding="utf-8"))
                    edited_plan = json.loads(plan_path.read_text(encoding="utf-8"))
                    full_response = sections_to_markdown(edited_plan, edited_sections)
                    docgen_job_id = prior_job_id
                    docgen_docx_override = new_docx_path
                else:
                    logger.info("WS product-kit (%s): docgen fresh pipeline run", doc_type)
                    job_registry.update_job(
                        db, registry_job_id,
                        current_stage="Retrieving knowledge base context",
                    )
                    await job_registry.ws_send_chunk(
                        websocket, registry_job_id,
                        emit_stage_progress_text("retrieving"),
                    )

                    # Fold approved BRD + TSD + clarifications into context.
                    additional_ctx_parts = []
                    if brd_content and brd_content != "No BRD available.":
                        additional_ctx_parts.append("--- Approved BRD ---\n" + brd_content)
                    if tech_spec_content and tech_spec_content != "No tech spec available.":
                        additional_ctx_parts.append("--- Technical Specification ---\n" + tech_spec_content)
                    if clarification_answers:
                        additional_ctx_parts.append("--- PM Clarifications ---\n" + clarification_answers)
                    if actual_msg and actual_msg != trigger_phrase:
                        additional_ctx_parts.append(
                            "## USER REVISION INTENT\n"
                            "Treat the following as feedback for this regeneration. "
                            "Address it explicitly. Do NOT skip any blueprint section.\n\n"
                            + actual_msg
                        )
                    try:
                        prior_pk_row = (
                            db.query(ProductKitDocument)
                            .filter(
                                ProductKitDocument.change_request_id == change_id,
                                ProductKitDocument.doc_type == ProductKitDocType(doc_type),
                            )
                            .order_by(ProductKitDocument.version.desc())
                            .first()
                        )
                        if prior_pk_row and (prior_pk_row.content or "").strip():
                            additional_ctx_parts.append(
                                f"## PRIOR {docgen_doc_type.upper()} DRAFT (continuity reference)\n"
                                "Preserve good prior content where the user did not request changes. "
                                "Always produce the full blueprint structure.\n\n"
                                + prior_pk_row.content[:30000]
                            )
                    except Exception as _e:
                        logger.warning("WS product-kit (%s): prior doc load failed: %s", doc_type, _e)
                    additional_context = "\n\n".join(additional_ctx_parts)

                    state = build_initial_state(
                        doc_type=docgen_doc_type,
                        change_id=change_id,
                        prompt=enriched_prompt,
                        document_title=(
                            f"{docgen_doc_type}: {cr.title}" if cr.title
                            else f"{docgen_doc_type}: {cr.initial_prompt[:60]}"
                        ),
                        audience=("Member Banks, PSPs, TPAPs" if doc_type == "circular"
                                  else "Product Managers, Tech Leads, Operations"),
                        desired_outcome=f"Approved {docgen_doc_type}",
                        research_report=research_report,
                        canvas_content=canvas_content,
                        additional_context=additional_context,
                        # Circular blueprint expects no diagrams (NPCI OC format).
                        include_diagrams=(doc_type != "circular"),
                        use_rag=True,
                    )

                    _stage_labels = {
                        "retrieving":          "Retrieving knowledge base context",
                        "planning":            "Planning document structure",
                        "generating_diagrams": "Generating UML diagrams",
                        "writing":             "Writing section content",
                        "reviewing":           "Validating sections",
                        "assembling":          "Building .docx",
                    }

                    async def _emit_stage(stage: str) -> None:
                        label_s = _stage_labels.get(stage, stage)
                        # Registry + Redis first, live socket last — see the BRD
                        # handler for why the ordering matters on a resumed page.
                        try:
                            job_registry.update_job(db, registry_job_id, current_stage=label_s)
                            await job_registry.ws_send_chunk(
                                websocket, registry_job_id,
                                emit_stage_progress_text(stage),
                            )
                        except Exception:
                            pass
                        try:
                            await websocket.send_text(json.dumps({
                                "type":          "progress",
                                "job_id":        registry_job_id,
                                "current_stage": label_s,
                            }))
                        except Exception:
                            pass

                    # R-9 — cooperative cancel: check the registry every poll
                    # tick. Returning True from cancel_check abandons the
                    # pipeline await and returns a synthetic cancelled state.
                    def _check_cancel() -> bool:
                        try:
                            return job_registry.is_cancelled(db, registry_job_id)
                        except Exception:
                            return False

                    final_state = await run_pipeline_in_thread(
                        state, on_stage=_emit_stage, cancel_check=_check_cancel,
                    )

                    # R-9 — handle cooperative cancel: skip fail_job (the
                    # cancel API already wrote `cancelled` to the registry),
                    # just inform the client and bail out of this turn.
                    if final_state.get("status") == "cancelled":
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "detail": "Cancelled by user",
                            "job_id": registry_job_id,
                            "cancelled": True,
                        }))
                        continue

                    if final_state.get("status") == "failed":
                        logger.warning(
                            "WS product-kit (%s): docgen pipeline failed err=%s",
                            doc_type, final_state.get("error"),
                        )
                        job_registry.fail_job(
                            db, registry_job_id,
                            error=str(final_state.get("error") or "pipeline failed"),
                            final_stage="Pipeline failed",
                        )
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "detail": "An internal error occurred",
                            "job_id": registry_job_id,
                        }))
                        continue

                    plan = final_state.get("document_plan") or {}
                    sections = final_state.get("generated_sections") or []
                    full_response = sections_to_markdown(plan, sections)
                    docgen_job_id = final_state.get("job_id")
                    docgen_docx_override = final_state.get("output_path")
                    if docgen_job_id:
                        set_latest_job(change_id, job_key, docgen_job_id)

                    logger.info(
                        "WS product-kit (%s): docgen complete change=%s job=%s len=%d docx=%s",
                        doc_type, change_id, docgen_job_id, len(full_response),
                        bool(docgen_docx_override),
                    )

                # Single-chunk delivery so ReactMarkdown sees the same text
                # the legacy path would have streamed. Mirror to chunk buffer.
                await job_registry.ws_send_chunk(websocket, registry_job_id, full_response)
            # ── Excel Testcase Engine fork ─────────────────────────────────
            # WHY this branch: cert_test_cases used to flow to the legacy
            # `stream_product_kit_doc` markdown stub below. The engine
            # replaces that with a LangGraph pipeline that produces an
            # NPCI-format .xlsx + .md + .docx. Gated on
            # `settings.excel_engine_enabled` (default true). To roll back
            # to the legacy markdown flow, flip the flag in env or DB config.
            elif (
                _settings_enabled(getattr(_app_settings, "excel_engine_enabled", True), True)
                and doc_type == "cert_test_cases"
            ):
                logger.info(
                    "WS product-kit excel-engine started: change=%s doc_type=%s job=%s",
                    change_id, doc_type, registry_job_id,
                )
                job_registry.update_job(
                    db, registry_job_id, current_stage=f"Generating {label}",
                )
                try:
                    from app.excel_testcase_engine.streaming import run_workflow_for_ws
                    from app.excel_testcase_engine.orchestrator.graph import _ARTIFACTS_DIR
                    # BRD/TSD-only: brief carries the user request + BRD +
                    # TSD verbatim. No canvas, research, or enriched-prompt —
                    # the engine's job is to extract test cases from BRD/TSD,
                    # not to interpret upstream context.
                    brief = (
                        f"{actual_msg}\n\n"
                        f"# BRD\n{brd_content[:6000]}\n\n"
                        f"# Tech Spec\n{tech_spec_content[:6000]}"
                    )
                    engine_scope_context = await _load_engine_scope_context(change_id, db)
                    # Default to Archetype C (full annexure) — that matches
                    # the depth the legacy markdown generator targeted. The
                    # frontend can override via options later if needed.
                    engine_result = await run_workflow_for_ws(
                        websocket=websocket,
                        brief=brief,
                        # WHY pause_on_questions=False: cert_test_cases regenerate
                        # is a one-click fresh flow — we never want the engine
                        # to pause at await_input for the user to resolve
                        # clarifying questions. Engine default is already False
                        # but pass explicitly so it's obvious from this call site.
                        # WHY change_request_id: the JSON companion follows
                        # the cert-simulator contract format, which keys on
                        # the host's CR id at the top level so downstream
                        # simulator tooling can link cases back to the source.
                        options={
                            "archetype": "C",
                            "pause_on_questions": False,
                            "change_request_id": str(change_id),
                            **engine_scope_context,
                        },
                        registry_job_id=registry_job_id,
                        artifacts_root=_ARTIFACTS_DIR,
                        job_registry=job_registry,
                        db=db,
                    )
                    full_response = engine_result.get("markdown") or ""
                    engine_xlsx = engine_result.get("xlsx_path") or ""
                    engine_md   = engine_result.get("md_path") or ""
                    engine_docx = engine_result.get("docx_path") or ""
                    engine_json = engine_result.get("json_path") or ""
                    logger.info(
                        "WS product-kit excel-engine done: change=%s doc_type=%s response_len=%d xlsx=%s",
                        change_id, doc_type, len(full_response), engine_xlsx,
                    )
                except Exception as exc:
                    logger.exception("WS product-kit excel-engine failed")
                    job_registry.fail_job(
                        db, registry_job_id, error=str(exc),
                        final_stage="Excel engine failed",
                    )
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "detail": _engine_error_detail(exc),
                            "job_id": registry_job_id,
                        }))
                    except (WebSocketDisconnect, RuntimeError):
                        pass
                    continue

            elif doc_type in ("promo_video", "explainer_video"):
                # ── Segmented AI-video script (model- & duration-aware) ─────
                # Produces structured JSON (VideoScript) split into ≤8s
                # segments; the actual clips are generated later by
                # services/video_gen_runner.py (one clip per segment, merged).
                # A non-"start" message on an existing script is a REVISION:
                # the prior script is fed back in and refined per the request.
                from app.agents.video_script_schema import VideoScript as _VS
                _prev_vid = latest_kit_doc(db, change_id, ProductKitDocType(doc_type))
                _cur_script = None
                if _prev_vid and _prev_vid.script_json and user_msg.lower() != "start":
                    try:
                        _cur_script = _VS.model_validate(_prev_vid.script_json)
                    except Exception:
                        _cur_script = None
                _revising = _cur_script is not None

                # On revise, keep the prior script's provider/model/duration so the
                # segment structure stays stable; a fresh run uses overrides/defaults.
                if _revising:
                    _vp = (data.get("video_provider") or _cur_script.provider or _app_settings.video_provider)
                    _vm = (data.get("video_model") or _cur_script.model or _app_settings.video_model)
                    _vd = int(data.get("video_duration_sec") or _cur_script.duration_sec)
                else:
                    _vp = (data.get("video_provider") or _app_settings.video_provider)
                    _vm = (data.get("video_model") or _app_settings.video_model)
                    _vd = int(data.get("video_duration_sec") or (
                        _app_settings.promo_video_duration_sec if doc_type == "promo_video"
                        else _app_settings.explainer_video_duration_sec))
                job_registry.update_job(
                    db, registry_job_id,
                    current_stage=f"{'Refining' if _revising else 'Scripting'} {label}",
                )
                try:
                    video_script_obj = await generate_video_script(
                        doc_type,
                        target_provider=_vp, target_model=_vm, duration_sec=_vd,
                        enriched_prompt=enriched_prompt, research_report=research_report,
                        canvas_content=canvas_content, brd_content=brd_content,
                        tech_spec_content=tech_spec_content,
                        proposals_block=proposals_block,
                        clarification_answers=clarification_answers,
                        xsd_content=_latest_xsd_content(change_id, db),
                        decisions_block=_decisions_block(change_id, db),
                        aspect_ratio=_app_settings.video_aspect_ratio,
                        segment_max_sec=_app_settings.video_segment_max_sec,
                        current_script=_cur_script,
                        revision_request=(actual_msg if _revising else ""),
                    )
                    full_response = render_video_script_markdown(video_script_obj)
                    logger.info(
                        "WS product-kit video-script done: change=%s doc=%s segments=%d provider=%s model=%s",
                        change_id, doc_type, len(video_script_obj.segments), _vp, _vm,
                    )
                except Exception as exc:
                    logger.exception("WS product-kit video-script failed")
                    job_registry.fail_job(db, registry_job_id, error=str(exc),
                                          final_stage="Scripting failed")
                    await websocket.send_text(json.dumps({
                        "type": "error", "detail": "An internal error occurred", "job_id": registry_job_id,
                    }))
                    continue
                # Single-chunk delivery so ReactMarkdown shows the rendered script.
                await job_registry.ws_send_chunk(websocket, registry_job_id, full_response)

            else:
                # ── [LEGACY — NOT IN USE under default settings] ───────────
                # WHY KEPT in code: emergency rollback path. When
                # `settings.excel_engine_enabled` is true (the default),
                # cert_test_cases is handled by the engine branch above and
                # this `else:` does NOT run for cert_test_cases. It still
                # serves the OTHER doc types (product_doc, product_deck,
                # promo_video, explainer_video, faq, manifest,
                # prototype_screens) — those continue to use the legacy
                # `stream_product_kit_doc` markdown agent unchanged.
                # WHY KEPT (not deleted): if a critical regression surfaces
                # in the engine, flipping the flag returns cert_test_cases
                # to this code path with zero deploy. Once the engine has
                # been stable in production for a release cycle, this
                # legacy branch can be removed.
                logger.info("WS product-kit streaming started: change=%s doc_type=%s", change_id, doc_type)
                job_registry.update_job(
                    db, registry_job_id, current_stage=f"Generating {label}",
                )
                try:
                    async for chunk in stream_product_kit_doc(
                        doc_type, enriched_prompt, research_report, canvas_content,
                        brd_content, tech_spec_content, prior_history, actual_msg,
                        proposals_block=proposals_block,
                        clarification_answers=clarification_answers,
                        xsd_content=_latest_xsd_content(change_id, db),
                        decisions_block=_decisions_block(change_id, db),
                    ):
                        full_response += chunk
                        await job_registry.ws_send_chunk(websocket, registry_job_id, chunk)
                    logger.info(
                        "WS product-kit streaming done: change=%s doc_type=%s response_len=%d",
                        change_id, doc_type, len(full_response),
                    )
                except Exception as exc:
                    logger.exception("WS product-kit (%s) legacy streaming failed", doc_type)
                    job_registry.fail_job(db, registry_job_id, error=str(exc),
                                          final_stage="Stream failed")
                    await websocket.send_text(json.dumps({
                        "type": "error", "detail": "An internal error occurred", "job_id": registry_job_id,
                    }))
                    continue

            _save_pk_message(MessageRole.ASSISTANT, full_response)

            # Insert a new ProductKitDocument version (history, like BRD/TSD —
            # never overwrite, so a kit shipped to partners stays recoverable).
            prev = latest_kit_doc(db, change_id, ProductKitDocType(doc_type))
            new_version = (prev.version + 1) if prev else 1

            # D6 — for product_deck, render the .pptx companion and strip
            # the trailing JSON fence so the .docx renderer below sees
            # only the clean script. Failures here never break Product
            # Kit gen — the helper logs WARN and returns pptx_path=None.
            deck_pptx_path: str | None = None
            if doc_type == "product_deck":
                full_response, deck_pptx_path = _render_deck_pptx_safe(
                    full_response, change_id=change_id, version=new_version,
                )


            pk_row = ProductKitDocument(
                change_request_id=change_id,
                doc_type=ProductKitDocType(doc_type),
                content=full_response,
                version=new_version,
                negotiation_version=getattr(cr, "negotiation_version", 1) or 1,
            )
            if deck_pptx_path:
                pk_row.pptx_path = deck_pptx_path
            if video_script_obj is not None:
                pk_row.script_json = video_script_obj.model_dump()
                pk_row.video_provider = video_script_obj.provider
                pk_row.video_model = video_script_obj.model
                pk_row.video_duration_sec = video_script_obj.duration_sec
            db.add(pk_row)
            db.commit()

            # S6 backstop: advisory check that any XML in this partner doc validates
            # against the approved schemas (the real XSDs are already injected, so this
            # only fires if the model still invented something). Logged, non-blocking.
            try:
                from app.services.xsd_validation import validate_xml_blocks
                _kxsd = _latest_xsd_content(change_id, db)
                if _kxsd.strip():
                    _kf = validate_xml_blocks(full_response, [_kxsd])
                    if _kf:
                        logger.warning("WS product-kit %s: XML-validation findings: %s",
                                       doc_type, "; ".join(_kf)[:300])
            except Exception as _kv_exc:  # noqa: BLE001
                logger.warning("WS product-kit %s: xml-validation skipped (%s)", doc_type, _kv_exc)

            # Engine path (cert_test_cases) skips the legacy validator and
            # docx-assembly: the engine already validates the workbook (with
            # mechanical + LLM-semantic checks) and writes md / docx companions
            # alongside the xlsx via excel_writer.exporters.write_companions.
            # Running the legacy validate_doc / _assemble_docx_safe here would
            # produce competing artifacts and overwrite the engine's
            # result_payload (which carries the engine's xlsx path used by
            # the .xlsx download endpoint).
            is_engine_path = (
                _settings_enabled(getattr(_app_settings, "excel_engine_enabled", True), True)
                and doc_type == "cert_test_cases"
            )
            if is_engine_path:
                docx_path = engine_docx or None
                # Legacy host validator never ran; surface a minimal validation
                # shape so the WS done-payload contract is preserved.
                validation = {"issues": [], "has_errors": False, "error_count": 0, "warning_count": 0}
                if docx_path and pk_row:
                    pk_row.docx_path = docx_path
                    db.commit()
                # Engine already called mark_complete via the orchestrator.
                # We re-call complete_job here to attach the host-side fields
                # (doc_type, version) on top — complete_job replaces
                # result_payload, so we merge engine fields explicitly.
                merged_result = {
                    "doc_type":       doc_type,
                    "version":        pk_row.version if pk_row else None,
                    "markdown_chars": len(full_response),
                    "docx_path":      engine_docx or None,
                    "validation":     validation,
                    "files": {
                        "xlsx": engine_xlsx or None,
                        "md":   engine_md or None,
                        "docx": engine_docx or None,
                        "json": engine_json or None,
                    },
                }
                job_registry.complete_job(
                    db, registry_job_id,
                    result=merged_result,
                    final_stage=f"{label} ready",
                )
            else:
                validation = summarize_validation(validate_doc(full_response, doc_type="product_kit"))
                # When docgen owns assembly the .docx already exists on disk —
                # honour that path rather than re-running the legacy assembler.
                if docgen_docx_override:
                    docx_path = docgen_docx_override
                else:
                    docx_path = _assemble_docx_safe(
                        full_response, change_id=change_id, doc_type="Product Kit",
                        version=pk_row.version if pk_row else 1, subtype=doc_type,
                        cr_title=cr.title or cr.initial_prompt[:80],
                    )
                if docx_path and pk_row:
                    pk_row.docx_path = docx_path
                    db.commit()

                # R-5 — mark the durable job complete.
                job_registry.complete_job(
                    db, registry_job_id,
                    result={
                        "doc_type":       doc_type,
                        "version":        pk_row.version if pk_row else None,
                        "markdown_chars": len(full_response),
                        "docx_path":      docx_path,
                        "docgen_job_id":  docgen_job_id,
                        "validation":     validation,
                    },
                    final_stage=f"{label} ready",
                )

            done_payload = {
                "type": "done",
                "full": full_response,
                "validation": validation,
                "doc_type": doc_type,
                "docx_available": bool(docx_path),
                "job_id": registry_job_id,
            }
            if docgen_job_id:
                # Surfaced for STEP 7 frontend section-wise edit dropdown.
                done_payload["docgen_job_id"] = docgen_job_id
            # WHY try/except: long engine runs (cert_test_cases) often outlive
            # the WS connection — the user navigates away or the browser
            # times out the socket. Sending after close raises RuntimeError;
            # the job result is already persisted via job_registry, so the
            # client can pick it up on reconnect from the chunk buffer.
            try:
                await websocket.send_text(json.dumps(done_payload))
            except (WebSocketDisconnect, RuntimeError) as exc:
                logger.info(
                    "WS product-kit done after disconnect: change=%s doc_type=%s job=%s reason=%s",
                    change_id, doc_type, registry_job_id, exc,
                )

    except WebSocketDisconnect:
        logger.info("WS product-kit disconnected: change=%s doc_type=%s", change_id, doc_type)
    except Exception as e:
        logger.exception("WS product-kit error: change=%s doc_type=%s", change_id, doc_type)
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": "An internal error occurred"}))
        except Exception:
            pass
    finally:
        db.close()


# ── WebSocket: Product Kit — generate ALL docs in parallel ───────────────────

# Cap concurrency so we don't hammer the LLM provider / run out of worker tokens.
# 3 is safe for most API tiers; tune per-provider if needed.
_PRODUCT_KIT_PARALLEL_LIMIT = 3


@router.websocket("/ws/changes/{change_id}/product-kit-all")
async def ws_product_kit_all(websocket: WebSocket, change_id: str):
    """Generate multiple Product Kit documents concurrently.

    Message protocol:
      Client → Server (once):
        {"token": "<jwt>"}
        {"doc_types": ["product_note", "circular", ...]}  # optional; omit for all
                                                          # retired types are rejected

      Server → Client (streamed):
        {"type": "started",   "doc_type": "...", "index": N, "total": M}
        {"type": "chunk",     "doc_type": "...", "text":  "..."}
        {"type": "doc_done",  "doc_type": "...", "full":  "...", "validation": {...}}
        {"type": "doc_error", "doc_type": "...", "detail":"..."}
        {"type": "all_done"}
    """
    await websocket.accept()
    # Attribute every LLM call in this change-pipeline handler to the change so the Usage
    # dashboard groups it under the right flow (not 'other'). Task-local contextvar — each
    # WS connection is its own asyncio task, so this never leaks across connections.
    try:
        from app.core.observability import set_usage_context as _set_usage_ctx
        _set_usage_ctx(change_request_id=change_id)
    except Exception:
        pass
    logger.info("WS product-kit-all connected: change=%s", change_id)

    # Single auth DB session; each worker task gets its own session.
    auth_db: Session = SessionLocal()
    ws_send_lock = asyncio.Lock()

    async def send(payload: dict):
        async with ws_send_lock:
            try:
                await websocket.send_text(json.dumps(payload))
            except Exception:
                pass  # client disconnected; worker will see it on next send

    # R-5 — durable job registry for the parallel fan-out.
    from app.services import job_registry
    import uuid as _uuid

    try:
        # 1. Authenticate
        auth_raw = await websocket.receive_text()
        token = json.loads(auth_raw).get("token", "")
        user = authenticate_ws(websocket, auth_db, token)
        if not user:
            logger.warning("WS product-kit-all auth failed: change=%s", change_id)
            await send({"type": "error", "detail": "Unauthorized"})
            return

        # R-5 — surface any active product_kit jobs for this change so the
        # client can show a bundle-level resume banner. We don't try to
        # deduplicate against the doc_types the client just requested —
        # the client already knows which docs it asked for and reconciles.
        active_pk = job_registry.get_active_jobs(
            auth_db, change_request_id=change_id, module="product_kit",
        )
        if active_pk:
            await send({"type": "active_jobs", "jobs": active_pk})

        # 2. Receive doc_types selection (or default to all)
        selection_raw = await websocket.receive_text()
        try:
            selection_payload = json.loads(selection_raw)
        except Exception:
            selection_payload = {}

        # R-5 — also accept an early replay_request if the client is reconnecting
        # to an in-flight bundle. We respond and then expect a real doc_types
        # selection on the next message.
        if selection_payload.get("type") == "replay_request":
            rep_job_id = (selection_payload.get("job_id") or "").strip()
            rep_since  = int(selection_payload.get("since_seq") or 0)
            if rep_job_id:
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await send({
                    "type": "replay", "job_id": rep_job_id, "since_seq": rep_since,
                    "chunks": [{"seq": s, "text": t} for (s, t) in chunks],
                    "count": len(chunks),
                })
            # Wait for the actual selection.
            selection_raw = await websocket.receive_text()
            try:
                selection_payload = json.loads(selection_raw)
            except Exception:
                selection_payload = {}

        requested = selection_payload.get("doc_types") or list(VALID_DOC_TYPES)
        doc_types = [d for d in requested if d in VALID_DOC_TYPES]
        if not doc_types:
            await send({"type": "error", "detail": "No valid doc_types provided"})
            return
        logger.info("WS product-kit-all: change=%s doc_types=%s", change_id, doc_types)

        # R-5 — bundle id ties the per-doc jobs together so the sidebar tray
        # / future bundle-status screen can group them under one heading.
        bundle_id = _uuid.uuid4().hex
        await send({
            "type":       "bundle_started",
            "bundle_id":  bundle_id,
            "doc_types":  doc_types,
            "total":      len(doc_types),
        })

        # 3. Load shared context artefacts ONCE (every worker uses these)
        cr = auth_db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        if not cr:
            await send({"type": "error", "detail": "Change request not found"})
            return
        enriched_prompt = cr.enhanced_prompt or cr.initial_prompt

        research = (auth_db.query(ResearchOutput)
                    .filter(ResearchOutput.change_request_id == change_id)
                    .order_by(ResearchOutput.version.desc()).first())
        research_report = research.combined_report if research else "No research report available."

        canvas = (auth_db.query(ProductCanvas)
                  .filter(ProductCanvas.change_request_id == change_id)
                  .order_by(ProductCanvas.version.desc()).first())
        canvas_content = canvas.content if canvas else "No canvas available."

        brd = (auth_db.query(BRD)
               .filter(BRD.change_request_id == change_id)
               .order_by(BRD.version.desc()).first())
        brd_content = brd.content if brd else "No BRD available."

        tech_spec = (auth_db.query(TechSpec)
                     .filter(TechSpec.change_request_id == change_id)
                     .order_by(TechSpec.version.desc()).first())
        tech_spec_content = (tech_spec.content if tech_spec
                             else (_decisions_block(change_id, auth_db) or "No tech spec available."))

        # Load cached proposals + PM answers once (Sprint 3 + 5) — shared across all workers
        shared_proposals_block = ""
        shared_clarification_answers = ""
        try:
            from app.services.context_cache import get_or_build
            from app.agents.proposals_extractor import format_for_prompt
            ctx = await get_or_build(change_id, auth_db)
            if ctx and ctx.proposals:
                shared_proposals_block = format_for_prompt(ctx.proposals)
        except Exception as e:
            logger.warning("WS product-kit-all: context_cache unavailable (%s)", e)
        try:
            from app.services.clarification_loader import load_answers_block
            shared_clarification_answers = load_answers_block(change_id, auth_db)
        except Exception as e:
            logger.warning("WS product-kit-all: clarification_loader failed (%s)", e)

        total = len(doc_types)
        sem = asyncio.Semaphore(_PRODUCT_KIT_PARALLEL_LIMIT)

        doc_labels = {
            "product_doc":       "product document",
            "product_deck":      "product deck",
            "promo_video":       "promotional video script",
            "explainer_video":   "explainer video script",
            "faq":               "FAQ document",
            "cert_test_cases":   "certification test case document",
            "circular":          "NPCI technical circular",
            "manifest":          "manifest file",
            "prototype_screens": "prototype screens HTML",
            "product_note":      "product note",
        }

        async def _generate_one(idx: int, doc_type: str):
            """Generate one Product Kit doc; stream its chunks multiplexed on the WS.

            R-5 — each worker creates its own agent_jobs row sharing
            metadata.bundle_id with its siblings. Chunks are multiplexed on
            the WS (with `doc_type` field for the client-side router) AND
            mirrored to that worker's per-job Redis chunk buffer for replay.
            """
            label = doc_labels.get(doc_type, doc_type)
            actual_msg = f"Generate the {label}."
            worker_db = SessionLocal()
            full_response = ""

            # R-5 — durable job for this single doc within the bundle.
            worker_job_id = job_registry.create_job(
                worker_db,
                change_request_id=change_id,
                module="product_kit",
                subtype=doc_type,
                started_by_user_id=user.id,
                metadata={
                    "trigger":   actual_msg[:120],
                    "label":     label,
                    "bundle_id": bundle_id,
                    "parallel":  True,
                },
            )
            async with sem:
                await send({
                    "type": "started", "doc_type": doc_type,
                    "index": idx, "total": total,
                    "job_id": worker_job_id, "bundle_id": bundle_id,
                })
                job_registry.update_job(
                    worker_db, worker_job_id, current_stage=f"Generating {label}",
                )
                try:
                    logger.info("WS product-kit-all streaming started: change=%s doc_type=%s", change_id, doc_type)
                    docgen_job_id: str | None = None
                    docgen_docx_override: str | None = None
                    engine_xlsx = ""
                    engine_md = ""
                    engine_docx = ""
                    engine_json = ""
                    is_engine_path = (
                        _settings_enabled(getattr(_app_settings, "excel_engine_enabled", True), True)
                        and doc_type == "cert_test_cases"
                    )
                    docgen_eligible = doc_type in ("circular", "product_note")
                    use_docgen_path = docgen_eligible

                    if is_engine_path:
                        from pathlib import Path
                        import uuid as _engine_uuid
                        from app.excel_testcase_engine.excel_writer.exporters import to_markdown
                        from app.excel_testcase_engine.orchestrator.graph import run_workflow, _ARTIFACTS_DIR
                        from app.excel_testcase_engine.schemas.workbook_plan import WorkbookPlan

                        # WHY custom all-doc engine bridge:
                        # run_workflow_for_ws emits the single-doc WS protocol
                        # without doc_type. Generate All multiplexes multiple
                        # docs on one socket, so every event must carry doc_type.
                        queue = asyncio.Queue(maxsize=512)
                        engine_job_id = str(_engine_uuid.uuid4())

                        def _on_engine_progress(progress) -> None:
                            try:
                                queue.put_nowait(progress)
                            except asyncio.QueueFull:
                                logger.warning("WS product-kit-all engine progress queue full: job=%s", worker_job_id)

                        _STAGE_BANDS = {
                            "pending":                 (0, 2),
                            "enhancing":               (2, 10),
                            "needs_input":             (10, 10),
                            "planning":                (10, 20),
                            "writing":                 (20, 80),
                            "rendering":               (80, 85),
                            "validating":              (85, 95),
                            "repairing":               (90, 95),
                            "completed":               (100, 100),
                            "completed_with_warnings": (100, 100),
                            "failed":                  (100, 100),
                        }

                        def _progress_pct(stage_value: str, current: int, total: int) -> int:
                            lo, hi = _STAGE_BANDS.get(stage_value, (0, 100))
                            if total > 0 and current >= 0:
                                frac = max(0.0, min(1.0, current / total))
                                return int(lo + (hi - lo) * frac)
                            return lo

                        async def _drain_engine_progress() -> None:
                            while True:
                                event = await queue.get()
                                if event is None:
                                    return
                                stage_value = event.status.value if hasattr(event.status, "value") else str(event.status)
                                pct = _progress_pct(stage_value, int(event.current or 0), int(event.total or 0))
                                try:
                                    job_registry.update_job(
                                        worker_db, worker_job_id,
                                        current_stage=event.message or stage_value,
                                        progress_pct=pct,
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning(
                                        "WS product-kit-all engine progress update failed: job=%s error=%s",
                                        worker_job_id, exc,
                                    )
                                await send({
                                    "type": "progress",
                                    "doc_type": doc_type,
                                    "job_id": worker_job_id,
                                    "current_stage": event.message or stage_value,
                                    "stage": stage_value,
                                    "message": event.message,
                                    "current": event.current,
                                    "total": event.total,
                                    "progress_pct": pct,
                                    "open_questions": event.open_questions,
                                })

                        drain_task = asyncio.create_task(_drain_engine_progress())
                        try:
                            # BRD/TSD-only: same shape as the single-doc branch.
                            brief = (
                                f"{actual_msg}\n\n"
                                f"# BRD\n{brd_content[:6000]}\n\n"
                                f"# Tech Spec\n{tech_spec_content[:6000]}"
                            )
                            engine_scope_context = await _load_engine_scope_context(change_id, worker_db)
                            rendered_path = await run_workflow(
                                brief=brief,
                                # See cert_test_cases branch above — same
                                # rationale for threading change_request_id
                                # so the JSON companion matches the
                                # cert-simulator contract format.
                                options={
                                    "archetype": "C",
                                    "pause_on_questions": False,
                                    "change_request_id": str(change_id),
                                    **engine_scope_context,
                                },
                                on_progress=_on_engine_progress,
                                job_id=engine_job_id,
                                registry_job_id=worker_job_id,
                            )
                        finally:
                            await queue.put(None)
                            try:
                                await asyncio.wait_for(drain_task, timeout=2.0)
                            except asyncio.TimeoutError:
                                drain_task.cancel()

                        plan = None
                        plan_path = _ARTIFACTS_DIR / engine_job_id / "03-rendered_plan.json"
                        if plan_path.exists():
                            try:
                                plan = WorkbookPlan.model_validate(json.loads(plan_path.read_text(encoding="utf-8")))
                            except Exception as exc:
                                logger.warning("WS product-kit-all engine plan reload failed: %s", exc)
                        full_response = (
                            to_markdown(plan)
                            if plan
                            else f"# Workbook generated\n\nFile: `{Path(rendered_path).name}`"
                        )
                        rp = Path(rendered_path)
                        engine_xlsx = str(rp)
                        engine_md = str(rp.with_suffix(".md")) if rp.with_suffix(".md").exists() else ""
                        engine_docx = str(rp.with_suffix(".docx")) if rp.with_suffix(".docx").exists() else ""
                        engine_json = str(rp.with_suffix(".json")) if rp.with_suffix(".json").exists() else ""
                        job_registry.append_chunk(worker_job_id, full_response)
                        await send({
                            "type": "chunk", "doc_type": doc_type, "text": full_response,
                            "job_id": worker_job_id,
                        })

                    elif use_docgen_path:
                        from app.services.docgen_runner import (
                            build_initial_state, emit_stage_progress_text,
                            run_pipeline_in_thread, sections_to_markdown,
                            set_latest_job,
                        )

                        _docgen_dispatch = {
                            "circular":     ("Circular",     "Circular/circular"),
                            "product_note": ("Product Note", "Product Note/product_note"),
                        }
                        docgen_doc_type, job_key = _docgen_dispatch[doc_type]

                        async def _emit_docgen_progress(stage: str) -> None:
                            try:
                                stage_labels = {
                                    "retrieving":          "Retrieving knowledge base context",
                                    "planning":            "Planning document structure",
                                    "generating_diagrams": "Generating UML diagrams",
                                    "writing":             "Writing section content",
                                    "reviewing":           "Validating sections",
                                    "assembling":          "Building .docx",
                                }
                                stage_label = stage_labels.get(stage, stage)
                                progress_text = emit_stage_progress_text(stage)
                                job_registry.update_job(worker_db, worker_job_id, current_stage=stage_label)
                                job_registry.append_chunk(worker_job_id, progress_text)
                                await send({
                                    "type": "progress",
                                    "doc_type": doc_type,
                                    "job_id": worker_job_id,
                                    "current_stage": stage_label,
                                })
                                await send({
                                    "type": "chunk", "doc_type": doc_type, "text": progress_text,
                                    "job_id": worker_job_id,
                                })
                            except Exception:
                                pass

                        additional_ctx_parts = []
                        if brd_content and brd_content != "No BRD available.":
                            additional_ctx_parts.append("--- Approved BRD ---\n" + brd_content)
                        if tech_spec_content and tech_spec_content != "No tech spec available.":
                            additional_ctx_parts.append("--- Technical Specification ---\n" + tech_spec_content)
                        if shared_clarification_answers:
                            additional_ctx_parts.append("--- PM Clarifications ---\n" + shared_clarification_answers)
                        additional_context = "\n\n".join(additional_ctx_parts)

                        state = build_initial_state(
                            doc_type=docgen_doc_type,
                            change_id=change_id,
                            prompt=enriched_prompt,
                            document_title=(
                                f"{docgen_doc_type}: {cr.title}" if cr.title
                                else f"{docgen_doc_type}: {cr.initial_prompt[:60]}"
                            ),
                            audience=("Member Banks, PSPs, TPAPs" if doc_type == "circular"
                                      else "Product Managers, Tech Leads, Operations"),
                            desired_outcome=f"Approved {docgen_doc_type}",
                            research_report=research_report,
                            canvas_content=canvas_content,
                            additional_context=additional_context,
                            include_diagrams=(doc_type != "circular"),
                            use_rag=True,
                        )

                        def _check_cancel() -> bool:
                            try:
                                return job_registry.is_cancelled(worker_db, worker_job_id)
                            except Exception:
                                return False

                        final_state = await run_pipeline_in_thread(
                            state, on_stage=_emit_docgen_progress, cancel_check=_check_cancel,
                        )
                        if final_state.get("status") == "cancelled":
                            raise RuntimeError("Cancelled by user")
                        if final_state.get("status") == "failed":
                            raise RuntimeError(f"Docgen pipeline failed: {final_state.get('error')}")

                        plan = final_state.get("document_plan") or {}
                        sections = final_state.get("generated_sections") or []
                        full_response = sections_to_markdown(plan, sections)
                        docgen_job_id = final_state.get("job_id")
                        docgen_docx_override = final_state.get("output_path")
                        if docgen_job_id:
                            set_latest_job(change_id, job_key, docgen_job_id)
                        job_registry.append_chunk(worker_job_id, full_response)
                        await send({
                            "type": "chunk", "doc_type": doc_type, "text": full_response,
                            "job_id": worker_job_id,
                        })

                    else:
                        if doc_type == "product_note":
                            raise RuntimeError("Product Note is docgen-only and must not reach the legacy path")
                        # Each parallel worker starts from an empty history — they're independent.
                        async for chunk in stream_product_kit_doc(
                            doc_type, enriched_prompt, research_report, canvas_content,
                            brd_content, tech_spec_content,
                            conversation_history=[], new_user_message=actual_msg,
                            proposals_block=shared_proposals_block,
                            clarification_answers=shared_clarification_answers,
                            xsd_content=_latest_xsd_content(change_id, worker_db),
                            decisions_block=_decisions_block(change_id, worker_db),
                        ):
                            full_response += chunk
                            # Mirror chunks to Redis and multiplex them with doc_type.
                            job_registry.append_chunk(worker_job_id, chunk)
                            await send({
                                "type": "chunk", "doc_type": doc_type, "text": chunk,
                                "job_id": worker_job_id,
                            })

                    logger.info(
                        "WS product-kit-all streaming done: change=%s doc_type=%s response_len=%d",
                        change_id, doc_type, len(full_response),
                    )

                    # Persist conversation + document
                    worker_db.add(Conversation(
                        change_request_id=change_id,
                        module=ConversationModule.PRODUCT_KIT,
                        role=MessageRole.USER,
                        content=actual_msg,
                        metadata_={"doc_type": doc_type, "parallel": True},
                        created_by=user.id,
                    ))
                    worker_db.add(Conversation(
                        change_request_id=change_id,
                        module=ConversationModule.PRODUCT_KIT,
                        role=MessageRole.ASSISTANT,
                        content=full_response,
                        metadata_={"doc_type": doc_type, "parallel": True},
                    ))

                    # Insert a new version (history, never overwrite) — see the
                    # legacy single-doc path above.
                    prev = latest_kit_doc(worker_db, change_id, ProductKitDocType(doc_type))
                    new_version = (prev.version + 1) if prev else 1

                    # D6 — see notes on the legacy single-doc path above.
                    deck_pptx_path: str | None = None
                    if doc_type == "product_deck":
                        full_response, deck_pptx_path = _render_deck_pptx_safe(
                            full_response, change_id=change_id, version=new_version,
                        )


                    pk_row_final = ProductKitDocument(
                        change_request_id=change_id,
                        doc_type=ProductKitDocType(doc_type),
                        content=full_response,
                        version=new_version,
                        negotiation_version=getattr(cr, "negotiation_version", 1) or 1,
                    )
                    if deck_pptx_path:
                        pk_row_final.pptx_path = deck_pptx_path
                    worker_db.add(pk_row_final)
                    worker_db.commit()
                    if is_engine_path:
                        docx_path = engine_docx or None
                        validation = {"issues": [], "has_errors": False, "error_count": 0, "warning_count": 0}
                        result_payload = {
                            "doc_type":       doc_type,
                            "version":        pk_row_final.version if pk_row_final else None,
                            "markdown_chars": len(full_response),
                            "docx_path":      engine_docx or None,
                            "validation":     validation,
                            "bundle_id":      bundle_id,
                            "files": {
                                "xlsx": engine_xlsx or None,
                                "md":   engine_md or None,
                                "docx": engine_docx or None,
                                "json": engine_json or None,
                            },
                        }
                    else:
                        validation = summarize_validation(validate_doc(full_response, doc_type="product_kit"))
                        # Assemble DOCX (best-effort; non-blocking). When docgen
                        # already produced one, keep that richer artifact.
                        docx_path = (
                            str(docgen_docx_override) if docgen_docx_override else
                            _assemble_docx_safe(
                                full_response, change_id=change_id, doc_type="Product Kit",
                                version=pk_row_final.version if pk_row_final else 1,
                                subtype=doc_type, cr_title=cr.title or cr.initial_prompt[:80],
                            )
                        )
                        result_payload = {
                            "doc_type":       doc_type,
                            "version":        pk_row_final.version if pk_row_final else None,
                            "markdown_chars": len(full_response),
                            "docx_path":      docx_path,
                            "docgen_job_id":  docgen_job_id,
                            "validation":     validation,
                            "bundle_id":      bundle_id,
                        }

                    if docx_path and pk_row_final:
                        pk_row_final.docx_path = docx_path
                        worker_db.commit()

                    job_registry.complete_job(
                        worker_db, worker_job_id,
                        result=result_payload,
                        final_stage=f"{label} ready",
                    )
                    await send({
                        "type": "doc_done", "doc_type": doc_type,
                        "full": full_response, "validation": validation,
                        "docx_available": bool(docx_path),
                        "xlsx_available": bool(engine_xlsx),
                        "job_id": worker_job_id,
                    })
                except Exception as e:
                    logger.exception(
                        "WS product-kit-all error: change=%s doc_type=%s", change_id, doc_type,
                    )
                    job_registry.fail_job(worker_db, worker_job_id, error=str(e))
                    await send({
                        "type": "doc_error", "doc_type": doc_type,
                        "detail": _engine_error_detail(e),
                        "job_id": worker_job_id,
                    })
                finally:
                    worker_db.close()

        # 4. Run all in parallel (semaphore caps concurrency)
        await asyncio.gather(*[
            _generate_one(i, dt) for i, dt in enumerate(doc_types)
        ])

        await send({"type": "all_done", "bundle_id": bundle_id})
        logger.info("WS product-kit-all: change=%s all done bundle=%s", change_id, bundle_id)

    except WebSocketDisconnect:
        logger.info("WS product-kit-all disconnected: change=%s", change_id)
    except Exception as e:
        logger.exception("WS product-kit-all fatal error: change=%s", change_id)
        try:
            await websocket.send_text(json.dumps({"type": "error", "detail": "An internal error occurred"}))
        except Exception:
            pass
    finally:
        auth_db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Docgen merge (Session 20+) — REST endpoints adopted from teammate's
# `doc-generation-wiring` branch.
#
#  • GET  /changes/{id}/docgen/sections      — list section headings from the
#                                              latest docgen job for a doc_type
#  • POST /changes/{id}/docgen/edit          — section-wise edit (Phase G UX)
#  • POST /changes/{id}/brd/dev-auto-approve — DEV-only fast-path approval
#                                              (gated by APP_ENV != production)
#
# The two `/docgen/*` endpoints depend on `app.services.docgen_runner` and
# `app.docgen.plan_store` (landed in STEP 2). They are read-only against the
# in-memory latest-job map + on-disk artifacts, so calling them today (before
# any docgen pipeline run has populated those artifacts) returns empty lists
# / 404 — that's the intended pre-STEP-5 behaviour.
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/changes/{change_id}/brd/dev-auto-approve")
def dev_auto_approve_brd(change_id: str, db: DbDep, _: AdminUser):
    """DEV-ONLY — fast-path approve every pending BRD reviewer for this change
    and advance change.status → tech_spec. Useful for testing the downstream
    flow without manually clicking through 4 reviewer dashboards.

    Refuses to run when APP_ENV=production. Admin-only.
    """
    if (_app_settings.app_env or "").lower() == "production":
        raise HTTPException(status_code=403, detail="Disabled in production")

    # Same gates as submit_brd: the dev fast-path must not approve a BRD whose uploaded-doc
    # reconciliation conflicts are still open, nor one whose accepted change overturns a
    # ratified decision without acknowledgement (§8.1).
    from app.agents.upload_reconciler import has_unresolved_reconciliation, overturns_needs_ack
    if has_unresolved_reconciliation(db, change_id, "brd"):
        raise HTTPException(status_code=409,
                            detail="Resolve the uploaded-BRD reconciliation conflicts before approving.")
    if overturns_needs_ack(db, change_id, "brd"):
        raise HTTPException(status_code=409,
                            detail="One of your accepted changes overturns a ratified plan decision — "
                                   "acknowledge it in the code-check panel before approving.")

    brd = (
        db.query(BRD)
        .filter(BRD.change_request_id == change_id)
        .order_by(BRD.version.desc())
        .first()
    )
    if not brd:
        raise HTTPException(status_code=404, detail="No BRD for this change")

    from datetime import datetime, timezone

    pending = (
        db.query(Approval)
        .filter(
            Approval.artifact_type == ApprovalArtifactType.BRD,
            Approval.artifact_id == brd.id,
            Approval.status == ApprovalStatus.PENDING,
        )
        .all()
    )
    for a in pending:
        a.status = ApprovalStatus.APPROVED
        a.comments = (a.comments or "") + " [dev auto-approve]"
        a.responded_at = datetime.now(timezone.utc)

    brd.status = BRDStatus.APPROVED
    from app.agents.upload_reconciler import apply_reconciliation_on_brd_approval
    apply_reconciliation_on_brd_approval(db, change_id)
    cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    auto_advance_blocked_by_gate: dict | None = None
    if cr and cr.status == ChangeStatus.BRD:
        # Phase 7 — even the dev auto-approve shortcut must honour the eval
        # gate, otherwise the dev path silently sidesteps Phase 7 protections.
        _post_brd = _post_brd_status(getattr(cr, "workflow_version", 2))
        allowed, gate_detail = _eval_gate_allows_transition(
            db=db,
            change_id=cr.id,
            current_status=ChangeStatus.BRD,
            next_status=_post_brd,
        )
        if allowed:
            cr.status = _post_brd
        else:
            auto_advance_blocked_by_gate = gate_detail
            logger.warning(
                "BRD dev-auto-approve advance blocked by eval gate: change=%s",
                change_id,
            )
    db.commit()
    logger.info("DEV auto-approve: change=%s approved=%d", change_id, len(pending))
    return {
        "ok":             True,
        "approved_count": len(pending),
        "brd_status":     "approved",
        "change_status":  cr.status.value if cr else None,
        "auto_advance_blocked_by_gate": auto_advance_blocked_by_gate,
    }


class _DocGenEditRequest(BaseModel):
    doc_type: str             # 'BRD' | 'TSD' | 'Circular' | 'Product Note'
    section_heading: str      # exact heading to edit (case-insensitive match)
    edit_instruction: str     # what the user wants changed in that section


# Map UI-facing labels → docgen_runner.get_latest_job() key. Mirrors the
# convention teammate's docgen pipeline uses for per-(change, doc_type) jobs.
_DOCGEN_KEYS = {
    "BRD":          "BRD",
    "TSD":          "TSD",
    "Circular":     "Circular/circular",
    "Product Note": "Product Note/product_note",
}


@router.get("/changes/{change_id}/docgen/sections")
def docgen_sections(change_id: str, doc_type: str, _: CurrentUser):
    """Return the list of section headings from the latest docgen job for this
    change + doc_type. Used to populate the 'Edit which section?' dropdown
    on the BRD / TechSpec / ProductKit pages.

    Returns `{job_id: None, sections: []}` when no docgen run has happened
    yet for this (change, doc_type) — UI treats that as "section-wise edit
    not available, fall through to full-doc revise via WS".
    """
    from app.services.docgen_runner import get_latest_job
    from app.docgen.plan_store import artifact_dir

    key = _DOCGEN_KEYS.get(doc_type, doc_type)
    job_id = get_latest_job(change_id, key)
    if not job_id:
        return {"job_id": None, "sections": []}

    plan_path = artifact_dir(job_id) / "document_plan.json"
    if not plan_path.exists():
        return {"job_id": job_id, "sections": []}

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:
        return {"job_id": job_id, "sections": []}

    headings = []
    for sec in plan.get("sections", []) or []:
        h = (sec.get("heading") or "").strip()
        if h and sec.get("render_style") != "cover":
            headings.append(h)
    return {"job_id": job_id, "sections": headings}


@router.post("/changes/{change_id}/docgen/edit")
async def docgen_edit_section(
    change_id: str,
    payload: _DocGenEditRequest,
    db: DbDep,
    user: CurrentUser,
):
    """Section-wise edit (Phase G). Calls docgen's `edit_document_section`,
    persists the new markdown rendition + .docx path on the appropriate
    DB row (BRD / TechSpec / ProductKitDocument).

    R-8 — wrapped with job_registry.tracked_step so the section-regeneration
    (~15-60 s) shows up in the sidebar tray and on the BRD/TSD page banner.
    The two job_ids are intentionally distinct:
      - `job_id` (existing): the docgen source artefact directory id —
        used by the section-list dropdown to find the right document_plan.json
      - `registry_job_id` (new R-8 field): the operation tracking id —
        what JobsContext records for sidebar tray + resume banner
    """
    from app.services.docgen_runner import (
        edit_section_in_thread, sections_to_markdown, get_latest_job,
    )
    from app.docgen.plan_store import artifact_dir
    from app.services import job_registry

    key = _DOCGEN_KEYS.get(payload.doc_type, payload.doc_type)
    job_id = get_latest_job(change_id, key)
    if not job_id:
        raise HTTPException(
            status_code=404,
            detail="No prior generation to edit. Generate the document first.",
        )

    if not payload.section_heading.strip() or not payload.edit_instruction.strip():
        raise HTTPException(
            status_code=400,
            detail="section_heading and edit_instruction are required",
        )

    # R-8 — track section-edit as a durable job. The subtype encodes the
    # parent doc_type so the sidebar tray label disambiguates ("Section
    # edit · BRD" vs "Section edit · TSD").
    section_short = payload.section_heading.strip()[:60]
    edit_short    = payload.edit_instruction.strip()[:120]
    doc_type_slug = (payload.doc_type or "").lower().replace(" ", "_") or "unknown"

    try:
        with job_registry.tracked_step(
            db,
            change_request_id=change_id,
            module="docgen_edit",
            subtype=doc_type_slug,
            user_id=user.id,
            initial_stage=f"Editing section \"{section_short}\"",
            metadata={
                "doc_type":         payload.doc_type,
                "section_heading":  section_short,
                "edit_instruction": edit_short,
                "source_job_id":    job_id,
            },
        ) as registry_job_id:
            try:
                new_docx_path = await edit_section_in_thread(
                    job_id, payload.section_heading.strip(), payload.edit_instruction.strip(),
                )
            except ValueError as e:   # section heading not found — caller error, not pipeline failure
                # Re-raise as HTTPException AFTER the tracked_step records the failure
                raise

            plan_path = artifact_dir(job_id) / "document_plan.json"
            sections_path = artifact_dir(job_id) / "generated_sections.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            sections = json.loads(sections_path.read_text(encoding="utf-8"))
            full_markdown = sections_to_markdown(plan, sections)

            # Persist on the corresponding DB row.
            if payload.doc_type == "BRD":
                row = (db.query(BRD).filter(BRD.change_request_id == change_id)
                       .order_by(BRD.version.desc()).first())
                if row:
                    row.content = full_markdown
                    if new_docx_path:
                        row.docx_path = new_docx_path
                    row.version += 1
                    db.commit()
            elif payload.doc_type == "TSD":
                row = (db.query(TechSpec).filter(TechSpec.change_request_id == change_id)
                       .order_by(TechSpec.version.desc()).first())
                if row:
                    row.content = full_markdown
                    if new_docx_path:
                        row.docx_path = new_docx_path
                    row.version += 1
                    db.commit()
            elif payload.doc_type in ("Circular", "Product Note"):
                sub = "circular" if payload.doc_type == "Circular" else "product_note"
                row = latest_kit_doc(db, change_id, ProductKitDocType(sub))
                if row:
                    row.content = full_markdown
                    if new_docx_path:
                        row.docx_path = new_docx_path
                    row.version += 1
                    db.commit()
    except ValueError as e:
        # Bad-request from the section-not-found path. tracked_step already
        # marked the job failed via __exit__. Translate to 400.
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        # Pass-through for HTTP responses raised inside the with-block.
        raise
    except Exception as e:
        logger.exception(
            "docgen_edit_section failed: change=%s doc_type=%s",
            change_id, payload.doc_type,
        )
        # SCR #6: broad handler — `e` here can be a SQLAlchemy error carrying
        # table/column names and the SQL statement. Operators get the full
        # traceback from logger.exception above.
        raise HTTPException(status_code=500,
                            detail=f"Section edit failed: {client_safe_detail(e)}")

    return {
        "ok":              True,
        "job_id":          job_id,            # docgen source artefact dir (unchanged contract)
        "registry_job_id": registry_job_id,   # R-8 — for JobsContext tracking
        "section_heading": payload.section_heading,
        "docx_path":       new_docx_path,
        "full":            full_markdown,
    }
