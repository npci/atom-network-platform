# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deep Researcher agent.

Given an enriched prompt, performs a three-part research:
  1. Market Research — industry trends, use-case landscape
  2. Product Knowledge — relevant context from the RAG knowledge base
  3. RBI Compliance — applicable guidelines from RAG + general compliance analysis

Streams the combined report token-by-token via an async generator.
Also handles the feedback-and-enrich loop (each feedback round is a new turn).
"""
import logging
from collections.abc import AsyncGenerator

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.domain.registry import prompt_block
from app.core.llm import stream_llm
from app.core.prompts import render_prompt
from app.rag.retrieval import retrieve, build_context, format_sources
from app.rag.hybrid_search import build_context_with_taxonomy
from app.agents.taxonomy import classify as classify_taxonomy
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE, safe_format

logger = logging.getLogger(__name__)


# Slice 9 — citation rules appended to the system prompt when enforcement flag is on.
# Slice 9b/9c — _CITATION_RULES extracted to `app.agents.citations.GENERATE_RULES`
# so BRD/TSD/Canvas can share the citation surface (PRESERVE_RULES variant).
from app.agents.citations import GENERATE_RULES as _CITATION_RULES  # noqa: E402,F401

# Domain vocabulary supplied by the active domain pack — see docs/genericization sweep.
SYSTEM_PROMPT_TEMPLATE = render_prompt(
    "agents/deep_researcher/system_prompt.md",
    PLATFORM_NAME=prompt_block("platform_name", "this change-management platform"),
    DOMAIN_NAME=prompt_block("domain_name", "this domain"),
    MARKET_COMPARABLES=prompt_block("market_comparables", "comparable products in this space"),
    MARKET_CONTEXT=prompt_block("market_context", "this market"),
    ECOSYSTEM_ACTORS=prompt_block("ecosystem_actors", "the platform's ecosystem"),
    AUTHORITY=prompt_block("authority", "the platform operator"),
    PRODUCT_OPERATING_EXTRA=prompt_block("product_operating_extra", ""),
    REGULATORY_BODY=prompt_block("regulatory_body", "the applicable regulator"),
    REFERENCE_KIND=prompt_block("reference_kind", "published guidance"),
    ANTI_INJECTION_CLAUSE=ANTI_INJECTION_CLAUSE,
)


async def stream_research_turn(
    enriched_prompt: str,
    conversation_history: list[dict],
    new_user_message: str,
    db: Session,
    top_k: int = 8,
) -> AsyncGenerator[str, None]:
    """
    Stream a research report (or a feedback-revised version of it).

    On the first call, `new_user_message` is the enriched prompt.
    On subsequent calls (feedback rounds), it is the user's feedback text.

    Yields:
        str — text chunks of the assistant's response
    """
    # Classify the feature into UPI taxonomy; drives analogue_queries in retrieval
    search_query = f"{enriched_prompt}\n{new_user_message}"
    classification = await classify_taxonomy(enriched_prompt or new_user_message)

    # 3-stage hybrid retrieval: analogue queries + semantic search (+ style later)
    chunks, rag_context = build_context_with_taxonomy(
        feature_description=search_query,
        classification=classification,
        db=db,
        per_query_top_k=3,
        overall_top_k=top_k,
    )
    if not chunks:
        # Fallback to plain hybrid retrieve if the taxonomy run came back empty
        chunks = retrieve(search_query, db, top_k=top_k)
        rag_context = build_context(chunks, max_tokens=3000) if chunks else "No relevant documents found in knowledge base."

    # Guaranteed grounding on the existing API-design source of truth. Broad
    # retrieval above can bury api_design_knowledge in a large corpus, so we pull
    # it scoped and prepend (deduped) — it is always present and ranked first,
    # in addition to (not instead of) the broad research context.
    from app.models.document_chunk import DocCategory
    api_design_chunks = retrieve(
        search_query, db, top_k=6, categories=[DocCategory.API_DESIGN_KNOWLEDGE],
    )
    if api_design_chunks:
        _seen = {c.get("id") for c in chunks}
        chunks = [c for c in api_design_chunks if c.get("id") not in _seen] + chunks
        rag_context = build_context(chunks, max_tokens=4000)

    # Slice 9 — when citation enforcement is on, replace the context with a
    # numbered-source formatted version and append citation rules to the prompt.
    citation_suffix = ""
    if settings.use_citation_enforcement and chunks:
        rag_context, _sources = format_sources(chunks)
        citation_suffix = _CITATION_RULES

    system_prompt = SYSTEM_PROMPT_TEMPLATE + citation_suffix

    rag_block = (
        f"\n\nKNOWLEDGE BASE CONTEXT (retrieved from internal documents):\n"
        f"{wrap_untrusted(rag_context, 'RAG_CONTEXT')}\n"
        if rag_context.strip()
        else ""
    )
    user_content = f"{rag_block}\n{new_user_message}" if rag_block else new_user_message
    messages = conversation_history + [{"role": "user", "content": user_content}]

    logger.info(
        "DeepResearcher — streaming turn, history_len=%d, rag_chunks=%d, taxonomy=%s (conf=%.2f)",
        len(messages), len(chunks),
        classification.get("primary"), classification.get("confidence", 0.0),
    )

    # max_tokens bumped 6144 → 32000 (2026-05-04, Layer-3 of truncation fix).
    # User reported visible truncation in research output. Production runs
    # produced response_chars≈35,000 which is ~9k tokens — pushed past the
    # 6144 cap, but AiNxt's SSE shim was masking the stop_reason marker so
    # our Layer-1 detection never fired. Claude Sonnet 4.6 supports 64K
    # output, so 32K leaves comfortable headroom for thorough reports.
    #
    # Deep Research model override (2026-05-06): this stage may use a different
    # provider/model from the rest of the platform. Empty settings = inherit
    # default (Claude Sonnet 4.6); explicit values override per-stage.
    dr_provider = (settings.deep_research_provider or "").strip() or None
    dr_model = (settings.deep_research_model or "").strip() or None
    if dr_provider or dr_model:
        logger.info(
            "DeepResearcher — using override provider=%s model=%s",
            dr_provider or "(inherit)", dr_model or "(inherit)",
        )
    async for chunk in stream_llm(
        system=system_prompt, messages=messages, max_tokens=32000,
        agent_name="deep_researcher",
        provider=dr_provider,
        model=dr_model,
    ):
        yield chunk
