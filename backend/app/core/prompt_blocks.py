# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 6.1 — Centralised prompt blocks.

Single source of truth for the long, stable prose blocks that several
agents share. Centralising them is the prerequisite for Phase 6.2
(Anthropic prompt caching): a cached segment has to be the SAME bytes
across requests for the cache to hit. Inline copies in each agent file
drifted over time — every fix to PROD_OUTPUT_RULES had to be made in
4-5 places. This module collapses that.

Usage:
    from app.core.prompt_blocks import (
        PROD_OUTPUT_RULES, CITATION_RULES, MARKDOWN_RULES,
        assemble_system_prompt, segments_for_anthropic_cache,
    )

    system = assemble_system_prompt([
        AGENT_ROLE_PREFACE,
        CITATION_RULES,
        MARKDOWN_RULES,
        PROD_OUTPUT_RULES,
        NETWORK_HARD_RULES,
        BRD_BLUEPRINT,
    ])

When Anthropic prompt caching is on, the agent layer can pass the same
parts list to `segments_for_anthropic_cache` which marks the long stable
blocks with `cache_control={"type": "ephemeral"}`.

NOTE: these strings are intentionally verbatim copies of what each agent
used to inline. Keep them stable — every edit invalidates the prompt
cache.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

from typing import Iterable


# ── Domain vocabulary ─────────────────────────────────────────────────────────
#
# The blocks below are 90% generic instruction and 10% domain nouns. Rather than
# fork the whole block per domain, the nouns are substitution points and the
# defaults reproduce today's text BYTE-FOR-BYTE (test_prompt_blocks.py pins
# this against a frozen copy — the module docstring above explains why: a single
# changed byte invalidates the Anthropic prompt cache for every request).
#
# A domain pack supplies its own mapping. The API-deprecation pack, for example,
# has no regulator to cite and no circulars to reference, so it overrides
# `evidence_sources` and `reference_kind` rather than inheriting words that
# describe an ecosystem it is not part of.
#
# MARKDOWN_RULES has no entry here on purpose: it is 100% generic and moves to
# core untouched (docs/genericization/03-domain-coupling-audit.md).

DEFAULT_TERMS: dict[str, str] = {
    # The publishing authority whose conventions the document follows.
    "authority": "Authority",
    # Corpora the model may cite as evidence.
    "evidence_sources": "Authority / Regulator",
    # Heading of the retrieved-evidence block in the user message. MUST match
    # what the RAG layer actually emits — see app/docgen/rag_bridge.py:97.
    "evidence_heading": "Retrieved authority corpus evidence",
    # A worked example of a cited claim, in this domain's idiom.
    "citation_example": "PSPs must revoke within 24h [S2].",
    # The domain's own name for a published authority reference.
    "reference_kind": "circular",
    # Claim types that must carry a citation when corpus-supported.
    "claim_kinds": "regulatory obligation, error-code, API name, or dispute-SLA claim",
    # How the document should describe its own register.
    "document_register": "regulated authority document",
}


def _render(template: str, terms: dict[str, str] | None = None) -> str:
    """Substitute domain nouns. Unknown keys in `terms` are ignored; missing
    keys fall back to the default, so a pack may override one noun without
    restating all of them."""
    return template.format(**{**DEFAULT_TERMS, **(terms or {})})


def active_pack_terms() -> dict[str, str]:
    """The domain-noun mapping supplied by the ACTIVE pack.

    Each DEFAULT_TERMS key doubles as a prompt-block name (`authority`,
    `evidence_sources`, …); a pack that supplies the block overrides the noun,
    and one that omits it inherits the default — per-noun, not all-or-nothing.
    The UPI pack's blocks are byte-identical to DEFAULT_TERMS, so the default
    deployment renders exactly the bytes it always has (prompt-cache safe).
    """
    from app.core.domain.registry import prompt_block

    return {k: prompt_block(k, v) for k, v in DEFAULT_TERMS.items()}


# ── Stable rule blocks ────────────────────────────────────────────────────────

_PROD_OUTPUT_RULES_T = load_prompt("core/prompt_blocks/prod_output_rules_t.md")

_CITATION_RULES_T = load_prompt("core/prompt_blocks/citation_rules_t.md")


def prod_output_rules(terms: dict[str, str] | None = None) -> str:
    return _render(_PROD_OUTPUT_RULES_T, terms)


def citation_rules(terms: dict[str, str] | None = None) -> str:
    return _render(_CITATION_RULES_T, terms)


# Module-level constants preserved, now rendered with the active pack's nouns
# (import-time is the established pattern — agents bake these into module-level
# system prompts). Under the default UPI pack the pack blocks are byte-identical
# to DEFAULT_TERMS, so these are the exact bytes they always were and the
# Anthropic prompt cache keeps hitting.
PROD_OUTPUT_RULES = prod_output_rules(active_pack_terms())
CITATION_RULES = citation_rules(active_pack_terms())

MARKDOWN_RULES = load_prompt("core/prompt_blocks/markdown_rules.md")


# ── Assembly helpers ─────────────────────────────────────────────────────────

_BLOCK_SEPARATOR = "\n\n---\n\n"


def assemble_system_prompt(parts: Iterable[str]) -> str:
    """Join non-empty parts with the canonical separator.

    Used by agents whose Anthropic-cache wiring isn't on yet, or by
    non-Claude providers where the system prompt is a single string.
    """
    blocks = [p.strip() for p in parts if p and p.strip()]
    return _BLOCK_SEPARATOR.join(blocks)


def segments_for_anthropic_cache(
    parts: list[tuple[str, bool]],
) -> list[dict]:
    """Phase 6.2 — produce Anthropic system-prompt segments with selective
    `cache_control` markers.

    Each element is `(text, cacheable)`. The function emits one
    `{"type": "text", "text": ...}` segment per non-empty input; segments
    flagged `cacheable=True` get `cache_control={"type": "ephemeral"}`
    attached. Anthropic permits up to 4 cache_control markers per request;
    callers are responsible for ordering and counting (typically: agent
    role preface, big-doc-rules block, blueprint schema, RAG context).

    Empty / whitespace segments are dropped so an unused optional block
    doesn't burn a cache slot.
    """
    out: list[dict] = []
    cache_slots_used = 0
    for text, cacheable in parts:
        if not text or not text.strip():
            continue
        segment: dict = {"type": "text", "text": text}
        if cacheable and cache_slots_used < 4:
            segment["cache_control"] = {"type": "ephemeral"}
            cache_slots_used += 1
        out.append(segment)
    return out
