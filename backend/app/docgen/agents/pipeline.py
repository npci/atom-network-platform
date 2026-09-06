# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""LangGraph StateGraph pipeline — 5 nodes + error handler."""
from __future__ import annotations
from app.core.prompts import render_prompt
from app.core.prompts import load_prompt
from app.core.domain.registry import prompt_block

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

import requests
from langchain_core.messages import HumanMessage, SystemMessage

# Truly lazy import — only loaded when LLM_PROVIDER=ollama and actually used
ChatOllama = None  # type: ignore[assignment,misc]

def _get_chat_ollama():
    global ChatOllama
    if ChatOllama is None:
        try:
            from langchain_ollama import ChatOllama as _ChatOllama
            ChatOllama = _ChatOllama
        except ImportError:
            pass
    return ChatOllama
from langgraph.graph import StateGraph, END

from app.docgen.config import settings
from app.docgen.content_fallbacks import fallback_table_data
from app.docgen.document_validator import repair_sections_for_validation, validate_generated_document
from app.docgen.document_guides import build_blueprint_plan, get_document_blueprint
from app.docgen.models import DocumentPlan, GeneratedContent
from app.docgen.plan_store import save_json_artifact
from app.docgen.rag_bridge import retrieve_multi_query
from app.docgen.tools.diagram_generator import generate_diagram
from app.docgen.tools.docx_builder import assemble_document

logger = logging.getLogger(__name__)


class _CompatLLMResponse:
    def __init__(self, content: str):
        self.content = content


