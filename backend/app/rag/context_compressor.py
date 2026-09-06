# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-chunk LLM context compressor (Slice 8).

Given a query and a list of retrieved chunks, reduce each chunk's `content`
to only those sentences directly relevant to the query. Plan §6.4 target:
3–6× token reduction with minimal recall loss.

Design:
  - **Per-chunk LLM call**, parallelized via `asyncio.gather` so N chunks
    ≈ 1 round-trip latency.
  - **Strict prompt** — the LLM returns a JSON array of sentence indices to
    KEEP. No self-rewriting, no summarization. Deterministic subset.
  - **Fail-open at every layer** — empty query, LLM exception, unparseable
    JSON, empty kept-indices, or all-out-of-range indices → original chunk
    content preserved. The compressor must never silently drop content on
    infra failure.
  - **Skip short chunks** — below `MIN_CHARS_TO_COMPRESS` the per-chunk LLM
    call wouldn't pay back its own latency. Those pass through unchanged.

Notes for follow-up:
  - LLMLingua-2 (plan §6.4 reference) would replace this LLM filter with a
    local sentence-relevance scorer. Faster, cheaper, and less brittle.
    Deferred; today's volume makes this acceptable.
  - Sentence splitting is naive (regex on `. ` followed by uppercase). Real
    NLP tokenization (spaCy / nltk) is overkill for Slice 8; we revisit if
    eval shows split mistakes hurt compression quality.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

MIN_CHARS_TO_COMPRESS = 200           # don't bother compressing short chunks
SENTENCE_BUDGET_TOKENS = 150          # cap on LLM response — just an index array
COMPRESSION_TIMEOUT_SECS = 15         # per-chunk LLM call hard timeout

# Naive sentence splitter — break on `.!?` followed by whitespace + capital letter.
# Keeps the terminator with the sentence. Good enough for prose; breaks on
# abbreviations ("Dr.", "etc.") occasionally — acceptable at this slice.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

_SYSTEM_PROMPT = load_prompt("rag/context_compressor/system_prompt.md")


def _split_sentences(content: str) -> list[str]:
    """Rough sentence tokenization. Returns non-empty trimmed sentences."""
    if not content:
        return []
    parts = _SENTENCE_SPLIT_RE.split(content.strip())
    return [p.strip() for p in parts if p.strip()]


async def _compress_one(query: str, content: str) -> str:
    """Compress a single chunk's content. Returns original on any failure."""
    sentences = _split_sentences(content)
    if len(sentences) <= 2:
        return content  # nothing meaningful to trim

    numbered = "\n".join(f"[{i}] {s}" for i, s in enumerate(sentences))
    user_msg = (
        f"Query: {query}\n\nSentences:\n{numbered}\n\n"
        f"Return the JSON array of indices to keep."
    )

    try:
        # Lazy imports so this module is importable without Anthropic SDK at rest.
        from app.core.llm import call_llm
        from app.core.llm_router import pick_model_for_agent
        from app.core.json_recovery import parse_llm_json

        # Slice 27a — context_compressor is Purpose.UTILITY (per-chunk
        # sentence-relevance filter, called once per retrieved chunk).
        raw = await asyncio.wait_for(
            call_llm(
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=SENTENCE_BUDGET_TOKENS,
                model=pick_model_for_agent("context_compressor"),
                agent_name="context_compressor",
            ),
            timeout=COMPRESSION_TIMEOUT_SECS,
        )
    except Exception as e:
        logger.warning("compress: LLM call failed (len=%d) — keeping original: %s",
                       len(content), e)
        return content

    parsed = await parse_llm_json(raw, fallback=None, llm_self_correct=False)
    if not isinstance(parsed, list):
        logger.debug("compress: LLM returned non-list, keeping original")
        return content

    kept_indices = sorted({
        i for i in parsed
        if isinstance(i, int) and 0 <= i < len(sentences)
    })
    if not kept_indices:
        # LLM dropped everything — suspicious; keep original rather than lose content.
        logger.debug("compress: zero indices kept, reverting to original")
        return content

    kept_sentences = [sentences[i] for i in kept_indices]
    return " ".join(kept_sentences)


async def compress_chunks(
    query: str,
    chunks: list[dict],
    *,
    min_chars_to_compress: int = MIN_CHARS_TO_COMPRESS,
) -> list[dict]:
    """Compress all chunks in parallel; preserve order.

    Returns a list of NEW dicts (doesn't mutate inputs). Chunks below
    `min_chars_to_compress` pass through unchanged.
    """
    if not query or not query.strip():
        return list(chunks)
    if not chunks:
        return []

    async def _one(chunk: dict) -> dict:
        content = chunk.get("content") or ""
        if len(content) < min_chars_to_compress:
            return dict(chunk)
        compressed = await _compress_one(query, content)
        return {**chunk, "content": compressed}

    return await asyncio.gather(*[_one(c) for c in chunks])


