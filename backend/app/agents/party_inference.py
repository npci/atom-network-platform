# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Infer which canonical parties are in-scope for a change from its
enriched prompt + deep-research + product-canvas + BRD prose.

Replaces the previous "N independent yes/no clarification questions"
UX. The clarification stage now asks ONE multi-select question with the
inferred parties pre-checked; the PM confirms with one click or edits.

Design:
- Structured-output LLM call via `call_llm_structured` (same helper
  `brd_corrector.propose_brd_corrections` uses). The response schema
  constrains `parties_in_scope` to the active domain pack's canonical
  PARTY keys, so a wrong-shape output can't leak through.
- Fail-open: on any LLM error or missing input, return every canonical
  party as the "safe default" so the PM sees the full menu and never
  gets a blank recommendation. `source="fallback_all_four"` marks that
  path for observability (the literal name predates multi-domain support;
  it now means "fell back to all canonical parties", not literally four).
- Deliberately artifact-length-capped to the same limits `context_cache`
  already uses (research 3k, canvas 2k, BRD 6k) so the prompt stays
  small and the call is cheap.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.domain.contract import cert_vocabulary_of
from app.core.domain.registry import get_active_pack, prompt_block
from app.core.llm import call_llm_structured

logger = logging.getLogger(__name__)

# Canonical PARTY vocabulary — sourced from the active domain pack's cert
# vocabulary. MUST stay in sync with the same source used by `_PARTIES` in
# `app/agents/question_generator.py` and `_FC_PARTIES` in `brd_extractor.py`
# — all three now read `cert_vocabulary_of(pack).parties()` rather than
# carrying their own hardcoded copy (was 3+ separate UPI-only literals).
_pack = get_active_pack()
_CANONICAL_PARTIES: tuple[str, ...] = tuple(
    k for k, _lbl in cert_vocabulary_of(_pack).parties()
)
_PARTY_BULLETS = "\n".join(f"  - {k}: {lbl}" for k, lbl in cert_vocabulary_of(_pack).parties())


class PartyInferenceResult(BaseModel):
    # Not a static Literal (was Literal["PAYER_PSP", ...]): the canonical set
    # is now pack-dependent and resolved at import time. `_SCHEMA`'s `enum`
    # below is the real enforcement (forced tool-call against the active
    # pack's keys); `infer_parties` also filters the parsed result against
    # `_CANONICAL_PARTIES` as a second line of defense.
    parties_in_scope: list[str] = Field(default_factory=list)
    rationale: str = ""
    confidence: Literal["low", "med", "high"] = "med"
    source: Literal["llm", "fallback_all_four", "no_signal"] = "llm"


_SYSTEM = (
    f"You are the party-inference step of {prompt_block('platform_name', 'a change-management platform')}. "
    "Given a change's enriched prompt, deep-research report, "
    "product canvas, and optional approved BRD, decide which of the canonical "
    f"parties are involved in the change:\n"
    f"{_PARTY_BULLETS}\n\n"
    "Rules:\n"
    "  - Return ONLY the parties clearly involved. Do NOT include a "
    "party 'just in case' — a wrong-side involvement leads to test "
    "cases the reviewer will reject.\n"
    f"  - A typical flow involves all {len(_CANONICAL_PARTIES)} canonical parties. A narrower "
    "or non-financial/meta change may involve only a subset — infer the subset from the "
    "artifacts rather than defaulting to all of them.\n"
    "  - The rationale must be ONE short sentence naming the evidence "
    "(e.g. 'BRD FR-01 names one party's path only').\n"
    "  - `confidence`: 'high' if the artifacts explicitly enumerate the "
    "parties; 'med' if you inferred from flow shape; 'low' if the "
    "artifacts are thin or contradictory.\n\n"
    + ANTI_INJECTION_CLAUSE
)