class _OpenAICompatChat:
    """Minimal OpenAI-compatible chat client used when langchain_openai is unavailable."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        temperature: float,
        response_format: dict | None = None,
        timeout: int = 180,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.response_format = response_format
        self.timeout = timeout

    def invoke(self, messages: list) -> _CompatLLMResponse:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system" if isinstance(message, SystemMessage) else "user",
                    "content": message.content,
                }
                for message in messages
            ],
            "temperature": self.temperature,
        }
        if self.response_format:
            payload["response_format"] = self.response_format

        response = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return _CompatLLMResponse(content=content)


def _provider() -> str:
    return settings.normalized_llm_provider

# ---------------------------------------------------------------------------
# LLM factories
# ---------------------------------------------------------------------------
# The platform integration: route every LLM call through `app.docgen.llm_bridge`,
# which proxies to the platform's `app.core.llm.call_llm` (Anthropic Claude by default
# with retry handling). The original Ollama / OpenAI-compatible code paths
# above are intentionally bypassed — the bridge gives the docgen pipeline the same
# LangChain `.invoke(messages)` contract it expects.

from app.docgen.llm_bridge import (
    make_llm_json as _bridge_make_llm_json,
    make_llm_content as _bridge_make_llm_content,
    make_llm as _bridge_make_llm,
)


def _make_llm_json():
    """JSON-mode LLM (planning + structured section content)."""
    return _bridge_make_llm_json()


def _make_llm_content():
    """Free-form LLM (prose, PlantUML source)."""
    return _bridge_make_llm_content()


def _make_llm():
    """Generic free-form LLM."""
    return _bridge_make_llm()


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json(text: str) -> Any:
    """Stage 1 — strict parse. Raises ValueError on failure."""
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Cannot parse JSON from: {cleaned[:200]}")


def _parse_json_lenient(text: str) -> Any:
    """Stage 3 — lenient parse: ignore unknown fields, partial content."""
    cleaned = _strip_fences(text)
    # Find the outermost JSON object
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("No JSON object found in output.")
    # Walk to find balanced closing brace
    depth = 0
    end = -1
    for i, ch in enumerate(cleaned[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    fragment = cleaned[start:end] if end != -1 else cleaned[start:]
    return json.loads(fragment)


def _llm_self_correct(llm: ChatOllama, original_messages: list, error_msg: str, raw_output: str) -> str:
    """Stage 2 — ask the LLM to fix its own invalid JSON."""
    correction_prompt = (
        f"Your previous response was not valid JSON. Error: {error_msg}\n"
        f"Your output started with: {raw_output[:400]}\n\n"
        "Return ONLY valid JSON. No markdown fences, no explanation, no text before or after the JSON. "
        "Start directly with { and end with }."
    )
    messages = list(original_messages) + [HumanMessage(content=correction_prompt)]
    response = llm.invoke(messages)
    return response.content if hasattr(response, "content") else str(response)


def _sanitize_json_strings(text: str) -> str:
    """
    Best-effort repair for common LLM JSON breakage:
    - Unescaped XML angle-brackets inside quoted strings  (<tag> → &lt;tag&gt; is wrong;
      the correct fix is to escape the quote that actually broke the string, or to
      replace literal newlines inside strings with \\n).
    - Literal newlines inside JSON string values  → replace with \\n
    - Trailing commas before ] or }
    """
    # Replace literal newlines that are inside JSON string values with \n escape
    # Strategy: walk char by char tracking whether we're inside a string.
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == "\\":
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string and ch == "\n":
            result.append("\\n")
            continue
        if in_string and ch == "\r":
            result.append("\\r")
            continue
        result.append(ch)
    cleaned = "".join(result)
    # Remove trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    return cleaned


def _parse_json_with_recovery(
    llm: ChatOllama,
    raw: str,
    original_messages: list,
    fallback: Any,
    context: str = "",
) -> Any:
    """4-stage JSON recovery.

    Stage 1: Primary strict parse.
    Stage 1b: Sanitise the raw text (escape bare newlines, strip trailing commas)
              and re-try strict parse.
    Stage 2: LLM self-correction retry with error message feedback.
    Stage 3: Lenient extraction of first JSON object from sanitised output.
    Fallback: Return provided default and log a warning.
    """
    ctx = f" ({context})" if context else ""

    # Stage 1 — strict parse of raw output
    stage1_err = ""
    try:
        return _parse_json(raw)
    except Exception as e1:
        stage1_err = str(e1)
        logger.warning("[JSON recovery] Stage 1 failed%s: %s", ctx, e1)

    # Stage 1b — sanitise then strict parse  (handles literal newlines / trailing commas)
    sanitised = _sanitize_json_strings(raw)
    try:
        result = _parse_json(sanitised)
        logger.info("[JSON recovery] Stage 1b (sanitised) succeeded%s", ctx)
        return result
    except Exception as e1b:
        logger.warning("[JSON recovery] Stage 1b failed%s: %s", ctx, e1b)

    # Stage 2 — LLM self-correction
    # NOTE: stage1_err is a plain str (not the exception object) so Python's
    # "del exception variable after except block" scoping rule doesn't affect it.
    try:
        corrected = _llm_self_correct(llm, original_messages, stage1_err, raw)
        # Try strict then sanitised on the corrected output
        for attempt in (corrected, _sanitize_json_strings(corrected)):
            try:
                result = _parse_json(attempt)
                logger.info("[JSON recovery] Stage 2 self-correction succeeded%s", ctx)
                return result
            except Exception:
                pass
    except Exception as e2:
        logger.warning("[JSON recovery] Stage 2 failed%s: %s", ctx, e2)

    # Stage 3 — lenient extraction from sanitised text
    for source in (sanitised, raw):
        try:
            result = _parse_json_lenient(source)
            logger.info("[JSON recovery] Stage 3 lenient parse succeeded%s", ctx)
            return result
        except Exception as e3:
            logger.warning("[JSON recovery] Stage 3 failed%s: %s", ctx, e3)

    logger.error("[JSON recovery] All stages failed%s — using fallback.", ctx)
    return fallback


def _safe_parse_json(text: str, fallback: Any) -> Any:
    """Legacy 1-stage helper used by diagram generation (no LLM ref available)."""
    try:
        return _parse_json(text)
    except Exception as e:
        logger.warning("JSON parse failed, using fallback: %s", e)
        return fallback


# ---------------------------------------------------------------------------
# Fallback defaults
# ---------------------------------------------------------------------------

DEFAULT_PLAN = {
    "title": "Document",
    "subtitle": "",
    "doc_type": "BRD",
    "sections": [
        {
            "heading": "1. Introduction",
            "level": 1,
            "content_instructions": "Provide an overview of the document purpose.",
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "heading": "2. Requirements",
            "level": 1,
            "content_instructions": "List and describe the main requirements.",
            "include_table": True,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
        {
            "heading": "3. Conclusion",
            "level": 1,
            "content_instructions": "Summarize findings and next steps.",
            "include_table": False,
            "include_diagram": False,
            "diagram_type": "flowchart",
            "diagram_description": "",
        },
    ],
}

# Gap 14 FIX: per-doc-type domain-aware fallback plans used when LLM planning
# fails. Built at CALL time from the active pack (participants, prompt blocks)
# so no ecosystem's vocabulary is baked into another domain's fallback output.
def _build_default_plans() -> dict[str, dict]:
    from app.core.domain.contract import participants_of
    from app.core.domain.registry import get_active_pack, prompt_block

    dn = prompt_block("domain_name", "")
    dn_ = f"{dn} " if dn else ""  # "UPI " or "" — keeps sentences readable either way
    authority = prompt_block("authority", "the issuing authority")
    try:
        participant_labels = [p.label for p in participants_of(get_active_pack())]
    except Exception:  # noqa: BLE001 — a fallback plan must never fail to build
        participant_labels = []
    participants_line = (
        ", ".join(participant_labels)
        if participant_labels
        else "every participant role defined for this domain"
    )
    letterhead = prompt_block(
        "docgen_circular_letterhead_note",
        "Authority letterhead with the circular reference number in the authority's "
        "standard format, and issue date.",
    )
    addressees = prompt_block(
        "docgen_circular_addressees",
        "Complete recipient list covering every participant category in the ecosystem.",
    )
    pricing = prompt_block(
        "docgen_fallback_pricing_instructions",
        "Describe the pricing or cost-recovery structure for each participant "
        "category, including any fees set by the operating authority.",
    )
    return {
        "BRD": {
            "title": "Business Requirements Document",
            "subtitle": f"{dn_}Feature Specification",
            "doc_type": "BRD",
            "sections": [
                {"heading": "1. Executive Summary", "level": 1, "content_instructions": f"Summarise the business need, target users, and expected impact of this {dn_}feature.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "2. Problem Statement & Objectives", "level": 1, "content_instructions": f"Describe the gap in the current {dn_}ecosystem that this feature addresses. State measurable objectives.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "3. Scope", "level": 1, "content_instructions": "Define what is in scope (participant types, transaction categories, participant integrations) and what is explicitly out of scope.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "4. Functional Requirements", "level": 1, "content_instructions": "List all functional requirements as numbered statements. Each requirement must specify actor, action, and expected outcome. Minimum 10 requirements.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "5. Non-Functional Requirements", "level": 1, "content_instructions": "Cover performance (TPS targets), availability (99.99% SLA), security (MFA, TLS 1.3), scalability, and audit trail requirements per the applicable regulator directive.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "6. User Journey", "level": 1, "content_instructions": "Describe the end-to-end user journey from initiation through authentication, routing, settlement, and notification. Cover happy path and error paths.", "include_table": False, "include_diagram": True, "diagram_type": "sequence", "diagram_description": f"{dn_}transaction lifecycle sequence diagram"},
                {"heading": "7. Ecosystem Participants", "level": 1, "content_instructions": f"Identify all participants: {participants_line}. Describe each participant's role and integration obligations.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "8. Compliance & Regulatory Requirements", "level": 1, "content_instructions": "Map each requirement to the applicable authority circulars and regulator directives named in the supplied context. Cite only directives that appear there.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "9. Success Metrics & KPIs", "level": 1, "content_instructions": "Define measurable KPIs: transaction success rate, P99 latency, adoption targets at 30/60/90 days, and fraud rate ceiling.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "10. Risks & Mitigations", "level": 1, "content_instructions": "Identify top 5 risks (technical, fraud, adoption, regulatory, operational) with likelihood, impact, and mitigation strategy for each.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
            ],
        },
        "TSD": {
            "title": "Technical Specification Document",
            "subtitle": f"{dn_}Feature Engineering Specification",
            "doc_type": "TSD",
            "sections": [
                {"heading": "1. System Architecture Overview", "level": 1, "content_instructions": f"Describe the high-level system architecture: services, databases, message queues, and external integrations. Cover connectivity to the {authority} switch.", "include_table": False, "include_diagram": True, "diagram_type": "flowchart", "diagram_description": "System component architecture diagram"},
                {"heading": "2. API Specifications", "level": 1, "content_instructions": "Define all APIs in the domain's wire style: endpoint URL or message name, request schema, response schema, error codes. Include authentication headers.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "3. Data Models", "level": 1, "content_instructions": f"Define all data entities: field names, data types, constraints, and relationships. Include the {dn_}transaction record schema.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "4. Transaction Flow & State Machine", "level": 1, "content_instructions": "Document the state machine for the transaction lifecycle: PENDING → PROCESSING → SUCCESS/FAILED/REVERSED. Include timeout handling.", "include_table": False, "include_diagram": True, "diagram_type": "sequence", "diagram_description": "Transaction state machine sequence diagram"},
                {"heading": "5. Security Architecture", "level": 1, "content_instructions": "Cover authentication (OAuth 2.0 / API keys), encryption (TLS 1.3, AES-256 at rest), tokenisation, credential validation flow, and audit logging.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "6. Error Handling & Retry Policy", "level": 1, "content_instructions": "Define error codes, retry logic (exponential backoff), idempotency keys, and timeout escalation paths for each failure scenario.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "7. Performance & Scalability Design", "level": 1, "content_instructions": "Describe horizontal scaling strategy, database sharding, caching layer (Redis), and load testing targets (TPS, P99 latency).", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "8. Integration Points", "level": 1, "content_instructions": f"Document all external integration touchpoints, including the {authority} switch, participant back-office systems, risk/abuse detection, and the notification service. Include SLA per integration.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "9. Deployment Architecture", "level": 1, "content_instructions": "Describe containerisation (Docker/K8s), CI/CD pipeline, environment matrix (dev/staging/prod), and rollback procedure.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "10. Monitoring & Observability", "level": 1, "content_instructions": "Define metrics (Prometheus), logging (ELK), alerting thresholds, SLO dashboards, and on-call escalation paths.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
            ],
        },
        "Product Note": {
            "title": "Product Note",
            "subtitle": f"{dn_}Feature Product Brief",
            "doc_type": "Product Note",
            "sections": [
                {"heading": "1. Feature Overview", "level": 1, "content_instructions": f"Describe the feature in plain language: what it does, who it serves, and why it matters for the {dn_}ecosystem.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "2. Strategic Rationale", "level": 1, "content_instructions": "Explain how this feature aligns with the authority's stated vision, the regulator's roadmap, and competitive ecosystem positioning.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "3. Market Opportunity", "level": 1, "content_instructions": "Quantify the addressable market, target transaction volume, and expected participant/user adoption in Year 1.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "4. Product Capabilities", "level": 1, "content_instructions": "List key product capabilities with brief description of each. Group by: Core, Enhanced, and Future capabilities.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "5. Go-to-Market Plan", "level": 1, "content_instructions": "Define the GTM phases: Pilot (select participants), Limited Launch, and General Availability. Include readiness criteria for each phase.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "6. Revenue & Pricing Model", "level": 1, "content_instructions": pricing, "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "7. Compliance Summary", "level": 1, "content_instructions": "Summarise the key regulatory requirements and how the product meets them. Reference the specific regulator directives and authority circulars supplied in context.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
            ],
        },
        "Circular": {
            "title": "Operational Circular",
            "subtitle": f"{authority} Directive" if authority != "the issuing authority" else "Authority Directive",
            "doc_type": "Circular",
            "sections": [
                {"heading": "Letterhead & Reference Block", "level": 1, "content_instructions": letterhead, "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "Addressee Line", "level": 1, "content_instructions": addressees, "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "Subject", "level": 1, "content_instructions": "One-line subject in formal sentence case naming the specific feature and its scope.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "Context", "level": 1, "content_instructions": "3-5 sentences describing the current ecosystem state and the reason for this circular. Factual and vendor-neutral.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "Decision & Scope", "level": 1, "content_instructions": f"Begin with '{authority} has decided to...' and specify the exact artefacts, APIs, or processes being mandated.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "Participant Obligations", "level": 1, "content_instructions": "For each participant category, list mandatory ('must') and advisory ('are advised to') obligations with a go-live deadline.", "include_table": True, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
                {"heading": "Signature Block", "level": 1, "content_instructions": "Close with 'Yours Sincerely,' then 'SD/-', then authorising official's name, designation, and department.", "include_table": False, "include_diagram": False, "diagram_type": "flowchart", "diagram_description": ""},
            ],
        },
    }


def _get_default_plan(doc_type: str) -> dict:
    """Return the pack-aware per-type default plan, falling back to the generic one."""
    import copy
    return _build_default_plans().get(doc_type) or copy.deepcopy(DEFAULT_PLAN)


# ---------------------------------------------------------------------------
# Full per-type planner system prompts
# ---------------------------------------------------------------------------

_SECTION_SCHEMA = load_prompt("docgen/agents/pipeline/section_schema.md")

_COMMON_JSON_RULES = load_prompt("docgen/agents/pipeline/common_json_rules.md")

# TODO: BRD/TSD separation gaps flagged in review. Status:
#   1. FIXED (2026-07): the BRD blueprint sections (document_guides.py) no longer
#      demand XML payloads / field-level contract tables / per-code TD-BD wire
#      content, and write_content strips BRD code blocks again — so the BRD is
#      business-level. _COMMON_BRD_RULES + these blueprint sections now agree.
#   2. _COMMON_TSD_RULES (below) is dead — never interpolated into either TSD
#      prompt, so the TSD half of the separation never reaches the model.
#   3. _filter_blueprint_by_tier keeps physical blueprint order, so the merged
#      "Envisaged Changes" sections render after annexures/glossary for the
#      COMPACT/STANDARD tiers. Needs an explicit canonical key order.
_COMMON_BRD_RULES = load_prompt("docgen/agents/pipeline/common_brd_rules.md")

# Domain-specific role responsibilities (e.g. UPI's "Issuer Bank = customer
# auth, account validation, debit, KYC") come from the ACTIVE PACK, appended to
# the static rules file at the seam — prompt files are static (the loader does
# no interpolation), so per-domain lines cannot live inside the .md. A pack
# that supplies no ownership block gets only the generic layer model.
_OWNERSHIP_ROLES_BLOCK = prompt_block("docgen_ownership_roles", "")
if _OWNERSHIP_ROLES_BLOCK:
    _COMMON_BRD_RULES = _COMMON_BRD_RULES + "\n\n" + _OWNERSHIP_ROLES_BLOCK


_COMMON_TSD_RULES = load_prompt("docgen/agents/pipeline/common_tsd_rules.md")


# Backward-compat alias — anything still importing the old name continues
# to work, biased toward BRD rules (the more conservative choice).
_COMMON_AUTHORITY_HARD_RULES = _COMMON_BRD_RULES

# Ecosystem knowledge spliced into EVERY writer/planner prompt — participants,
# message types, canonical flows. It comes from the ACTIVE PACK, for every
# document type.
#
# It used to come from `common_network_domain.md` for BRD/TSD/PN/Circular and from
# the pack only for the generic fallback. That split is why a library-domain TSD
# still read "No new NPCI wire codes": the writer prompt handed the model UPI
# ecosystem knowledge no matter which pack was active, and the model used the
# vocabulary it was given. There is no doc type for which another ecosystem's
# participants are the right context, so there is no longer a second path.
#
# Empty is a valid, meaningful answer: a pack that supplies no domain notes gets
# none, rather than inheriting a foreign ecosystem's.
_PACK_DOMAIN_NOTES = prompt_block("docgen_generic_domain_notes", "")

# Architecture/engineering-design principles the TSD is held to (e.g. modularity,
# mechanical sympathy, autoscaling, observability, failure handling). Externalized
# so a self-hosted deployment can swap in its own organization's principles by
# editing this ONE file — no code change required. An empty file is a valid
# choice: the TSD pipeline runs fine with no architecture guidance beyond what
# the section blueprints already ask for.
_ARCHITECTURE_PRINCIPLES = load_prompt("docgen/agents/pipeline/architecture_principles.md")

# Wire-schema namespace guidance for generated diagrams. Domain-specific (UPI's
# xmlns:upi=..., NLLN's default-namespace binding), so it comes from the pack;
# an empty block means the diagram prompt simply carries no namespace rule.
_SCHEMA_NAMESPACE_NOTE = prompt_block("docgen_schema_namespace_note", "")


def _planner_system_prompt(doc_type: str) -> str:
    """Return the full, doc-type-specific system prompt for the planner node."""
    normalized = (doc_type or "").strip().lower()

    if normalized == "brd":
        return _BRD_SYSTEM_PROMPT
    elif normalized == "tsd":
        return _TSD_SYSTEM_PROMPT
    elif normalized in ("product note", "prd"):
        return _PN_SYSTEM_PROMPT
    elif normalized == "circular":
        return _CIRCULAR_SYSTEM_PROMPT
    else:
        return _GENERIC_SYSTEM_PROMPT.format(doc_type=doc_type)


# ---------------------------------------------------------------------------
# BRD system prompt
# ---------------------------------------------------------------------------

# The third Envisaged-Changes stakeholder (8.3) is domain-specific: UPI names
# the Issuer Bank, a library network names the Lending Library. Resolved from
# the pack so the planner never plans a foreign ecosystem's section.
_ENVISAGED_ROLE_LABEL = prompt_block(
    "docgen_envisaged_changes_role_label", "Servicing Participant"
)

_BRD_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/brd_system_prompt.md",
    SECTION_SCHEMA=_SECTION_SCHEMA, COMMON_BRD_RULES=_COMMON_BRD_RULES, DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, COMMON_JSON_RULES=_COMMON_JSON_RULES,
    ENVISAGED_ROLE_LABEL=_ENVISAGED_ROLE_LABEL,
)


# ---------------------------------------------------------------------------
# TSD system prompt
# ---------------------------------------------------------------------------

_TSD_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/tsd_system_prompt.md",
    SECTION_SCHEMA=_SECTION_SCHEMA, DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, COMMON_JSON_RULES=_COMMON_JSON_RULES,
    ARCHITECTURE_PRINCIPLES=_ARCHITECTURE_PRINCIPLES,
)


# ---------------------------------------------------------------------------
# Product Note system prompt
# ---------------------------------------------------------------------------

_PN_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/pn_system_prompt.md",
    SECTION_SCHEMA=_SECTION_SCHEMA, DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, COMMON_JSON_RULES=_COMMON_JSON_RULES,
)


# ---------------------------------------------------------------------------
# Circular system prompt
# ---------------------------------------------------------------------------

_CIRCULAR_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/circular_system_prompt.md",
    SECTION_SCHEMA=_SECTION_SCHEMA, DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, COMMON_JSON_RULES=_COMMON_JSON_RULES,
)


# ---------------------------------------------------------------------------
# Generic fallback system prompt
# ---------------------------------------------------------------------------

_GENERIC_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/generic_system_prompt.md",
    SECTION_SCHEMA=_SECTION_SCHEMA, DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, COMMON_JSON_RULES=_COMMON_JSON_RULES,
)


# ---------------------------------------------------------------------------
# Node 1 — retrieve_context
# ---------------------------------------------------------------------------

def retrieve_context(state: dict) -> dict:
    logger.info("[retrieve_context] job_id=%s", state.get("job_id"))
    state["status"] = "retrieving"

    try:
        prompt = state.get("prompt", "")
        collection = state.get("collection_name", "default")
        use_rag = state.get("use_rag", True)

        if not use_rag or not collection:
            state["rag_chunks"] = []
            state["rag_context"] = ""
            return state

        # Retrieval query must be the short feature description, NOT the
        # rich_prompt blob (feature + research + canvas, often 4-10k chars).
        # The blob causes query_understanding to produce generic doc-template
        # sub-questions that don't match the indexed authority/network corpus.
        # Pass topic="" so rag_bridge skips a second pass on document_title
        # ("BRD: change …") which would pollute results with off-topic chunks.
        retrieval_query = (
            state.get("feature_description")
            or " ".join(prompt.split()[:30])
        )

        chunks, context = retrieve_multi_query(
            prompt=retrieval_query,
            topic="",
            collection_name=collection,
        )

        state["rag_chunks"] = chunks
        state["rag_context"] = context
        if chunks:
            logger.info(
                "[retrieve_context] collection=%s, retrieved_chunks=%d, rag_enabled=%s",
                collection,
                len(chunks),
                use_rag,
            )
        else:
            logger.warning(
                "[retrieve_context] collection=%s, no chunks retrieved — documents will be generated without RAG context.",
                collection,
            )

        # If a reference file was uploaded, extract its structure
        ref_path = state.get("reference_file_path")
        if ref_path and Path(ref_path).exists():
            from app.docgen.rag_bridge import extract_reference_structure
            state["reference_structure"] = extract_reference_structure(ref_path)

        # ── BRD tier classification (only for BRD doc_type) ─────────────
        # Picks compact / standard / comprehensive per feature complexity so
        # we don't force a 30-section blueprint on a small contained change.
        # Honour explicit override from build_initial_state / WS payload first.
        if (state.get("doc_type") or "").strip().lower() == "brd":
            override = (state.get("brd_tier_override") or "").strip().lower()
            if override in ("compact", "standard", "comprehensive"):
                state["brd_tier"] = override
                logger.info("[brd_tier] override accepted: %s", override)
            else:
                feature_text = (
                    (state.get("prompt") or "")
                    + "\n\n"
                    + (state.get("research_report") or "")[:2000]
                    + "\n\n"
                    + (state.get("additional_context") or "")[:2000]
                )
                state["brd_tier"] = _classify_brd_tier(feature_text)
                logger.info("[brd_tier] classified: %s", state["brd_tier"])

    except Exception as e:
        logger.error("[retrieve_context] error: %s", e, exc_info=True)
        state["error"] = f"Context retrieval failed: {e}"
        state["status"] = "FAILED"

    return state


# ─────────────────────────────────────────────────────────────────────────────
# BRD tier classifier — chooses compact / standard / comprehensive per feature.
# ─────────────────────────────────────────────────────────────────────────────

_BRD_TIER_CLASSIFIER_SYSTEM = load_prompt("docgen/agents/pipeline/brd_tier_classifier_system.md")


def _classify_brd_tier(feature_text: str) -> str:
    """Classify a BRD by complexity. Returns 'compact' / 'standard' / 'comprehensive'.

    Fail-safe: empty/unclassifiable input returns 'standard' (~20 sections), NOT 'comprehensive'
    (~29). Comprehensive forces the full participant-matrix + regulatory + SLA template, which is
    pure "no change to X" bloat for the small/internal changes that dominate — so it must be earned
    by a real classification, never the silent fallback.
    """
    feature_text = (feature_text or "").strip()
    if not feature_text:
        return "standard"
    try:
        # Use the docgen JSON-mode bridge — same auth path as the planner.
        from app.docgen.llm_bridge import make_llm_json
        llm = make_llm_json(max_tokens=300, agent_name="brd_tier_classifier")
        resp = llm.invoke([
            SystemMessage(content=_BRD_TIER_CLASSIFIER_SYSTEM),
            HumanMessage(content=feature_text[:6000]),
        ])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        parsed = _parse_json(raw)
        if isinstance(parsed, dict):
            tier = str(parsed.get("tier", "")).strip().lower()
            if tier in ("compact", "standard", "comprehensive"):
                logger.info(
                    "[brd_tier_classifier] tier=%s rationale=%s",
                    tier, parsed.get("rationale", "")[:160],
                )
                return tier
        logger.warning(
            "[brd_tier_classifier] unexpected response, defaulting to standard: %r",
            raw[:200],
        )
    except Exception as e:
        logger.warning("[brd_tier_classifier] failed (defaulting to standard): %s", e)
    return "standard"


# ---------------------------------------------------------------------------
# Plan validation helper
# ---------------------------------------------------------------------------

def _validate_plan(plan: dict, doc_type: str) -> list[str]:
    """
    Return a list of error strings if the LLM-generated plan fails minimum quality checks.
    An empty list means the plan is acceptable.
    Note: blueprint plans bypass this function entirely (they return early in plan_document).
    """
    errors: list[str] = []

    sections = plan.get("sections", [])

    # Sections must exist
    if not sections:
        errors.append("Plan has no sections.")
        return errors

    placeholder_markers = (
        "[tbd]",
        "[to be updated]",
        "this section covers",
        "details to be elaborated",
        "to be elaborated further",
    )

    # No explicit placeholder content — catch obvious lazy planner output
    for sec in sections:
        instr = (sec.get("content_instructions", "") or "").strip()
        lowered = instr.lower()
        if any(marker in lowered for marker in placeholder_markers):
            errors.append(
                f"Section '{sec.get('heading')}' has placeholder content_instructions and must be regenerated."
            )

    return errors


# ---------------------------------------------------------------------------
# Node 2 — plan_document
# ---------------------------------------------------------------------------

def plan_document(state: dict) -> dict:
    logger.info("[plan_document] job_id=%s", state.get("job_id"))
    state["status"] = "planning"

    try:
        prompt = state.get("prompt", "")
        doc_type = state.get("doc_type", "BRD")
        brief = {
            "prompt": prompt,
            "document_title": state.get("document_title"),
            "version_number": state.get("version_number"),
            "classification": state.get("classification"),
            "audience": state.get("audience"),
            "desired_outcome": state.get("desired_outcome"),
            "format_constraints": state.get("format_constraints"),
            "organization_name": state.get("organization_name"),
            "reference_code": state.get("reference_code"),
            "issue_date": state.get("issue_date"),
            "recipient_line": state.get("recipient_line"),
            "subject_line": state.get("subject_line"),
            "signatory_name": state.get("signatory_name"),
            "signatory_title": state.get("signatory_title"),
            "signatory_department": state.get("signatory_department"),
            # BRD tier (compact / standard / comprehensive). Classified once
            # in retrieve_context_node; honoured by document_guides.
            "brd_tier": state.get("brd_tier") or "comprehensive",
        }
        blueprint_plan = build_blueprint_plan(doc_type, brief)
        if blueprint_plan:
            plan_data = DocumentPlan.model_validate(blueprint_plan).model_dump()
            state["document_plan"] = plan_data
            # Extract diagram specs from blueprint sections
            diagram_specs = []
            for i, section in enumerate(plan_data.get("sections", [])):
                if section.get("include_diagram") and state.get("include_diagrams", True):
                    diagram_specs.append({
                        "diagram_id": f"diagram_{i}_{uuid.uuid4().hex[:6]}",
                        "section_index": i,
                        "target_heading": section.get("heading", ""),
                        "diagram_type": section.get("diagram_type", "flowchart"),
                        "description": section.get("diagram_description", section.get("heading", "")),
                        "caption": section.get("diagram_description", section.get("heading", "")),
                    })
            state["diagram_specs"] = diagram_specs
            save_json_artifact(state.get("job_id", "tmp"), "document_plan.json", plan_data)
            return state

        llm = _make_llm_json()
        rag_context = state.get("rag_context", "")
        ref_structure = state.get("reference_structure", "")
        additional_context = state.get("additional_context", "")
        audience = state.get("audience", "")
        desired_outcome = state.get("desired_outcome", "")
        format_constraints = state.get("format_constraints", "")
        proposals = state.get("proposals") or {}
        taxonomy = state.get("taxonomy") or {}
        proposals_block = ""
        if proposals:
            import json as _json
            proposals_block = (
                "\n\nAUTHORITATIVE TECHNICAL SPECIFICATIONS (derived from NPCI documentation):\n"
                f"APIs: {_json.dumps(proposals.get('apis', []), indent=2)}\n"
                f"Auth Method: {proposals.get('auth_method', 'UPI PIN')}\n"
                f"Transaction Limit: {proposals.get('transaction_limit', 'As per NPCI guidelines')}\n"
                f"Flow Sequence: {_json.dumps(proposals.get('flow_sequence', []), indent=2)}\n"
                f"Error Codes: {_json.dumps(proposals.get('error_codes', []), indent=2)}\n"
                "Use these exact API names, field names, and error codes in the plan.\n"
            )
        if taxonomy:
            proposals_block += (
                f"\nFeature Classification: {taxonomy.get('primary_category', '')} "
                f"({', '.join(taxonomy.get('labels', []))})\n"
            )

        context_block = ""
        if rag_context:
            # Gap 3 FIX: was [:3000]; raised to [:8000] to avoid silently dropping
            # the latter half of multi-chunk RAG results.
            _RAG_LIMIT = 8000
            if len(rag_context) > _RAG_LIMIT:
                logger.warning(
                    "[plan_document] RAG context truncated from %d → %d chars",
                    len(rag_context), _RAG_LIMIT,
                )
            context_block = (
                "\nRelevant knowledge-base context. Use it only if it directly supports the user request:\n"
                f"{rag_context[:_RAG_LIMIT]}\n"
            )
        if ref_structure:
            context_block += f"\nReference document structure:\n{ref_structure}\n"
        if additional_context:
            context_block += f"\nAdditional context:\n{additional_context}\n"
        if audience:
            context_block += f"\nAudience:\n{audience}\n"
        if desired_outcome:
            context_block += f"\nDesired outcome:\n{desired_outcome}\n"
        if format_constraints:
            context_block += f"\nFormat constraints:\n{format_constraints}\n"

        system_msg = _planner_system_prompt(doc_type)
        user_msg = (
            f"Create a {doc_type} document plan for the following request:\n\n"
            f"{prompt}\n"
            f"{proposals_block}"
            f"{context_block}"
        )
        messages = [SystemMessage(content=system_msg), HumanMessage(content=user_msg)]

        response = llm.invoke(messages)
        raw = response.content if hasattr(response, "content") else str(response)
        plan_data = _parse_json_with_recovery(llm, raw, messages, _get_default_plan(state.get("doc_type", "BRD")), context=f"plan_document/{doc_type}")

        # Validate required keys
        if not isinstance(plan_data, dict) or "sections" not in plan_data:
            plan_data = _get_default_plan(state.get("doc_type", "BRD"))

        if not plan_data.get("title"):
            plan_data["title"] = f"{doc_type} Document"

        plan_data = DocumentPlan.model_validate(plan_data).model_dump()
        state["document_plan"] = plan_data
        save_json_artifact(state.get("job_id", "tmp"), "document_plan.json", plan_data)

        # Post-planning validation
        doc_type = plan_data.get("doc_type", state.get("doc_type", "BRD"))
        plan_errors = _validate_plan(plan_data, doc_type)
        if plan_errors:
            logger.warning(
                "[plan_document] Plan validation issues: %s — re-planning once",
                "; ".join(plan_errors),
            )
            retry_msg = (
                f"The plan you generated has the following issues:\n"
                + "\n".join(f"- {e}" for e in plan_errors)
                + "\n\nPlease fix ALL issues and return the corrected plan JSON."
            )
            messages_retry = messages + [HumanMessage(content=retry_msg)]
            response2 = llm.invoke(messages_retry)
            raw2 = response2.content if hasattr(response2, "content") else str(response2)
            plan_data2 = _parse_json_with_recovery(
                llm, raw2, messages_retry, plan_data, context="plan_document/retry"
            )
            if isinstance(plan_data2, dict) and "sections" in plan_data2:
                plan_data = DocumentPlan.model_validate(plan_data2).model_dump()
                state["document_plan"] = plan_data
                save_json_artifact(state.get("job_id", "tmp"), "document_plan.json", plan_data)

        # Extract diagram specs — include target_heading for positional embedding
        diagram_specs = []
        for i, section in enumerate(plan_data.get("sections", [])):
            if section.get("include_diagram") and state.get("include_diagrams", True):
                diagram_specs.append({
                    "diagram_id": f"diagram_{i}_{uuid.uuid4().hex[:6]}",
                    "section_index": i,               # kept for legacy compat
                    "target_heading": section.get("heading", ""),
                    "diagram_type": section.get("diagram_type", "flowchart"),
                    "description": section.get("diagram_description", section.get("heading", "")),
                    "caption": section.get("diagram_description", section.get("heading", "")),
                })

        state["diagram_specs"] = diagram_specs

    except Exception as e:
        logger.error("[plan_document] error: %s", e, exc_info=True)
        state["error"] = f"Document planning failed: {e}"
        state["status"] = "FAILED"
        state["document_plan"] = _get_default_plan(state.get("doc_type", "BRD"))

    return state


# ---------------------------------------------------------------------------
# Node 3 — generate_diagrams
# ---------------------------------------------------------------------------

def _generate_single_diagram(llm: ChatOllama, spec: dict, output_dir: str, llm_content=None) -> tuple[str, str, dict]:
    """Generate one diagram. Returns (diagram_id, path_or_empty).

    Strategy:
    1. Ask LLM for a valid PlantUML @startuml...@enduml block.
    2. Try to render it with plantuml.jar (requires Java + jar on disk).
    3. Fall back to Pillow-based rendering using a JSON spec if PlantUML is unavailable.
    """
    from app.docgen.tools.diagram_generator import generate_plantuml_diagram, _find_plantuml_jar

    diagram_id = spec["diagram_id"]
    dtype = spec["diagram_type"]
    description = spec["description"]
    out_path = str(Path(output_dir) / f"{diagram_id}.png")

    engine = getattr(settings, "diagram_engine", "plantuml")

    # ── Mermaid path (engine=mermaid; needs mmdc, else falls through) ──
    if engine == "mermaid":
        from app.docgen.tools.diagram_generator import render_mermaid
        # Type-specific syntax + a valid example. Sequence diagrams are the main
        # failure mode (strict arrow tokens: "Expecting SOLID_ARROW…"), so give the
        # model the exact grammar rather than hoping it guesses it.
        _mmd_hint = {
            "sequence": (
                "Header: `sequenceDiagram`. Declare each participant first: `participant P as Label`.\n"
                "Every message MUST use a valid arrow: `A->>B: text` (request) or `A-->>B: text` "
                "(response). NEVER use unicode arrows (→) or `->`/`=>`. Keep each message on ONE "
                "line; the text after the first `:` must not contain another `:` or an arrow. "
                "Notes: `Note over A,B: text`. Branches: `alt cond`/`else`/`end`, `opt cond`/`end`, "
                "`loop cond`/`end`.\n"
                "VALID EXAMPLE:\nsequenceDiagram\n  participant U as User\n  participant A as App\n"
                "  U->>A: set daily limit\n  A-->>U: limit confirmed\n"
            ),
            "flowchart": (
                "Header: `flowchart TD`. Nodes: `id[Label]` (process), `id{Label}` (decision), "
                "`id([Label])` (start/end); node ids must be alphanumeric (no spaces). "
                "Edges: `A --> B` or `A -->|label| B`.\n"
                "VALID EXAMPLE:\nflowchart TD\n  S([Start]) --> P[Process]\n  P --> D{Approved?}\n"
                "  D -->|Yes| E([End])\n  D -->|No| P\n"
            ),
        }
        _hint = _mmd_hint.get(dtype, _mmd_hint["flowchart"])
        mermaid_system = (
            f"You are a Mermaid expert. Generate ONE valid Mermaid {dtype} diagram that renders "
            "with mermaid-cli (mmdc).\n"
            "Return ONLY the Mermaid source — no explanation, no code fences.\n"
            f"SYNTAX (follow exactly):\n{_hint}\n"
            "Include the actors/steps/decisions from the description; keep it to 6-14 steps.\n"
        )

        def _clean_mmd(txt: str) -> str:
            t = re.sub(r"^```(?:mermaid)?\s*", "", (txt or "").strip(), flags=re.IGNORECASE)
            return re.sub(r"\s*```$", "", t).strip()

        try:
            _llm_mmd = llm_content if llm_content is not None else _make_llm_content()
            resp0 = _llm_mmd.invoke([SystemMessage(content=mermaid_system),
                                     HumanMessage(content=f"Create a {dtype} Mermaid diagram for: {description}")])
            raw_mmd = _clean_mmd(resp0.content if hasattr(resp0, "content") else str(resp0))
            if raw_mmd:
                mmd_result, mmd_err = render_mermaid(raw_mmd, out_path)
                if mmd_result:
                    logger.info("Mermaid diagram generated: %s -> %s", diagram_id, out_path)
                    return diagram_id, mmd_result, {"source_type": "mermaid", "source": raw_mmd}
                # ONE retry: feed the actual mmdc parse error back to the model to fix the
                # syntax before falling through to PlantUML. Most fallbacks were invalid
                # Mermaid the model emitted, not a real Mermaid failure.
                if mmd_err:
                    logger.info("Mermaid render failed for %s (%s) — retrying with the error",
                                diagram_id, mmd_err[:80])
                    fix_system = ("You are a Mermaid expert. The Mermaid below FAILED to render with "
                                  "mmdc. Fix the SYNTAX so it renders; keep the same diagram content. "
                                  f"SYNTAX (follow exactly):\n{_hint}\n"
                                  "Return ONLY the corrected Mermaid source — no explanation, no fences.")
                    fix_user = (f"mmdc error:\n{mmd_err[:600]}\n\nInvalid Mermaid:\n{raw_mmd}\n\n"
                                "Return the corrected Mermaid source only.")
                    resp1 = _llm_mmd.invoke([SystemMessage(content=fix_system),
                                             HumanMessage(content=fix_user)])
                    fixed_mmd = _clean_mmd(resp1.content if hasattr(resp1, "content") else str(resp1))
                    if fixed_mmd and fixed_mmd != raw_mmd:
                        retry_result, _ = render_mermaid(fixed_mmd, out_path)
                        if retry_result:
                            logger.info("Mermaid diagram generated on retry: %s -> %s", diagram_id, out_path)
                            return diagram_id, retry_result, {"source_type": "mermaid", "source": fixed_mmd}
        except Exception as e:
            logger.warning("Mermaid LLM/render step failed for %s: %s — falling back", diagram_id, e)

    # ── Step 1 & 2: PlantUML path ──────────────────────────────────────
    plantuml_system = (
        f"You are a PlantUML expert. Generate a valid PlantUML {dtype} diagram.\n"
        "Rules:\n"
        "- Return ONLY the @startuml...@enduml block — no explanation, no markdown fences.\n"
        "- First line must be @startuml, last line must be @enduml.\n"
        f"- Diagram type: {dtype}\n"
        "- For ACTIVITY diagrams: every activity step MUST end with a semicolon, e.g. :Process Payment;\n"
        "- For SEQUENCE diagrams: declare all participants; show every message with a label.\n"
        "- For FLOWCHART (use_case / component): use --> arrows with labels.\n"
        "- Include all actors, steps, and decision points relevant to the description.\n"
        "- Keep it to 6-14 steps/messages for readability.\n"
        + (f"{_SCHEMA_NAMESPACE_NOTE}\n" if _SCHEMA_NAMESPACE_NOTE else "")
    )
    plantuml_user = f"Create a {dtype} PlantUML diagram for: {description}"

    try:
        _llm_puml = llm_content if llm_content is not None else _make_llm_content()
        resp = _llm_puml.invoke([SystemMessage(content=plantuml_system), HumanMessage(content=plantuml_user)])
        raw_puml = resp.content if hasattr(resp, "content") else str(resp)
        # Strip any markdown fences
        raw_puml = re.sub(r"^```(?:plantuml)?\s*", "", raw_puml.strip(), flags=re.IGNORECASE)
        raw_puml = re.sub(r"\s*```$", "", raw_puml)
        raw_puml = raw_puml.strip()
        if "@startuml" in raw_puml:
            puml_result = generate_plantuml_diagram(raw_puml, out_path)
            if puml_result:
                logger.info("PlantUML diagram generated: %s -> %s", diagram_id, out_path)
                return diagram_id, puml_result, {"source_type": "plantuml", "source": raw_puml}
    except Exception as e:
        logger.warning("PlantUML LLM/render step failed for %s: %s", diagram_id, e)

    # ── Step 3: Pillow fallback — ask for JSON spec ────────────────────
    schema_hint = {
        "sequence": (
            '{"title": "Authentication Sequence", "subtitle": "Happy path and validation checks", '
            '"actors": ["User", "Frontend", "Auth API"], '
            '"messages": [{"from_actor": "Actor1", "to_actor": "Actor2", '
            '"label": "Request", "direction": "forward"}], '
            '"notes": ["Optional note"]}'
        ),
        "flowchart": (
            '{"title": "Provisioning Flow", "subtitle": "Main steps, branch points, and outcomes", '
            '"nodes": [{"id": "start", "label": "Start", "node_type": "start"}, '
            '{"id": "p1", "label": "Process Step", "node_type": "process"}, '
            '{"id": "d1", "label": "Decision", "node_type": "decision"}, '
            '{"id": "end", "label": "End", "node_type": "end"}], '
            '"edges": [{"from_node": "start", "to_node": "p1", "label": ""}, '
            '{"from_node": "p1", "to_node": "d1", "label": ""}, '
            '{"from_node": "d1", "to_node": "end", "label": "Yes"}]}'
        ),
        "activity": (
            '{"title": "Cross-team Workflow", "subtitle": "Ownership by lane and handoff sequence", '
            '"lanes": ["Lane 1", "Lane 2"], '
            '"activities": [{"id": "a1", "label": "Activity 1", "lane": "Lane 1", "row": 0}], '
            '"edges": [{"from_id": "a1", "to_id": "a2", "label": ""}]}'
        ),
    }
    hint = schema_hint.get(dtype, schema_hint["flowchart"])
    json_system = (
        f"You are a diagram specification generator. "
        f"Create a {dtype} diagram spec as STRICT JSON. "
        "Respond with valid JSON ONLY. No markdown, no explanation. "
        f"Use this schema example:\n{hint}\n"
        "Make the diagram informative and presentation-ready. "
        "Use a clear title, a short subtitle, explicit labels, and realistic domain terminology. "
        "Prefer 4-7 nodes/messages when that improves clarity."
    )
    try:
        resp2 = llm.invoke([SystemMessage(content=json_system), HumanMessage(content=f"Create a {dtype} diagram for: {description}")])
        raw2 = resp2.content if hasattr(resp2, "content") else str(resp2)
        diagram_spec = _parse_json(raw2)
    except Exception as e:
        logger.warning("LLM JSON diagram spec failed for %s: %s, using empty", diagram_id, e)
        diagram_spec = {}

    result = generate_diagram(diagram_spec, dtype, out_path)
    return diagram_id, result or "", {"source_type": "json", "source": diagram_spec}


def _party_flow_lines(fs: dict | None) -> list[str]:
    """Numbered hop lines (1..N) across the plan's party_flows — ONE numbering,
    used by BOTH the flow diagram and the BRD's transaction-flow text so the
    arrows in the picture and the steps in the prose cannot disagree."""
    entries = [e for e in ((fs or {}).get("party_flows") or []) if isinstance(e, dict)]
    lines: list[str] = []
    n = 0
    for e in entries:
        for h in (e.get("hops") or []):
            if not isinstance(h, dict):
                continue
            n += 1
            msg = h.get("message") or e.get("api") or ""
            line = f"{n}. {h.get('from', '?')} -> {h.get('to', '?')}: {msg}".strip()
            note = str(h.get("note") or "").strip()
            if note:
                line += f" — Note: {note}"
            lines.append(line)
    return lines


def _party_flow_entities(fs: dict | None) -> list[str]:
    """Every party appearing in the party_flows hops, in first-seen order — the
    participants of the flow diagram. Only the entities actually involved: a
    2-party flow renders 2 boxes, EFRM appears only when a hop names it."""
    seen: list[str] = []
    for e in ((fs or {}).get("party_flows") or []):
        if not isinstance(e, dict):
            continue
        for h in (e.get("hops") or []):
            if not isinstance(h, dict):
                continue
            for p in (str(h.get("from") or "").strip(), str(h.get("to") or "").strip()):
                if p and p not in seen:
                    seen.append(p)
    return seen


def _flow_spec_anchor(fs: dict | None) -> str:
    """Render the approved shared flow_spec (ChangeAnalysis) as an explicit actors+steps
    anchor (accuracy: shared-spec diagrams). The SAME anchor is injected into the BRD's
    and the TSD's flow diagrams, so they render from one source and cannot deviate.

    When the plan carries party_flows (the evidence-cited four-party routes), the
    anchor renders from THOSE: every involved entity becomes a labelled participant
    box and every hop a numbered arrow (1, 2, …) — the NPCI flow-diagram style the
    BRD's Transaction Flow section requires."""
    if not isinstance(fs, dict) or not fs:
        return ""
    pf_lines = _party_flow_lines(fs)
    if pf_lines:
        entities = _party_flow_entities(fs)
        return ("\n\nRender the flow EXACTLY from the approved party flow below — the "
                "diagram style is entity boxes with numbered arrows:\n"
                "Entities (render EVERY one as its own labelled participant, and ONLY "
                "these — no extra parties): " + ", ".join(entities) + "\n"
                "Numbered message arrows (keep the numbers on the arrow labels, same "
                "order):\n" + "\n".join(pf_lines) +
                "\nWhere a line carries '— Note: …', render that note as a small annotation "
                "under that numbered arrow (the detail bullets of that step), not as an "
                "extra arrow.\n"
                "Do NOT invent, omit, rename, or reorder entities or arrows — the same "
                "spec drives the document's numbered step list, so they must match.")
    lines: list[str] = []
    actors = fs.get("actors") or []
    if actors:
        lines.append("Actors: " + ", ".join(str(a) for a in actors))
    for i, s in enumerate(fs.get("steps") or [], 1):
        if isinstance(s, dict):
            lines.append(f"{s.get('id', i)}. {s.get('actor', '')}: "
                         f"{s.get('action') or s.get('text') or s.get('message') or ''}".strip())
        else:
            lines.append(f"{i}. {s}")
    for m in fs.get("messages") or []:
        if isinstance(m, dict):
            lines.append(f"{m.get('from', '')} -> {m.get('to', '')}: "
                         f"{m.get('label') or m.get('message') or ''}".strip())
    if not lines:
        return ""
    return ("\n\nUse EXACTLY these actors and steps from the approved shared flow spec — do NOT "
            "invent, omit, rename, or reorder them (this same spec drives the other document's "
            "diagram, so the two must match):\n" + "\n".join(lines))


