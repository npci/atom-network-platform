# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Structured JSON proposals extractor.

Produces a network-grounded JSON "skeleton" (APIs, fields, error codes, flow,
participant obligations, etc.) BEFORE any narrative document is written.
Downstream agents (BRD, Tech Spec, XSD, Product Kit) inject this skeleton
into their prompts so the LLM works from real NPCI names/patterns instead
of inventing plausible-sounding ones.

Why this matters
────────────────
Without proposals, BRD/TSD generators happily hallucinate API names like
"PayInitRequest" or invent error codes like "ERR_TXN_FAIL". With proposals,
the LLM sees the exact shapes from the corpus (e.g. ReqTransfer / RespTransfer,
U67 / Z9 / RB) and reuses them consistently across every document.

Pattern adapted from a standalone RAG agent implementation.
(lines 69-117) with NET-specific constraints re-tightened.
"""
import logging
from app.core.prompts import load_prompt
from typing import Any

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.llm import call_llm
from app.core.llm_router import pick_model_for_agent
from app.core.json_recovery import parse_llm_json
from app.core.domain.contract import participants_of
from app.core.domain.registry import get_active_pack, prompt_block

# Supplied by the active domain pack, not imported from a UPI module.
DOMAIN_HARD_RULES = prompt_block("hard_rules")
DOMAIN_ERROR_CODE_EXAMPLES = prompt_block("error_codes")

# Domain nouns / rule fragments from the active pack, resolved at import
# (registry pattern). Under the default UPI pack the assembled system template
# renders byte-identically to the previous hardcoded text.
_AUTHORITY = prompt_block("authority", "the ecosystem authority")
_DOMAIN = prompt_block("domain_name", "").strip()
_DOMAIN_ADJ = f"{_DOMAIN} " if _DOMAIN else ""
_ERROR_CODE_RULE = prompt_block(
    "proposals_error_code_rule",
    "- Error codes MUST follow this domain's declared error-code format.\n"
    "  NEVER emit HTTP status codes (400, 401, 500) as error codes.",
)
_ERROR_CLASS_RULE = prompt_block(
    "proposals_error_class_rule",
    '- Give every error code its two-letter classification in the "td_bd" '
    "field, using the classes this domain defines.",
)
_PARTICIPANT_LIST = participants_of(get_active_pack())
_AUTH_ACTOR = next(
    (p.label for p in _PARTICIPANT_LIST if p.is_authority), "The authority")
_PART_ACTOR = next(
    (p.label for p in _PARTICIPANT_LIST if not p.is_authority), "Each participant")

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Schema — the LLM must produce this exact shape. Empty lists / null values
# are acceptable when a field is genuinely unknown.
# ──────────────────────────────────────────────────────────────────────────────

_PROPOSALS_SCHEMA_EXAMPLE = load_prompt("agents/proposals_extractor/proposals_schema_example.md")


# ──────────────────────────────────────────────────────────────────────────────
# System prompt — mixes the taxonomy-specific required_fields and the UPI hard
# rules so the LLM knows both WHAT to extract and HOW to format it.
# ──────────────────────────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = f"""You are the {_DOMAIN_ADJ}Technical Proposals Extractor for {_AUTHORITY}.

Given a feature description, retrieved context from similar past features,
and the feature's taxonomy classification, produce a single JSON object
that captures the feature's technical shape: APIs, fields, error codes,
flow steps, participant obligations, and test scenarios.

Priority order when values conflict:
1. Explicit statements in the FEATURE DESCRIPTION (authoritative)
2. Names, field lists, and error codes present in the RETRIEVED CONTEXT
3. Reasonable {_AUTHORITY} {_DOMAIN_ADJ}conventions (fallback only; mark such entries
   with an explicit "assumption" note in the description text)

MUST-DO:
- Include at LEAST 6 functional_requirements, each prefixed "FR-01:", "FR-02:", etc.
{_ERROR_CODE_RULE}
{_ERROR_CLASS_RULE}
- Every flow_sequence step MUST name the responsible actor.
- Use active obligation verbs ("{_AUTH_ACTOR} shall", "{_PART_ACTOR} must"), never "the system will".
- Required fields for this taxonomy bucket: {{required_fields}}
  Ensure each of these appears somewhere meaningful in your output.