_SCHEMA = {
    "type": "object",
    "properties": {
        "parties_in_scope": {
            "type": "array",
            "items": {"type": "string", "enum": list(_CANONICAL_PARTIES)},
            "uniqueItems": True,
        },
        "rationale": {"type": "string", "maxLength": 400},
        "confidence": {"type": "string", "enum": ["low", "med", "high"]},
    },
    "required": ["parties_in_scope", "rationale", "confidence"],
}


def _fallback_all_four(reason: str) -> PartyInferenceResult:
    return PartyInferenceResult(
        parties_in_scope=list(_CANONICAL_PARTIES),
        rationale=f"Inference fallback — defaulting to all {len(_CANONICAL_PARTIES)} canonical parties. {reason}",
        confidence="low",
        source="fallback_all_four",
    )


async def infer_parties(
    enhanced_prompt: str,
    research_report: str = "",
    canvas_content: str = "",
    brd_content: str = "",
) -> PartyInferenceResult:
    """Infer canonical UPI parties in-scope for a change.

    Fail-open contract: never raises. On any error returns the all-four
    fallback so the clarification stage always has a recommendation to
    render.
    """
    if not (enhanced_prompt or "").strip():
        return PartyInferenceResult(
            parties_in_scope=list(_CANONICAL_PARTIES),
            rationale="No prompt provided — defaulting to all four canonical parties.",
            confidence="low",
            source="no_signal",
        )

    user_parts: list[str] = [
        f"ENRICHED PROMPT:\n{wrap_untrusted(enhanced_prompt[:6000], 'ENRICHED_PROMPT')}",
    ]
    if (research_report or "").strip():
        user_parts.append(
            f"DEEP-RESEARCH REPORT (extract, most-recent):\n"
            f"{wrap_untrusted(research_report[:3000], 'RESEARCH_REPORT')}"
        )
    if (canvas_content or "").strip():
        user_parts.append(
            f"PRODUCT CANVAS (extract):\n"
            f"{wrap_untrusted(canvas_content[:2000], 'PRODUCT_CANVAS')}"
        )
    if (brd_content or "").strip():
        user_parts.append(
            f"APPROVED BRD (extract):\n"
            f"{wrap_untrusted(brd_content[:6000], 'BRD_CONTENT')}"
        )

    user = "\n\n---\n\n".join(user_parts) + (
        "\n\n---\n\nDecide the parties in scope now. Return the JSON per the schema."
    )

    try:
        data = await call_llm_structured(
            _SYSTEM, user,
            schema=_SCHEMA,
            tool_name="record_party_inference",
            agent_name="party_inference",
            max_tokens=600,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("party_inference LLM call failed: %s", exc)
        return _fallback_all_four(f"LLM error: {type(exc).__name__}.")

    if not isinstance(data, dict):
        return _fallback_all_four("LLM returned non-object response.")

    try:
        result = PartyInferenceResult.model_validate({**data, "source": "llm"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("party_inference schema validation failed: %s", exc)
        return _fallback_all_four("LLM response failed schema validation.")

    # Second line of defense now that `parties_in_scope` is `list[str]` rather
    # than a static `Literal` — drop anything outside the active pack's
    # canonical set instead of trusting the schema enforcement alone.
    valid_keys = set(_CANONICAL_PARTIES)
    filtered = [p for p in result.parties_in_scope if p in valid_keys]
    if len(filtered) != len(result.parties_in_scope):
        logger.warning("party_inference: dropped out-of-vocabulary parties %r",
                        [p for p in result.parties_in_scope if p not in valid_keys])
    result.parties_in_scope = filtered

    if not result.parties_in_scope:
        # LLM returned empty (or everything was out-of-vocabulary) — treat as
        # "no signal", show every canonical party so the PM can pick rather
        # than see a blank recommendation.
        return _fallback_all_four(
            "LLM returned no in-vocabulary parties — showing all canonical parties so you can pick."
        )
    return result


__all__ = ["PartyInferenceResult", "infer_parties"]