def generate_diagrams(state: dict) -> dict:
    logger.info("[generate_diagrams] job_id=%s, specs=%d",
                state.get("job_id"), len(state.get("diagram_specs", [])))
    state["status"] = "generating_diagrams"

    try:
        specs = state.get("diagram_specs", [])
        if not specs or not state.get("include_diagrams", True):
            state["generated_diagrams"] = {}
            return state

        output_dir = str(Path(settings.output_dir) / state.get("job_id", "tmp"))
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        llm = _make_llm_json()
        generated: dict[str, str] = {}
        sources: dict[str, dict] = {}
        # Shared flow spec → identical flow/sequence/activity diagrams across BRD & TSD.
        flow_anchor = _flow_spec_anchor(state.get("source_flow_spec"))
        pf_entities = _party_flow_entities(state.get("source_flow_spec"))

        for spec in specs:
            if flow_anchor and spec.get("diagram_type") in ("sequence", "flowchart", "activity"):
                # The blueprint's canned caption names the generic four-party route and can
                # CONTRADICT the approved flow (e.g. "…→ Beneficiary Bank" on a balance
                # enquiry that never touches the payee side). Stamp the caption from the
                # party-flow entities so the figure text matches the figure. Mutates the
                # spec in `specs` so the persisted plan + docx assembly both see it.
                if pf_entities:
                    spec["caption"] = "Transaction flow: " + " → ".join(pf_entities)
                spec = {**spec, "description": (spec.get("description") or "") + flow_anchor}
            try:
                did, path, src = _generate_single_diagram(llm, spec, output_dir, llm_content=_make_llm_content())

                if path:
                    generated[did] = path
                    sources[did] = src
                    logger.info("Generated diagram: %s -> %s", did, path)
            except Exception as e:
                logger.warning("Skipping diagram %s due to error: %s", spec.get("diagram_id"), e)

        state["generated_diagrams"] = generated
        # Persist diagram SOURCE (not just the PNG) so diagrams are re-renderable
        # and surgically editable. Additive artifact; the docx renderer ignores it.
        state["generated_diagram_sources"] = sources
        _job_id = state.get("job_id", "tmp")
        try:
            save_json_artifact(_job_id, "generated_diagram_sources.json", sources)
            # Persist the diagram→PNG map AND the diagram_specs so EDITORS can re-embed
            # diagrams on re-assembly. Without this, any edit (including the consistency
            # auto-repair) reloads an EMPTY map + empty specs and silently drops every
            # diagram from the re-assembled .docx. The editors read these two from disk,
            # but the pipeline never wrote them — a gap since docgen editing first landed
            # (565ed883, 2026-04-30). The fresh pipeline embeds from in-memory state, so
            # only edited/repaired documents were affected.
            save_json_artifact(_job_id, "generated_diagrams.json", generated)
            from app.docgen.plan_store import artifact_dir
            _plan_path = artifact_dir(_job_id) / "document_plan.json"
            if _plan_path.exists():
                import json as _json
                _plan = _json.loads(_plan_path.read_text(encoding="utf-8"))
                _plan["diagram_specs"] = specs
                _plan_path.write_text(_json.dumps(_plan, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("[generate_diagrams] diagram wiring persistence skipped: %s", e)

    except Exception as e:
        logger.error("[generate_diagrams] error: %s", e, exc_info=True)
        state["error"] = f"Diagram generation failed: {e}"
        state["status"] = "FAILED"

    return state


# ---------------------------------------------------------------------------
# Full per-type writer system prompts
# ---------------------------------------------------------------------------

_WRITER_CONTENT_SCHEMA = load_prompt("docgen/agents/pipeline/writer_content_schema.md")

_WRITER_JSON_RULES = load_prompt("docgen/agents/pipeline/writer_json_rules.md")

_BRD_WRITER_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/brd_writer_system_prompt.md",
    COMMON_BRD_RULES=_COMMON_BRD_RULES, DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, WRITER_CONTENT_SCHEMA=_WRITER_CONTENT_SCHEMA, WRITER_JSON_RULES=_WRITER_JSON_RULES,
)


_TSD_WRITER_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/tsd_writer_system_prompt.md",
    DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, WRITER_CONTENT_SCHEMA=_WRITER_CONTENT_SCHEMA, WRITER_JSON_RULES=_WRITER_JSON_RULES,
    ARCHITECTURE_PRINCIPLES=_ARCHITECTURE_PRINCIPLES,
)


