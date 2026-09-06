# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Negotiation Agent — drafts responses to partner queries using RAG context.

When a partner asks an implementation question, this agent:
  1. Retrieves relevant context from BRD, Tech Spec, Product Kit (via RAG)
  2. Drafts a response using Claude
  3. Returns the draft for PO review and approval
"""
import logging
from sqlalchemy.orm import Session

from app.core.llm import call_llm
from app.rag.retrieval import retrieve, build_context
from app.models.document_chunk import DocCategory
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior product specialist at the Authority
responding to implementation queries from ecosystem partners (banks, PSPs, TPAPs) about a network feature change.

You have access to the following context about the feature:
- Business Requirements Document (BRD)
- Technical Specification
- Product Kit documents
- Relevant knowledge base articles

## Instructions

1. Answer the partner's question clearly and accurately based on the provided context.
2. Be specific — include exact field names, API endpoints, schema elements, timelines when available.
3. If the answer is not fully covered in the context, say so explicitly and suggest what additional clarity the Authority should provide.
4. Use a professional, collaborative tone — these are ecosystem partners implementing the Authority's specification.
5. Structure your response with clear sections if the question has multiple parts.
6. Reference specific document sections (e.g., "As per BRD Section 4.2...") when possible.

Keep responses concise but complete. Partners need actionable implementation guidance.

""" + ANTI_INJECTION_CLAUSE


async def draft_negotiation_response(
    query: str,
    change_title: str,
    brd_content: str,
    tech_spec_content: str,
    enhanced_prompt: str,
    thread_history: list[dict],
    db: Session,
) -> str:
    """
    Draft an AI response to a partner's implementation query.

    Args:
        query:             The partner's question.
        change_title:      Title of the change request.
        brd_content:       BRD text.
        tech_spec_content: Tech Spec text.
        enhanced_prompt:   The enriched feature prompt.
        thread_history:    Prior Q&A in this thread [{role, content}].
        db:                SQLAlchemy session for RAG retrieval.

    Returns:
        The AI-drafted response text.
    """
    # RAG retrieval — search across product knowledge for relevant context
    rag_chunks = retrieve(query, db, top_k=6, min_score=0.15)
    rag_context = build_context(rag_chunks, max_tokens=3000) if rag_chunks else ""

    # Build context block
    context = f"""## Feature: {wrap_untrusted(change_title, "CHANGE_TITLE")}

### Enriched Prompt
{wrap_untrusted(enhanced_prompt or '(not available)', "ENRICHED_PROMPT")}

### BRD (Summary)
{wrap_untrusted(brd_content[:3000] if brd_content else '(not available)', "BRD_CONTENT")}

### Tech Spec (Summary)
{wrap_untrusted(tech_spec_content[:3000] if tech_spec_content else '(not available)', "TECH_SPEC_CONTENT")}
"""

    if rag_context:
        context += f"""
### Additional Knowledge Base Context
{rag_context}
"""

    # Build messages
    messages = [{"role": "user", "content": f"Feature context:\n{context}"}]

    # Add thread history
    for msg in thread_history:
        if msg["role"] == "partner":
            messages.append({"role": "user", "content": wrap_untrusted(msg['content'], "PARTNER_HISTORY")})
        elif msg["role"] == "po_approved":
            messages.append({"role": "assistant", "content": msg["content"]})

    # Add the current query
    messages.append({"role": "user", "content": wrap_untrusted(query, "PARTNER_QUERY")})

    logger.info("NegotiationAgent — drafting response, query_len=%d, history=%d", len(query), len(thread_history))

    draft = await call_llm(system=SYSTEM_PROMPT, messages=messages, max_tokens=2000, agent_name="negotiation")
    logger.info("NegotiationAgent — draft complete, len=%d", len(draft))
    return draft
