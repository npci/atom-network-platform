# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-agent LLM routing (Slice 27).

Plan §3.2 — separate model selection from agent code so operators can
route lightweight workloads (taxonomy classifier, query enrichment,
doc-code link confidence scoring, code summarizer, context compressor)
to cheaper/faster models, reserving the frontier model for the hard
reasoning paths (BRD, Tech Spec, code-change, deep researcher).

Design choices:
  - Three purposes is enough for v0. Splitting further (SUMMARISATION
    vs CLASSIFICATION vs SCORING vs RANKING) would be premature — every
    agent currently maps cleanly to one of these three.
  - Operators set model ids directly (e.g. "claude-haiku-4-5-20251001",
    "gpt-4o-mini") in env vars. The router doesn't validate against
    a hardcoded list — wrong model ids surface as provider errors at
    call time, with no extra abstraction layer to maintain as new
    models ship.
  - `pick_model_for(purpose)` returns None when routing is off OR when
    the operator left that purpose's config blank. None means
    "callers fall back to the global `llm.get_model()` default" —
    flag-off equals exactly current behaviour.
  - `Purpose` is a string Enum so the values can be passed across
    serialisation boundaries (Celery args, log lines, telemetry
    fields) without type juggling.
"""
from __future__ import annotations

import enum
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


class Purpose(str, enum.Enum):
    """Coarse routing buckets. Members documented at the assignment
    table in `agent_purposes()`."""
    REASONING = "reasoning"   # frontier — BRD/TSD/code_change/deep_researcher
    ROUTING   = "routing"     # cheap+fast — taxonomy, query enrichment, link confidence
    UTILITY   = "utility"     # cheap — summarisation, compression, ambiguity detection


# Default mapping from agent / module name → Purpose. Used by
# `purpose_for_agent()` so callers don't have to remember which bucket
# their agent sits in. Adding a new agent? Add the mapping here.
_AGENT_PURPOSE: dict[str, Purpose] = {
    # Reasoning-heavy
    "brd":              Purpose.REASONING,
    "tech_spec":        Purpose.REASONING,
    "code_change":      Purpose.REASONING,
    "deep_researcher":  Purpose.REASONING,
    "canvas":           Purpose.REASONING,
    "xsd":              Purpose.REASONING,
    "product_kit":      Purpose.REASONING,
    "negotiation":      Purpose.REASONING,
    "version_change_summary": Purpose.REASONING,
    "escalation_advisor": Purpose.REASONING,
    "revision_planner": Purpose.REASONING,
    "cert_triage":      Purpose.REASONING,
    # Phase B UAT triage: root-causes build + test-script logs (code vs test vs
    # env) with quoted evidence — genuine log reasoning, not classification.
    "uat_triage":       Purpose.REASONING,
    "cert_testing":     Purpose.REASONING,
    "decline_designer": Purpose.REASONING,
    "xml_template_generator": Purpose.REASONING,
    "code_review":      Purpose.REASONING,
    "is_review":        Purpose.REASONING,
    "code_planner":     Purpose.REASONING,
    "change_walkthrough": Purpose.REASONING,
    "delta_grounding":  Purpose.REASONING,   # code-back reconciliation plan amendments
    # Stuck-run helper: a quick classification of an error + a small set of recovery options.
    # Reasoning, but the input is tiny — cheap call. Validator is the same path.
    "stuck_helper":     Purpose.ROUTING,
    "stuck_helper_validator": Purpose.ROUTING,
    # Doc↔plan consistency auditor (BRD/TSD vs ratified plan). Reasoning — it must understand the
    # technical surface of a long document, not just classify.
    "doc_consistency":  Purpose.REASONING,
    # Surgical-edit patch planner (NL edit instruction + block index → minimal patch ops).
    # Reasoning — it must understand the instruction against document content, not just classify.
    "docgen_patch_planner": Purpose.REASONING,
    # Plan-fidelity gate (did the code DELIVER the plan + is the hard part real, not faked).
    # Reasoning — it reasons about a diff against intended behaviours, not a classification.
    "plan_fidelity":    Purpose.REASONING,
    # SECURITY NOTE: adversarial review shares Purpose.REASONING (and therefore
    # the same model) as code_change by default. This means the same model grades
    # its own output — a structural weakness for self-review. Mitigation:
    # `settings.agentic_reviewer_model` routes the agentic reviewer (run_review)
    # to a dedicated Anthropic model; set it so the reviewer is never the same
    # LLM as the author.
    "adversarial":      Purpose.REASONING,
    # Governance stages (EA → InfoSec pre-build gates): rule-by-rule review over the
    # whole diff + a surgical fixer — frontier-tier. The stage reviewer additionally
    # routes through settings.agentic_reviewer_model (different eyes), like run_review.
    "gov_ea_review":    Purpose.REASONING,
    "gov_is_review":    Purpose.REASONING,
    "gov_fix":          Purpose.REASONING,
    "self_correction":  Purpose.REASONING,
    # Stuck-loop strategist (P2): one structural recommendation after repeated failed fix
    # rounds — small input, but it must genuinely reason about the failure history.
    "strategist":       Purpose.REASONING,
    # Slice 28b additions — also reasoning-heavy in practice (PO-intent
    # rewriting / structured-proposal construction).
    "prompt_enhancer":  Purpose.REASONING,
    "enrichment":       Purpose.REASONING,

    # Routing — fast classification / extraction / scoring decisions
    "taxonomy":         Purpose.ROUTING,
    "query_understanding": Purpose.ROUTING,
    "doc_code_linker":  Purpose.ROUTING,
    "proposals_extractor": Purpose.ROUTING,
    "question_generator":  Purpose.ROUTING,
    "ast_editor":       Purpose.ROUTING,
    "diff_stats_gate":  Purpose.ROUTING,
    "doc_impact":       Purpose.ROUTING,
    "build_triager":    Purpose.ROUTING,

    # Utility — cheap content transformations
    "ambiguity_detector":   Purpose.UTILITY,
    "assumption_handler":   Purpose.UTILITY,
    "context_compressor":   Purpose.UTILITY,
    "code_summarizer":      Purpose.UTILITY,
    "document_validator":   Purpose.UTILITY,
    "citation_validator":   Purpose.UTILITY,
    "adr_checker":          Purpose.UTILITY,
}


def agent_purposes() -> dict[str, Purpose]:
    """Return a copy of the agent → Purpose mapping. Read-only — callers
    that want to override per-agent purposes should use
    `pick_model_for(purpose=...)` directly with their own Purpose value."""
    return dict(_AGENT_PURPOSE)


def purpose_for_agent(agent_name: str) -> Purpose:
    """Resolve `agent_name` → Purpose. Unknown names default to
    REASONING (safest fallback — pay the frontier-model cost rather
    than silently downgrade an unmapped agent to Haiku)."""
    if not agent_name:
        return Purpose.REASONING
    return _AGENT_PURPOSE.get(agent_name.lower(), Purpose.REASONING)


def pick_model_for(purpose: Purpose | str) -> str | None:
    """Look up the configured model id for `purpose`.

    Returns None when:
      - `settings.use_llm_routing` is False (routing disabled globally)
      - The operator left that purpose's `routing_model_<purpose>` blank
      - `purpose` value is unrecognised (defensive — caller passed a
        string outside the Purpose enum)

    None signals "callers should fall back to `llm.get_model()`" — i.e.
    flag-off equals exactly current pre-Slice-27 behaviour.
    """
    if not settings.use_llm_routing:
        return None

    if isinstance(purpose, Purpose):
        key = purpose.value
    else:
        key = (purpose or "").lower()

    if key == Purpose.REASONING.value:
        return settings.routing_model_reasoning or None
    if key == Purpose.ROUTING.value:
        return settings.routing_model_routing or None
    if key == Purpose.UTILITY.value:
        return settings.routing_model_utility or None

    logger.debug("pick_model_for: unknown purpose %r — falling back to default", purpose)
    return None


def pick_model_for_agent(agent_name: str) -> str | None:
    """Convenience: `purpose_for_agent` + `pick_model_for` in one call."""
    return pick_model_for(purpose_for_agent(agent_name))