_PN_WRITER_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/pn_writer_system_prompt.md",
    DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, WRITER_CONTENT_SCHEMA=_WRITER_CONTENT_SCHEMA, WRITER_JSON_RULES=_WRITER_JSON_RULES,
)


_CIRCULAR_WRITER_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/circular_writer_system_prompt.md",
    DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, WRITER_CONTENT_SCHEMA=_WRITER_CONTENT_SCHEMA, WRITER_JSON_RULES=_WRITER_JSON_RULES,
)


_GENERIC_WRITER_SYSTEM_PROMPT = render_prompt(
    "docgen/agents/pipeline/generic_writer_system_prompt.md",
    DOMAIN_KNOWLEDGE=_PACK_DOMAIN_NOTES, WRITER_CONTENT_SCHEMA=_WRITER_CONTENT_SCHEMA, WRITER_JSON_RULES=_WRITER_JSON_RULES,
)


# Conciseness discipline — applied to EVERY section writer. The pipeline was producing 170KB+
# documents for small internal features by restating the same facts in section after section and
# spelling out "no change to X" for every uninvolved party. Size to the change, say each fact once.
_CONCISENESS_CLAUSE = load_prompt("docgen/agents/pipeline/conciseness_clause.md")


def _writer_system_prompt(doc_type: str) -> str:
    """Return the full, doc-type-specific system prompt for the writer node (+ the shared
    conciseness discipline that keeps documents sized to the change)."""
    normalized = (doc_type or "").strip().lower()
    if normalized == "brd":
        base = _BRD_WRITER_SYSTEM_PROMPT
    elif normalized == "tsd":
        base = _TSD_WRITER_SYSTEM_PROMPT
    elif normalized in ("product note", "prd"):
        base = _PN_WRITER_SYSTEM_PROMPT
    elif normalized == "circular":
        base = _CIRCULAR_WRITER_SYSTEM_PROMPT
    else:
        base = _GENERIC_WRITER_SYSTEM_PROMPT
    return base + _CONCISENESS_CLAUSE


