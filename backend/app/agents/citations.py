# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared citation-enforcement helpers (Slice 9b/9c).

Slice 9 introduced citation enforcement on the deep_researcher agent
only. 9b/9c replicate the pattern to BRD / Tech Spec / Canvas — three
agents that consume a *cited* upstream document (research_report or
canvas_content) and would otherwise lose those citations as they
re-author the content into their own structured output.

Two flavours of rule suffix:

  - GENERATE_RULES — for agents that retrieve their own RAG chunks AND
    are responsible for emitting NEW `[N]` markers for every factual
    claim they author (deep_researcher). Pairs with
    `app.rag.retrieval.format_sources` which numbers chunks `[1]..[N]`.

  - PRESERVE_RULES — for agents that consume an already-cited upstream
    document (BRD/TSD/Canvas read deep_researcher's report) and must
    NOT strip the `[N]` markers when re-authoring. Cheap fail-safe
    against citation loss across the multi-agent pipeline.

Both are gated by `settings.use_citation_enforcement` — flag-off =
empty string suffix = zero behaviour change.
"""
from __future__ import annotations
from app.core.prompts import render_prompt

from app.core.config import settings
from app.core.domain.registry import prompt_block


# ──────────────────────────────────────────────────────────────────────────────
# Rule blocks — domain vocabulary supplied by the active domain pack.
# ──────────────────────────────────────────────────────────────────────────────

GENERATE_RULES = render_prompt(
    "agents/citations/generate_rules.md",
    NUMBERED_CITATION_EXAMPLE=prompt_block(
        "numbered_citation_example", "The stated limit is 100 units for most cases [3]."),
    REGULATORY_CLAIM_LABEL=prompt_block("regulatory_claim_label", "regulatory guideline"),
)


PRESERVE_RULES = render_prompt(
    "agents/citations/preserve_rules.md",
    CORPUS_LABEL=prompt_block("corpus_label", "source"),
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def generate_suffix() -> str:
    """Return GENERATE_RULES when flag on, empty string when off.

    Used by deep_researcher (and any future RAG-grounded generator
    that emits its own [N] markers).
    """
    return GENERATE_RULES if settings.use_citation_enforcement else ""


def preserve_suffix() -> str:
    """Return PRESERVE_RULES when flag on, empty string when off.

    Used by BRD / Tech Spec / Canvas agents that consume an
    already-cited upstream document and must propagate citations.
    """
    return PRESERVE_RULES if settings.use_citation_enforcement else ""


def upstream_has_citations(text: str | None) -> bool:
    """Cheap probe — returns True if the text contains at least one
    `[N]`-style citation marker. Caller can use this to skip the
    PRESERVE suffix when the upstream document doesn't carry citations
    (saving prompt tokens for the no-op case).
    """
    if not text:
        return False
    import re
    return bool(re.search(r"\[\d+(?:\s*[,\]]\s*\d+)*\]", text))
