# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""RAG Explorer agent — temporary testing utility.

Given a query and a set of retrieved chunks, asks the LLM to:
  • Synthesise a concise answer grounded in the chunks
  • Cite which sources it used (file + section)
  • Flag if the chunks don't actually answer the question (low-coverage)

Used by the Admin → RAG Sandbox UI to verify retrieval quality end-to-end.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.llm import call_llm, stream_llm

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a RAG retrieval evaluator for the Network Change Management Platform.

Given:
  • A user query
  • A list of retrieved chunks from a knowledge base (each tagged with [doc_category | source_file])

Your job is to:
  1. Synthesise a tight, factual answer to the query using ONLY the chunks provided.
  2. Cite the sources you actually used inline as [source_file].
  3. If the chunks do NOT contain enough information to answer the query, say so
     explicitly and explain what's missing — do not invent or pad.
  4. Highlight any contradictions between chunks.

Output format (markdown):

**Answer**
<2-6 sentence synthesis citing [source_file] inline>

**Sources used**
- <source_file> — <one-line note on what it contributed>

**Coverage assessment**
<one of: STRONG | PARTIAL | WEAK> — <one sentence justification>

**Notes** (only if relevant)
- <contradictions, gaps, or caveats>

Keep it concise. No preamble, no apologies, no recap of the question.

""" + ANTI_INJECTION_CLAUSE


def _format_chunks(chunks: list[dict]) -> str:
    """Format retrieved chunks for LLM context."""
    if not chunks:
        return "(no chunks retrieved)"
    parts = []
    for i, c in enumerate(chunks, 1):
        header = f"[{i}] [{c.get('doc_category', '?')} | {c.get('source_file', '?')}]"
        score_bits = []
        if c.get("score") is not None:
            score_bits.append(f"rrf={c['score']:.3f}")
        if c.get("dense_score") is not None:
            score_bits.append(f"dense={c['dense_score']:.3f}")
        if c.get("bm25_score") is not None:
            score_bits.append(f"bm25={c['bm25_score']:.3f}")
        if score_bits:
            header += "  (" + ", ".join(score_bits) + ")"
        body = (c.get("content") or "").strip()
        parts.append(f"{header}\n{body}")
    return "\n\n---\n\n".join(parts)


def _build_user_message(query: str, chunks: list[dict]) -> str:
    return (
        f"User query:\n{wrap_untrusted(query.strip(), 'USER_QUERY')}\n\n"
        f"Retrieved chunks ({len(chunks)}):\n"
        f"{wrap_untrusted(_format_chunks(chunks), 'RETRIEVED_CHUNKS')}"
    )


async def summarise(query: str, chunks: list[dict], max_tokens: int = 1500) -> str:
    """Return a synthesised, source-cited answer for the query+chunks pair."""
    if not query.strip():
        return "**Coverage assessment**\nWEAK — empty query."
    user_msg = _build_user_message(query, chunks)
    return await call_llm(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=max_tokens,
    )


async def stream_summarise(
    query: str,
    chunks: list[dict],
    max_tokens: int = 1500,
) -> AsyncGenerator[str, None]:
    """Token-by-token streaming variant for live UI updates."""
    if not query.strip():
        yield "**Coverage assessment**\nWEAK — empty query."
        return
    user_msg = _build_user_message(query, chunks)
    async for chunk in stream_llm(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=max_tokens,
    ):
        yield chunk