# ---------------------------------------------------------------------------
# Node 4 — write_content
# ---------------------------------------------------------------------------

def _write_section(
    llm: ChatOllama,
    section: dict,
    rag_context: str,
    doc_type: str,
    audience: str = "",
    desired_outcome: str = "",
    feature_prompt: str = "",
    proposals: dict = None,
    source_skeleton: str = "",
    decisions_block: str = "",
    source_xsd: str = "",
    source_xsd_bundle: str = "",
    tech_design: str = "",
    party_flow_steps: str = "",
    ratified_plan: str = "",
) -> dict:
    section_key = section.get("section_key")
    heading = section.get("heading", "Section")
    render_style = section.get("render_style", "body")
    instructions = section.get("content_instructions", f"Write content for {heading}")
    prompt_instruction = section.get("prompt_instruction", "")
    include_table = section.get("include_table", False)
    table_guidance = _table_guidance(section_key, heading) if include_table else ""

    system_msg = _writer_system_prompt(doc_type)


    proposals_snippet = ""
    if proposals:
        proposals_snippet = (
            "AUTHORITATIVE TECHNICAL SPECIFICATIONS — use these exact names, do NOT invent alternatives:\n"
            f"APIs: {json.dumps(proposals.get('apis', []))}\n"
            f"Request Fields: {json.dumps(proposals.get('request_fields', []))}\n"
            f"Error Codes: {json.dumps(proposals.get('error_codes', []))}\n"
            f"Auth Method: {proposals.get('auth_method', '')}\n"
            f"Flow: {json.dumps(proposals.get('flow_sequence', []))}\n"
        )

    # Bumped 2k → 8k so the writer receives the full [S#]-tagged corpus block
    # plus Source index footer (citation rule depends on it).
    context_snippet = rag_context[:8000] if rag_context else ""
    # The upstream binding spine (e.g. the approved BRD's FRs + architecture for
    # a TSD). Injected high-priority and UNTRUNCATED — feature_prompt below is
    # sliced to 3000 chars and, for a TSD, the BRD sits at the tail of that
    # merged blob and never survives the slice. Without this the writer authors
    # each section from planner-compressed instructions alone and the flows
    # drift from the source document.
    skeleton_block = ""
    if source_skeleton:
        # Capped like the sibling context blocks — this is injected into EVERY
        # parallel section write, so an uncapped spine multiplies token cost by
        # the section count. 8k chars comfortably holds the FR list + architecture.
        skeleton_block = (
            "SOURCE DOCUMENT — BINDING FLOW SKELETON "
            "(your section MUST stay consistent with these requirements and flows; "
            "do NOT re-derive, rename, reorder, or contradict them — refine to this "
            "document's altitude, never re-author the flow):\n"
            f"{source_skeleton[:8000]}\n"
        )

    # Human-ratified Decision Ledger — the binding decisions every downstream
    # section must respect. Capped (injected into every parallel section write).
    decisions_chunk = ""
    if decisions_block:
        decisions_chunk = (
            "DECISIONS (BINDING — human-ratified; your section MUST NOT contradict, re-derive, "
            "or reopen any of these):\n" + decisions_block[:6000] + "\n"
        )

    # Approved schemas verbatim — the TSD must REPRODUCE these real element/field/
    # namespace names, never invent XML. Injected directly (capped) so it survives
    # the feature_prompt[:3000] slice that would otherwise drop it.
    xsd_chunk = ""
    if source_xsd:
        xsd_chunk = (
            "REALIZED XSD SCHEMA CHANGE (AUTHORITATIVE — this is what was ACTUALLY schema-modeled "
            "for THIS change; reproduce these real element/field/namespace names EXACTLY and do NOT "
            "invent or rename them). SCOPE RULE: the wire/schema footprint of this change is LIMITED "
            "to what appears here plus the ratified decisions — do NOT present new APIs, message "
            "types, or services that are absent from this schema as if they are being built; if the "
            "broader feature implies them, label them explicitly as future/out-of-scope, not as part "
            "of this change:\n"
            f"{source_xsd[:8000]}\n"
        )

    # Ratified code-grounded technical design (classes/methods/keys/codes). Injected directly
    # (capped) so it SURVIVES the feature_prompt[:3000] slice that would otherwise drop it — the
    # exact reason the TSD used to hand-wave the implementation. The engineering sections name
    # these identifiers VERBATIM.
    design_chunk = ""
    if tech_design:
        design_chunk = (
            "RATIFIED TECHNICAL DESIGN (AUTHORITATIVE — name these EXACT classes, methods, cache "
            "keys, config keys, and response codes VERBATIM; do NOT paraphrase or rename them to "
            "cleaner synonyms, and do NOT invent identifiers the design does not list):\n"
            f"{tech_design[:7000]}\n"
        )

    # Ratified functional plan (the human-approved solution). For the BRD this is the
    # authoritative source the document is written FROM — injected directly (capped) so it
    # SURVIVES the feature_prompt[:3000] slice. Business altitude: it carries scope/decisions,
    # not implementation identifiers (those are the TSD's tech_design).
    plan_chunk = ""
    if ratified_plan:
        plan_chunk = (
            "RATIFIED PLAN (AUTHORITATIVE — the human-approved solution this document must reflect. "
            "Document its scope, decisions, requirements, and compatibility faithfully; do NOT "
            "contradict, exceed, or re-author the plan, and do NOT introduce scope it does not "
            "contain):\n"
            f"{ratified_plan[:6000]}\n"
        )

    # Prompt-cache split: everything RUN-CONSTANT (identical bytes for all ~10-30 parallel
    # section calls of one document run) goes into cacheable SYSTEM segments; the user
    # message carries only the per-section tail. Call 1 writes the ~13k-token prefix to the
    # Anthropic cache; calls 2..N (and every _parse_json_with_recovery replay) read it at
    # ~10% input price. Non-Claude providers just see the segments flattened to one string.
    # `plan_chunk` is run-constant (one ratified plan per document), so it belongs here.
    shared_blob = (
        (f"{proposals_snippet}\n" if proposals_snippet else "")
        + (f"{decisions_chunk}\n" if decisions_chunk else "")
        + (f"{xsd_chunk}\n" if xsd_chunk else "")
        + (f"{design_chunk}\n" if design_chunk else "")
        + (f"{plan_chunk}\n" if plan_chunk else "")
        + (f"{skeleton_block}\n" if skeleton_block else "")
        + (f"Feature context (use this as the primary source of specific details):\n{feature_prompt[:3000]}\n" if feature_prompt else "")
        + (f"Audience focus: {audience}\n" if audience else "")
        + (f"Desired outcome: {desired_outcome}\n" if desired_outcome else "")
        + (
            f"\n## RETRIEVED CORPUS EVIDENCE (authoritative — cite [S#] inline on every grounded claim)\n"
            f"{context_snippet}\n"
            f"INSTRUCTION: Every regulatory obligation, error code, API name, limit, "
            f"settlement timeline, or compliance claim that has corpus support above MUST "
            f"carry an inline [S#] tag. Reproduce the Source index verbatim under a "
            f"'References' section at the end of the document.\n"
            if context_snippet else ""
        )
    )
    # Wire-facing sections additionally receive the UNCHANGED schemas involved in
    # the flow (siblings/imports of the changed one — e.g. the RespTransfer to a changed
    # ReqTransfer) so XML samples, field dictionaries, and flow narratives cite REAL
    # sibling structure instead of re-inventing it. Injected in the per-section
    # user message, NOT the shared blob: the blob must stay byte-identical across
    # all parallel section calls for the prompt cache, and only these few sections
    # need the extra schema weight.
    _WIRE_SECTIONS = {"interface_spec", "control_flow", "annexure_api_changes"}
    # The flow sections reproduce the approved party flow as their numbered steps —
    # the SAME numbering the flow diagram's arrows carry (both derive from
    # flow_spec.party_flows), so the picture and the prose cannot disagree.
    _FLOW_SECTIONS = {"product_construct_transaction", "control_flow"}
    flow_steps_chunk = ""
    if party_flow_steps and section_key in _FLOW_SECTIONS:
        flow_steps_chunk = (
            "APPROVED PARTY FLOW (AUTHORITATIVE — reproduce these steps as this section's "
            "numbered_items EXACTLY: same order, same numbering, one item per hop; they must "
            "match the flow diagram's numbered arrows. Involve ONLY the entities named here — "
            "do not add parties the flow does not touch):\n"
            f"{party_flow_steps}\n"
        )
    bundle_chunk = ""
    if source_xsd_bundle and section_key in _WIRE_SECTIONS:
        bundle_chunk = (
            "INVOLVED FLOW SCHEMAS (UNCHANGED — authoritative structure of the existing "
            "messages this change's flow touches; derive XML samples and field tables for "
            "these messages from THIS text, reproduce element/attribute/namespace names "
            "EXACTLY, and do NOT invent or rename anything):\n"
            f"{source_xsd_bundle}\n"
        )
    user_msg = (
        f"Write content for section: '{heading}'\n"
        f"Instructions: {instructions}\n"
        + (f"{flow_steps_chunk}" if flow_steps_chunk else "")
        + (f"{bundle_chunk}" if bundle_chunk else "")
        + (f"Required structure/style: {prompt_instruction}\n" if prompt_instruction else "")
        + (f"Required table format: {table_guidance}\n" if table_guidance else "")
        + (
            "CRITICAL: Your JSON MUST include non-null table_data with non-empty headers and rows "
            "exactly as specified. Omitting the table breaks document generation.\n"
            if include_table
            else ""
        )
    )

    fallback = {
        "section_key": section_key,
        "section_heading": heading,
        "render_style": render_style,
        "paragraphs": [f"This section covers {heading}.", "Details to be elaborated further."],
        "bullet_points": [],
        "numbered_items": [],
        "code_blocks": [],
        "table_data": fallback_table_data(section, heading) if include_table else None,
    }

    from app.core.prompt_blocks import segments_for_anthropic_cache
    system_segments = segments_for_anthropic_cache(
        [(system_msg, True), (shared_blob, True)])   # static writer prompt | run-shared context
    messages = [SystemMessage(content=system_segments), HumanMessage(content=user_msg)]
    try:
        response = llm.invoke(messages)
        raw = response.content if hasattr(response, "content") else str(response)
        content = _parse_json_with_recovery(llm, raw, messages, fallback, context=f"write_section/{heading}")
        if not isinstance(content, dict):
            content = fallback
        content.setdefault("section_heading", heading)
        content.setdefault("section_key", section_key)
        content.setdefault("render_style", render_style)
        content.setdefault("paragraphs", [])
        content.setdefault("bullet_points", [])
        content.setdefault("numbered_items", [])
        content.setdefault("code_blocks", [])
        content.setdefault("table_data", None)
        return GeneratedContent.model_validate(content).model_dump()
    except Exception as e:
        logger.warning("Content generation failed for '%s': %s", heading, e)
        return GeneratedContent.model_validate(fallback).model_dump()