OUTPUT FORMAT:
Return ONLY a single JSON object. No markdown fences, no commentary,
no explanation. Use null or an empty list [] when a detail is genuinely
unknown rather than inventing a value. Example shape:

{{schema_example}}

{{domain_rules}}

{{error_code_examples}}

""" + ANTI_INJECTION_CLAUSE


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

async def extract_proposals(
    feature_description: str,
    classification: dict,
    retrieved_chunks: list[dict] | None = None,
    max_tokens: int = 4096,
) -> dict[str, Any] | None:
    """Produce the structured proposals JSON.

    Args:
        feature_description: The enriched prompt / feature spec.
        classification:      Result of taxonomy.classify() — drives required_fields.
        retrieved_chunks:    RAG-retrieved chunks to ground naming (list of dicts
                             with at least 'content' and 'source_file' keys).

    Returns:
        Parsed dict matching the schema, or None if extraction failed completely.
    """
    if not feature_description or not feature_description.strip():
        logger.warning("extract_proposals: empty feature description")
        return None

    required_fields = (
        classification.get("bucket", {}).get("required_fields")
        or []
    )
    primary = classification.get("primary", "unknown")

    # Build context section
    context_parts: list[str] = []
    if retrieved_chunks:
        for c in retrieved_chunks[:8]:   # cap to avoid blowing past token budget
            header = f"[{c.get('doc_category','?')} | {c.get('source_file','?')}]"
            body = (c.get("content") or "").strip()
            if body:
                context_parts.append(f"{header}\n{body}")
    retrieved_block = "\n\n---\n\n".join(context_parts) if context_parts else "(no corpus context available)"

    system_prompt = _SYSTEM_TEMPLATE.format(
        required_fields=", ".join(required_fields) if required_fields else "(no taxonomy-specific required fields)",
        # The few-shot example is pack content (a worked spec in the domain's
        # own messages and codes); the file on disk is the domain-free default
        # for packs that supply none.
        schema_example=prompt_block(
            "proposals_few_shot_example", _PROPOSALS_SCHEMA_EXAMPLE),
        domain_rules=DOMAIN_HARD_RULES,
        error_code_examples=DOMAIN_ERROR_CODE_EXAMPLES,
    )

    user_content = (
        f"# Feature description\n{wrap_untrusted(feature_description, 'FEATURE_DESCRIPTION')}\n\n"
        f"# Taxonomy classification\nprimary: {primary}\n"
        f"confidence: {classification.get('confidence', 0.0)}\n"
        f"labels: {classification.get('labels', [])}\n\n"
        f"# Retrieved context (use only for names and conventions)\n{wrap_untrusted(retrieved_block, 'RETRIEVED_CONTEXT')}"
    )

    logger.info(
        "extract_proposals: feature_len=%d, taxonomy=%s, chunks=%d, required_fields=%d",
        len(feature_description), primary,
        len(retrieved_chunks) if retrieved_chunks else 0,
        len(required_fields),
    )

    # Slice 27a — proposals_extractor is Purpose.ROUTING (structured-extract
    # of API names / error codes / FRs from retrieved chunks).
    raw = await call_llm(
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=max_tokens,
        model=pick_model_for_agent("proposals_extractor"),
        agent_name="proposals_extractor",
    )

    proposals = await parse_llm_json(raw, fallback=None)
    if not proposals or not isinstance(proposals, dict):
        logger.error("extract_proposals: JSON recovery failed; first 500 chars of raw: %s", (raw or "")[:500])
        return None

    # Ensure every expected top-level key exists (missing → empty value of right type)
    defaults: dict[str, Any] = {
        "apis": [],
        "request_fields": [],
        "response_fields": [],
        "error_codes": [],
        "auth_method": None,
        "transaction_limit": None,
        "flow_sequence": [],
        "current_state": None,
        "limitations": None,
        "functional_requirements": [],
        "dispute_framework": None,
        "user_journey_plain": [],
        "test_scenarios": [],
        "policy_rules": [],
        "failure_scenarios": [],
        "participant_obligations": {},
        "go_live_timeline": None,
        "supersedes_circular": None,
    }
    for key, default in defaults.items():
        proposals.setdefault(key, default)

    logger.info(
        "extract_proposals: done — apis=%d fields=%d errors=%d frs=%d",
        len(proposals.get("apis") or []),
        len(proposals.get("request_fields") or []) + len(proposals.get("response_fields") or []),
        len(proposals.get("error_codes") or []),
        len(proposals.get("functional_requirements") or []),
    )

    return proposals


# ──────────────────────────────────────────────────────────────────────────────
# Confidence scoring (adapted from synthesis.py:309-316 in RAG_SYSTEM)
# ──────────────────────────────────────────────────────────────────────────────

def score_proposals_confidence(
    proposals: dict | None,
    *,
    had_corpus_context: bool,
    pm_confirmed: bool = False,
) -> str:
    """Return a confidence tier for a proposals dict:

      high          PM confirmed + substantive proposals + corpus-grounded
      medium-high   Substantive proposals + corpus-grounded
      medium        Proposals derived but sparse corpus
      low           Extraction failed or output too thin
    """
    if not proposals:
        return "low"

    has_apis   = bool(proposals.get("apis"))
    has_errors = bool(proposals.get("error_codes"))
    has_frs    = len(proposals.get("functional_requirements") or []) >= 6
    has_flow   = bool(proposals.get("flow_sequence"))

    substantive = sum([has_apis, has_errors, has_frs, has_flow]) >= 3
    if not substantive:
        return "low"

    if pm_confirmed and had_corpus_context:
        return "high"
    if had_corpus_context:
        return "medium-high"
    return "medium"


# ──────────────────────────────────────────────────────────────────────────────
# Render helper — inline format for pasting into downstream agent prompts
# ──────────────────────────────────────────────────────────────────────────────

def format_for_prompt(proposals: dict | None) -> str:
    """Render proposals as a compact string block for injection into BRD/TSD prompts.

    Omits empty sections to keep token cost proportional to useful content.
    """
    if not proposals:
        return "(no structured proposals available — the LLM must infer conservatively)"

    lines: list[str] = []

    def _section(title: str, items: list, formatter):
        if not items:
            return
        lines.append(f"\n### {title}")
        for it in items:
            lines.append(formatter(it))

    if proposals.get("apis"):
        lines.append("### APIs (use these names exactly)")
        for api in proposals["apis"]:
            lines.append(
                f"- {api.get('name', '?')} → {api.get('response', '?')} "
                f"(initiator: {api.get('initiator', '?')}): {api.get('description', '')}"
            )

    _section("Request Fields (use these names/types)",
             proposals.get("request_fields") or [],
             lambda f: f"- {f.get('name','?')} ({f.get('type','?')}, "
                       f"{'M' if f.get('mandatory') else 'O'}, {f.get('dLength','?')}): "
                       f"{f.get('description','')}")

    _section("Response Fields",
             proposals.get("response_fields") or [],
             lambda f: f"- {f.get('name','?')} ({f.get('type','?')}, "
                       f"{'M' if f.get('mandatory') else 'O'}, {f.get('dLength','?')}): "
                       f"{f.get('description','')}")

    if proposals.get("error_codes"):
        lines.append("\n### Error Codes (use these exact codes; never HTTP codes)")
        for ec in proposals["error_codes"]:
            lines.append(
                f"- {ec.get('code','?')} [{ec.get('td_bd','?')}] "
                f"({ec.get('entity','?')}): {ec.get('description','')}"
            )

    if proposals.get("auth_method"):
        lines.append(f"\n**Auth method:** {proposals['auth_method']}")
    if proposals.get("transaction_limit"):
        lines.append(f"**Transaction limit:** {proposals['transaction_limit']}")

    if proposals.get("flow_sequence"):
        lines.append("\n### Flow Sequence")
        for step in proposals["flow_sequence"]:
            lines.append(f"- {step}")

    if proposals.get("functional_requirements"):
        lines.append("\n### Functional Requirements (use these verbatim)")
        for fr in proposals["functional_requirements"]:
            lines.append(f"- {fr}")

    if proposals.get("participant_obligations"):
        lines.append("\n### Participant Obligations")
        for actor, obligations in proposals["participant_obligations"].items():
            if obligations:
                lines.append(f"**{actor}:**")
                for o in obligations:
                    lines.append(f"  - {o}")

    return "\n".join(lines) if lines else "(proposals extracted but empty)"
