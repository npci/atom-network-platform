# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""One-shot enrichment generator (Slice 10).

Parallel path to the production multi-turn `prompt_enhancer`. Given a raw PO
prompt, makes a single LLM call and returns a structured `EnrichedStory`
dict. Used by the eval harness (`run_enrichment_eval.py`); no production
caller yet — adding one is sub-slice 10a.

Fail-open: any LLM error or unparseable response returns an empty dict.
The eval validator treats that as a full failure on schema_valid /
field_completeness, which is the correct behaviour.
"""
from __future__ import annotations

import logging

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.agents.enrichment_schema import ALL_FIELDS

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 2500


_SYSTEM_PROMPT = """You are the Requirement Enrichment Agent for the network
Change Management Platform. Given a Product Owner's one-line idea, produce a
structured user story that a downstream design agent can act on.

Return ONLY a JSON object with EXACTLY these fields:

{
  "title":                 "Short actionable title (≤12 words)",
  "as_a":                  "The actor or system requesting the change",
  "i_want":                "The desired capability",
  "so_that":               "The business or user outcome",
  "context_summary":       "2-4 sentences summarising what exists today and where this change fits. Reference current the network capabilities / components when relevant.",
  "acceptance_criteria":   ["Testable outcome 1", "Testable outcome 2", ...],
  "non_functional":        ["NFR 1 (latency/availability/compatibility)", ...],
  "open_questions":        ["Question for the PO to clarify", ...],
  "affected_components":   [{"repo": "repo-name", "files": ["path/to/File.java", ...]}, ...],
  "citations":             ["source_uri if any specific guideline cited; empty list is OK", ...]
}

Rules:
- Return ONLY the JSON. No markdown fences, no commentary.
- Be specific to the network domain — avoid generic fintech boilerplate.
- 3-7 acceptance_criteria; each must be testable.
- 2-5 non_functional items covering latency, backward compatibility, observability.
- Include open_questions when the PO prompt is ambiguous; empty list is also OK.
- affected_components may be [] if unknown; prefer empty over guessing.

""" + ANTI_INJECTION_CLAUSE


async def generate_enriched_story(
    po_prompt: str,
    *,
    rag_context: str = "",
) -> dict:
    """Produce an EnrichedStory dict from a one-line PO prompt.

    Args:
        po_prompt: The PO's idea as free text (one sentence or a short paragraph).
        rag_context: Optional pre-built RAG context (string) to ground the LLM.
                     The eval harness passes empty; production callers (future)
                     would pass retrieved chunks here.

    Returns:
        A dict matching the EnrichedStory schema. Empty dict on failure.
    """
    if not po_prompt or not po_prompt.strip():
        return {}

    user_payload = wrap_untrusted(po_prompt.strip(), "PO_PROMPT")
    if rag_context:
        user_payload += f"\n\nKnowledge base context:\n{wrap_untrusted(rag_context, 'RAG_CONTEXT')}"

    try:
        from app.core.llm import call_llm
        from app.core.json_recovery import parse_llm_json

        raw = await call_llm(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_payload}],
            max_tokens=MAX_OUTPUT_TOKENS,
            agent_name="enrichment",
        )
    except Exception as e:
        logger.warning("enrichment.generate: LLM call failed: %s", e)
        return {}

    parsed = await parse_llm_json(raw, fallback=None, llm_self_correct=False)
    if not isinstance(parsed, dict):
        logger.warning("enrichment.generate: LLM returned non-dict, discarding")
        return {}

    # Keep only expected fields (drop junk the model may have added).
    return {k: v for k, v in parsed.items() if k in ALL_FIELDS}