def write_content(state: dict) -> dict:
    logger.info("[write_content] job_id=%s", state.get("job_id"))
    state["status"] = "writing"

    try:
        plan = state.get("document_plan", DEFAULT_PLAN)
        rag_context = state.get("rag_context", "")

        llm = _make_llm_json()
        sections_data = plan.get("sections", [])
        generated_sections = []
        doc_type = plan.get("doc_type", state.get("doc_type", "BRD"))
        audience = plan.get("document_meta", {}).get("audience", state.get("audience", ""))
        desired_outcome = plan.get("document_meta", {}).get("desired_outcome", state.get("desired_outcome", ""))
        feature_prompt = state.get("prompt", "")  # fix pre-existing NameError in the docgen source

        from concurrent.futures import ThreadPoolExecutor, as_completed
        proposals = state.get("proposals") or {}
        source_skeleton = state.get("source_skeleton") or ""
        decisions_block = state.get("decisions_block") or ""
        source_xsd = state.get("source_xsd") or ""
        source_xsd_bundle = state.get("source_xsd_bundle") or ""
        tech_design = state.get("tech_design") or ""
        party_flow_steps = "\n".join(_party_flow_lines(state.get("source_flow_spec")))
        # Deterministic API-spec gate: when the API Registry covers wire APIs, the
        # writer must NOT author their field dictionaries — authoritative tables are
        # inserted post-write from registry rows (see below). Core set = ratified
        # inputs; the full registry rides along so a post-write sweep can cover any
        # registry API the prose turns out to mention (QA findings I1/I2).
        _reg_by_name = state.get("api_registry_specs_by_name") or {}
        _reg_core = [n for n in (state.get("api_registry_core_names") or []) if n in _reg_by_name]
        _legacy_specs = state.get("api_registry_specs") or []
        if _legacy_specs and not _reg_by_name:
            _reg_by_name = {s["api_name"]: s for s in _legacy_specs}
            _reg_core = list(_reg_by_name)
        if _reg_by_name:
            _reg_names = ", ".join(sorted(_reg_by_name))
            for _sec in sections_data:
                if _sec.get("section_key") == "interface_spec":
                    _sec["content_instructions"] = (
                        _sec.get("content_instructions", "")
                        + " REGISTRY OVERRIDE: the platform API Registry holds the authoritative "
                          f"per-field dictionaries and message XML for: {_reg_names}. A registry-rendered "
                          "spec section is appended automatically for EVERY one of these APIs this "
                          "document names anywhere. Do NOT write field dictionary tables or XML samples "
                          "for ANY of them — list them in the Interfaces-in-Scope table with role/status "
                          "and change narrative only; write full detail only for interfaces NOT in that "
                          "list, and do not name registry APIs the flow does not actually touch."
                    )
        ratified_plan = state.get("ratified_plan") or ""
        def _write_one(idx_section: tuple[int, dict]) -> tuple[int, dict]:
            idx, section = idx_section
            content = _write_section(
                llm,
                section,
                rag_context,
                doc_type=doc_type,
                audience=audience,
                desired_outcome=desired_outcome,
                feature_prompt=feature_prompt,
                proposals=proposals,
                source_skeleton=source_skeleton,
                decisions_block=decisions_block,
                source_xsd=source_xsd,
                source_xsd_bundle=source_xsd_bundle,
                tech_design=tech_design,
                party_flow_steps=party_flow_steps,
                ratified_plan=ratified_plan,
            )
            content["section_key"] = section.get("section_key")
            content["render_style"] = section.get("render_style", "body")
            content["level"] = section.get("level", 1)
            content["section_heading"] = section.get("heading", content.get("section_heading", ""))
            # BRD + Product Note are BUSINESS-level documents — strip code blocks so no
            # XML/JSON wire payloads leak in. Wire-level detail belongs in the TSD.
            # (Reverts the 2026-06-05 "NPCI BRDs are technical artefacts" change so the
            # BRD is functional again; the legacy editors already stripped BRD code blocks.)
            if doc_type.strip().lower() in ("brd", "product note"):
                content["code_blocks"] = []
            return idx, content

        configured_workers = min(settings.max_parallel_sections, len(sections_data)) or 1
        if _provider() == "openai_compat":
            # vLLM-backed JSON generation degrades under heavy concurrent section fan-out.
            max_workers = 1
        else:
            max_workers = configured_workers
        logger.info(
            "[write_content] job_id=%s sections=%d max_workers=%d provider=%s",
            state.get("job_id"),
            len(sections_data),
            max_workers,
            _provider(),
        )
        results: dict[int, dict] = {}
        # Parallel section writers run in worker THREADS, which do NOT inherit the caller's
        # contextvars — so the docgen usage context (change_request_id / section) set by the
        # runner was lost, mis-attributing section-generation LLM spend (and the per-call
        # transcript dump) to "misc" instead of this change. Run each worker inside a COPY of
        # the submitting thread's context so both land under the right change.
        import contextvars as _cv
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_cv.copy_context().run, _write_one, (i, section)): i
                for i, section in enumerate(sections_data)
            }
            for future in as_completed(futures):
                idx, content = future.result()
                results[idx] = content

        # Reconstruct in original section order
        generated_sections = [results[i] for i in range(len(sections_data))]

        repaired, repair_notes = repair_sections_for_validation(plan, generated_sections)
        if repair_notes:
            logger.info(
                "[write_content] Auto-repaired %d section issue(s): %s",
                len(repair_notes),
                repair_notes,
            )
        generated_sections = repaired
        # Normalize SCREAMING_SNAKE UPI API placeholders (REQ_FOO/RESP_FOO) to canonical
        # Req/Resp PascalCase — the writer occasionally emits them despite the prompt rule,
        # and the doc validator then flags them as invented_api_name. Deterministic, fail-open.
        try:
            from app.agents.document_validator import canonicalize_api_names as _canon_api

            def _canon_cell(c):
                return _canon_api(c) if isinstance(c, str) else c

            def _canon_row(_row):
                # Rows come in two shapes (both handled by the renderer): a list of
                # cells, or a header-keyed dict. Preserve the shape — iterating a dict
                # with `for c in row` would yield its KEYS and drop every value.
                if isinstance(_row, dict):
                    return {k: _canon_cell(v) for k, v in _row.items()}
                if isinstance(_row, list):
                    return [_canon_cell(c) for c in _row]
                return _row

            for _sec in generated_sections:
                # NB: code_blocks are intentionally excluded — schema/XML/JSON snippets
                # carry real SCREAMING_SNAKE field/constant identifiers (RESP_CODE_DESC,
                # REQ_TIMEOUT_MS) that the REQ_/RESP_ heuristic would corrupt. Invented
                # API-name *references* live in prose/tables/headings, which we do fix.
                for _f in ("paragraphs", "bullet_points", "numbered_items"):
                    if isinstance(_sec.get(_f), list):
                        _sec[_f] = [_canon_cell(x) for x in _sec[_f]]
                if isinstance(_sec.get("section_heading"), str):
                    _sec["section_heading"] = _canon_api(_sec["section_heading"])
                _td = _sec.get("table_data")
                if isinstance(_td, dict):
                    if isinstance(_td.get("headers"), list):
                        _td["headers"] = [_canon_cell(h) for h in _td["headers"]]
                    if isinstance(_td.get("rows"), list):
                        _td["rows"] = [_canon_row(_row) for _row in _td["rows"]]
        except Exception as e:  # noqa: BLE001
            logger.warning("[write_content] API-name normalization skipped: %s", e)
        # Deterministic API-spec sections — rendered from API Registry rows, never by
        # the LLM (same rows in → identical tables out; immune to token-cap truncation).
        # Final set = stable core + post-write sweep: any registry API the generated
        # sections mention (prose, tables, code blocks — the same surface the eval
        # check scans) gets its section too, so the doc is self-consistent with
        # check_tsd_api_specs_registry_backed by construction. Inserted into BOTH the
        # plan and the generated list at the SAME index: repair/validation align
        # plan↔sections BY INDEX and compare lengths, so the two lists must stay parallel.
        try:
            if _reg_by_name:
                from app.services.api_registry_ingest import (
                    derive_involved_api_names, render_registry_sections, sections_scan_text)
                _mentioned = derive_involved_api_names(sections_scan_text(generated_sections))
                _extras = sorted(n for n in _mentioned if n in _reg_by_name and n not in _reg_core)
                _final_names = _reg_core + _extras
                _reg_sections = render_registry_sections([_reg_by_name[n] for n in _final_names])
                _plan_secs = plan.setdefault("sections", [])
                _idx = next((i for i, s in enumerate(generated_sections)
                             if s.get("section_key") == "interface_spec"),
                            len(generated_sections) - 1)
                _plan_entries = [{
                    "section_key": s["section_key"], "heading": s["section_heading"],
                    "level": s.get("level", 2), "render_style": "body",
                    "include_table": True, "include_diagram": False,
                    "content_instructions": "Rendered deterministically from the API Registry.",
                } for s in _reg_sections]
                generated_sections[_idx + 1:_idx + 1] = _reg_sections
                if _idx + 1 <= len(_plan_secs):
                    _plan_secs[_idx + 1:_idx + 1] = _plan_entries
                else:
                    _plan_secs.extend(_plan_entries)
                logger.info("[write_content] inserted %d registry-rendered API spec section(s)",
                            len(_reg_sections))
        except Exception as e:  # noqa: BLE001 — registry sections are additive, never fatal
            logger.warning("[write_content] registry section injection skipped: %s", e)
        # Assign stable per-block IDs so the surgical (patch-based) editor can
        # address individual paragraphs/rows. Additive metadata — the docx
        # renderer ignores it. Fail-open: generation must never break on this.
        try:
            from app.docgen.block_ids import ensure_document_ids
            ensure_document_ids(generated_sections)
        except Exception as e:  # noqa: BLE001
            logger.warning("[write_content] block-id assignment skipped: %s", e)
        state["generated_sections"] = generated_sections
        save_json_artifact(state.get("job_id", "tmp"), "generated_sections.json", generated_sections)

    except Exception as e:
        logger.error("[write_content] error: %s", e, exc_info=True)
        state["error"] = f"Content writing failed: {e}"
        state["status"] = "FAILED"

    return state