def compress_chunks_sync(
    query: str,
    chunks: list[dict],
    *,
    min_chars_to_compress: int = MIN_CHARS_TO_COMPRESS,
) -> list[dict]:
    """Sync wrapper that dispatches on `settings.context_compression_mode`.

    Phase 2.3 — three modes:
      - "off"      → pass-through (no compression).
      - "overlap"  → deterministic line-overlap scorer; keeps the top-N
                     query-relevant lines per chunk. Pure-Python, no LLM
                     call. Default for new installs.
      - "llm"      → original Slice 8 path (per-chunk LLM filter).

    Fail-soft at every step. Never raises.
    """
    if not chunks:
        return []

    try:
        from app.core.config import settings
        mode = (getattr(settings, "context_compression_mode", "overlap") or "overlap").lower()
    except Exception:
        mode = "overlap"

    if mode == "off":
        return list(chunks)

    if mode == "overlap":
        try:
            return compress_chunks_overlap(query, chunks)
        except Exception as e:
            logger.warning(
                "compress_chunks_overlap failed (%s) — returning original chunks", e,
            )
            return list(chunks)

    # mode == "llm" (or anything unrecognised) → legacy Slice 8 path.
    try:
        return asyncio.run(compress_chunks(
            query, chunks, min_chars_to_compress=min_chars_to_compress,
        ))
    except RuntimeError as e:
        # e.g. called from within an already-running loop
        logger.warning("compress_chunks_sync called from running loop: %s", e)
        return list(chunks)
    except Exception as e:
        logger.warning("compress_chunks_sync unexpected failure: %s", e)
        return list(chunks)


# ── Phase 2.3 — Deterministic line-overlap compression ───────────────────────

def _line_overlap_score(query_tokens: set[str], line: str) -> int:
    """Count of query tokens that appear in `line` (same tokeniser as
    bm25_search so behaviour matches the hybrid-overlap filter).
    Lazy import keeps this module loadable without sqlalchemy in tests."""
    try:
        from app.rag.bm25_search import tokenize as _tokenize
        line_tokens = set(_tokenize(line or ""))
    except Exception:
        line_tokens = set((line or "").lower().split())
    if not line_tokens:
        return 0
    return len(query_tokens & line_tokens)


def _compress_one_overlap(query: str, content: str, *, max_lines: int) -> str:
    """Keep the top-scoring lines per chunk while preserving source order.

    - Chunks with `len(lines) <= max_lines` pass through untouched.
    - If no line has any token overlap, original content is returned
      (fail-open — better to surface a marginally relevant chunk than nothing).
    - For positive-score lines, ±1 neighbours are kept too so multi-line
      code blocks aren't stranded.
    - The keep-set fills with zero-score lines (in source order, courtesy of
      Python's stable sort) until the `max_lines` budget is reached.
    """
    if not content:
        return content
    lines = content.splitlines()
    if len(lines) <= max_lines:
        return content

    try:
        from app.rag.bm25_search import tokenize as _tokenize
        q_tokens = set(_tokenize(query or ""))
    except Exception:
        q_tokens = set((query or "").lower().split())
    if not q_tokens:
        return content

    scored: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        scored.append((i, _line_overlap_score(q_tokens, line)))

    if all(s == 0 for _, s in scored):
        return content

    # Highest score first; Python's sort is stable so ties preserve source order.
    scored.sort(key=lambda t: t[1], reverse=True)
    keep: set[int] = set()
    for idx, score in scored:
        if score == 0 and len(keep) >= max_lines:
            break
        keep.add(idx)
        if score > 0:
            if idx > 0:
                keep.add(idx - 1)
            if idx + 1 < len(lines):
                keep.add(idx + 1)
        if len(keep) >= max_lines:
            break

    return "\n".join(lines[i] for i in sorted(keep))


def compress_chunks_overlap(query: str, chunks: list[dict]) -> list[dict]:
    """Phase 2.3 — pure-Python compression. Same return contract as
    `compress_chunks` (list of new dicts, content possibly reduced).
    Skips chunks below `MIN_CHARS_TO_COMPRESS` chars."""
    if not query or not query.strip() or not chunks:
        return list(chunks)
    try:
        from app.core.config import settings
        max_lines = int(getattr(settings, "context_compression_max_lines", 25) or 25)
    except Exception:
        max_lines = 25
    out: list[dict] = []
    for chunk in chunks:
        content = chunk.get("content") or ""
        if len(content) < MIN_CHARS_TO_COMPRESS:
            out.append(dict(chunk))
            continue
        out.append({**chunk, "content": _compress_one_overlap(query, content, max_lines=max_lines)})
    return out