# ---------------------------------------------------------------------------
# Node 5 — assemble_document
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Node 5 — review_document
# ---------------------------------------------------------------------------

def review_document(state: dict) -> dict:
    logger.info("[review_document] job_id=%s", state.get("job_id"))
    state["status"] = "reviewing"

    try:
        plan = state.get("document_plan", DEFAULT_PLAN)
        sections = state.get("generated_sections", [])
        sections, repair_notes = repair_sections_for_validation(plan, sections)
        if repair_notes:
            logger.info(
                "[review_document] Auto-repaired %d section issue(s): %s",
                len(repair_notes),
                repair_notes,
            )
            state["generated_sections"] = sections
        review = validate_generated_document(
            plan=plan,
            sections=sections,
            include_diagrams=state.get("include_diagrams", True),
        )
        if repair_notes:
            review.setdefault("warnings", [])
            review["warnings"].extend(f"Auto-repair: {n}" for n in repair_notes)
        state["review_report"] = review
        save_json_artifact(state.get("job_id", "tmp"), "review_report.json", review)
        if review["errors"]:
            state["error"] = "Document validation failed: " + "; ".join(review["errors"])
            state["status"] = "FAILED"
            return state
    except Exception as e:
        logger.error("[review_document] error: %s", e, exc_info=True)
        state["error"] = f"Document review failed: {e}"
        state["status"] = "FAILED"

    return state


# ---------------------------------------------------------------------------
# Node 6 — assemble_document
# ---------------------------------------------------------------------------

def assemble_doc(state: dict) -> dict:
    logger.info("[assemble_document] job_id=%s", state.get("job_id"))
    state["status"] = "assembling"

    try:
        job_id = state.get("job_id", "unknown")
        plan = state.get("document_plan", DEFAULT_PLAN)
        sections = state.get("generated_sections", [])
        diagram_specs = state.get("diagram_specs", [])
        generated_diagrams = state.get("generated_diagrams", {})

        session_id = state.get("session_id")
        doc_type_slug = plan.get("doc_type", "document").replace(" ", "_").lower()
        if session_id:
            # Session-scoped storage: all docs for a session in one folder
            session_dir = Path(settings.output_dir) / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(session_dir / f"{doc_type_slug}_{job_id[:8]}.docx")
        else:
            output_path = str(Path(settings.output_dir) / job_id / "document.docx")
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        final_path = assemble_document(
            plan,
            sections,
            output_path,
            diagram_specs=diagram_specs,
            generated_diagrams=generated_diagrams,
        )
        state["output_path"] = final_path
        state["status"] = "completed"
        logger.info("Document assembled: %s", final_path)

        doc_type = plan.get("doc_type", "")
        diagrams_embedded = sum(
            1 for s in diagram_specs
            if generated_diagrams.get(s.get("diagram_id", ""))
        )
        logger.info(
            "[assemble_document] doc_type=%s sections=%d diagrams_embedded=%d",
            doc_type, len(sections), diagrams_embedded,
        )

    except Exception as e:
        logger.error("[assemble_document] error: %s", e, exc_info=True)
        state["error"] = f"Document assembly failed: {e}"
        state["status"] = "FAILED"

    return state


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

def handle_error(state: dict) -> dict:
    logger.error("[handle_error] job_id=%s, error=%s", state.get("job_id"), state.get("error"))
    state["status"] = "failed"
    return state


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------

def _route_or_error(next_node: str):
    def router(state: dict) -> str:
        if state.get("error") or state.get("status") == "FAILED":
            return "handle_error"
        return next_node
    return router


# ---------------------------------------------------------------------------
# Build the StateGraph
# ---------------------------------------------------------------------------

def build_pipeline() -> Any:
    graph = StateGraph(dict)

    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("plan_document", plan_document)
    graph.add_node("generate_diagrams", generate_diagrams)
    graph.add_node("write_content", write_content)
    graph.add_node("review_document", review_document)
    graph.add_node("assemble_document", assemble_doc)
    graph.add_node("handle_error", handle_error)

    graph.set_entry_point("retrieve_context")

    graph.add_conditional_edges(
        "retrieve_context",
        _route_or_error("plan_document"),
        {"plan_document": "plan_document", "handle_error": "handle_error"},
    )
    graph.add_conditional_edges(
        "plan_document",
        _route_or_error("generate_diagrams"),
        {"generate_diagrams": "generate_diagrams", "handle_error": "handle_error"},
    )
    graph.add_conditional_edges(
        "generate_diagrams",
        _route_or_error("write_content"),
        {"write_content": "write_content", "handle_error": "handle_error"},
    )
    graph.add_conditional_edges(
        "write_content",
        _route_or_error("review_document"),
        {"review_document": "review_document", "handle_error": "handle_error"},
    )
    graph.add_conditional_edges(
        "review_document",
        _route_or_error("assemble_document"),
        {"assemble_document": "assemble_document", "handle_error": "handle_error"},
    )
    graph.add_edge("assemble_document", END)
    graph.add_edge("handle_error", END)

    return graph.compile()


def build_docgen_subgraph():
    """Build and return the document generation pipeline as a compiled LangGraph subgraph.

    This compiled graph can be embedded as a node in a parent StateGraph:

        parent_graph.add_node("docgen", build_docgen_subgraph())

    Required input state keys (must be provided by the parent graph):
        - job_id (str): Unique identifier for tracking and artifact storage.
        - prompt (str): User prompt / feature description.
        - doc_type (str): "BRD" | "TSD" | "Product Note" | "Circular"

    Optional input state keys:
        - session_id, document_title, version_number, classification,
          collection_name, use_rag, include_diagrams, audience,
          desired_outcome, format_constraints, organization_name,
          reference_code, issue_date, recipient_line, subject_line,
          signatory_name, signatory_title, signatory_department,
          additional_context

    Output state keys populated:
        - document_plan (dict)
        - diagram_specs (list)
        - generated_diagrams (dict)
        - generated_sections (list)
        - output_path (str | None): Path to the generated .docx file.
        - status (str): "completed" | "failed"
        - error (str | None)

    See SUBGRAPH_INTEGRATION_GUIDE.md for full usage examples.
    """
    return build_pipeline()


def _table_guidance(section_key: str | None, heading: str) -> str:
    key = (section_key or heading).lower()
    if "error" in key:
        return "Headers should be [Response Code, Error Code, Description, API, Entity, TD/BD]."
    if "testing" in key:
        return "Headers should be [Scenario, Objective, Owner] with realistic certification/test scenarios."
    if any(token in key for token in ("transaction", "setting", "construct", "responsibilities", "api")):
        return "Headers should be [Step, Activity, Responsible] with Pre-Check, Step 1..N, and Post Response rows."
    return "Use concise, realistic headers aligned to the section purpose."


def _section_mode_guidance(doc_type: str, section_key: str | None, heading: str) -> str:
    key = (section_key or heading).lower()
    normalized_doc_type = (doc_type or "").lower()

    if normalized_doc_type == "tsd":
        return (
            "Use precise technical language. Prefer exact field names and message names only when they are grounded in the supplied prompt/context. "
            "Do not invent XML tags, APIs, or schema attributes."
        )
    if normalized_doc_type == "product note":
        return (
            "Explain the feature in product and operational language. Avoid raw XML snippets, code identifiers, and internal class or handler names."
        )
    if normalized_doc_type == "brd":
        return (
            "Focus on stakeholder accountability, business impact, and required changes. Structure the prose so implementation ownership is explicit."
        )
    if normalized_doc_type == "circular":
        return (
            "Keep the content formal, directive, and concise. Avoid unnecessary narrative detail and do not add technical implementation trivia."
        )
    if "error" in key:
        return "Make the content operationally precise and aligned to realistic failure handling."
    return ""


# Singleton compiled pipeline
_pipeline = None


def get_pipeline():
    global _pipeline
    if _pipeline is None:
        _pipeline = build_pipeline()
    return _pipeline


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------

def run_pipeline(initial_state: dict) -> dict:
    """Run the full pipeline and return the final state."""
    pipeline = get_pipeline()
    final_state = pipeline.invoke(initial_state)
    return final_state
