# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Doc ↔ Code linker (Slice 18).

Post-ingest pass that, for each doc chunk, finds candidate code-symbol
chunks and asks the LLM to score on a 0–1 scale whether the doc describes
that symbol. Edges above `min_confidence` land in the `doc_code_links`
table. Idempotent: re-runs UPDATE existing rows (keyed by the unique
`(doc_chunk_id, symbol_chunk_id)` constraint) rather than duplicate.

Design:
  - **Dependency injection** for every side effect so the orchestrator is
    testable without a real DB or real LLM.
      - `find_candidates_fn(doc_chunk_id, doc_content) -> list[SymbolCandidate]`
      - `score_link_fn(doc_content, symbol_content) -> Awaitable[float]`
  - **Pure helpers** (`_extract_symbol_mentions`, `_parse_confidence`)
    cover the token-matching + LLM-output-parsing logic.
  - Fail-open: per-pair LLM failure yields 0.0 confidence (edge dropped);
    one pair's failure never stops the linker from processing the rest.

Not wired into ingestion automatically. Callers invoke explicitly, e.g.
from a future `POST /admin/link-docs-to-code` endpoint.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.config import settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Data shapes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SymbolCandidate:
    """A code symbol chunk suggested as possibly described by a doc chunk."""
    symbol_chunk_id: str
    symbol_name: str
    symbol_kind: str | None
    source_file: str
    content: str


@dataclass
class LinkerReport:
    """Summary of a single `link_chunks` pass."""
    docs_processed: int = 0
    candidates_considered: int = 0
    edges_written: int = 0
    edges_skipped_low_confidence: int = 0
    edges_skipped_llm_error: int = 0
    per_doc_edge_counts: dict[str, int] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────────

# Match dotted paths like RateLimiter.acquire, also CamelCase class names on
# their own (e.g. "RateLimiter"), and method names inside code-fence-style
# backticks (e.g. `acquire()`).
_DOTTED_SYMBOL_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
_CAMELCASE_IDENT_RE = re.compile(r"\b([A-Z][a-z]+[A-Za-z0-9_]*)\b")
_BACKTICK_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?)`")


def _extract_symbol_mentions(text: str) -> list[str]:
    """Return a deduplicated, order-preserving list of symbol-like tokens
    mentioned in a doc chunk.

    Captures:
      - `Class.method` dotted references (`RateLimiter.acquire`) → emits both
        the class and the method as separate mentions.
      - Bare CamelCase identifiers (`RateLimiter`, `PaymentController`).
      - Backtick-quoted identifiers — with or without `()` suffix.

    Pure, no external deps.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def _add(tok: str) -> None:
        tok = tok.strip().rstrip("()")
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)

    for m in _DOTTED_SYMBOL_RE.finditer(text):
        _add(m.group(1))   # class
        _add(m.group(2))   # method

    for m in _CAMELCASE_IDENT_RE.finditer(text):
        _add(m.group(1))

    for m in _BACKTICK_IDENT_RE.finditer(text):
        _add(m.group(1))

    return out


def _parse_confidence(llm_raw: str) -> float:
    """Extract a 0–1 float from the LLM's confidence reply.

    Tolerant of common response shapes:
      "0.8"              → 0.8
      "0.8 - ..."        → 0.8
      "{\"confidence\": 0.8}" → 0.8 (parses first matching float)
      "high"             → 0.0 (we demand a numeric; prose = dropped)

    Returns 0.0 on any failure. Clamps to [0.0, 1.0].
    """
    if not llm_raw:
        return 0.0
    # First, try JSON-like with a `confidence` key. Include optional minus
    # so negative values are captured (and clamped to 0.0 below).
    m = re.search(r'"confidence"\s*:\s*(-?[0-9]*\.?[0-9]+)', llm_raw)
    if m is None:
        # Fall back to first signed float in the string.
        m = re.search(r"(-?\d*\.?\d+)", llm_raw)
    if m is None:
        return 0.0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        # Also handle "80%" style: if the LLM wrote 80 intending 0.80.
        if value <= 100.0:
            return value / 100.0
        return 1.0
    return value


# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────

_SCORE_SYSTEM_PROMPT = load_prompt("rag/doc_code_linker/score_system_prompt.md")


async def score_link(
    doc_content: str,
    symbol_content: str,
    *,
    call_llm_fn: Callable[[str, list[dict]], Awaitable[str]] | None = None,
) -> float:
    """Ask the LLM for a 0-1 confidence that `doc_content` describes `symbol_content`.

    Args:
        doc_content: The doc chunk text.
        symbol_content: The code symbol chunk text.
        call_llm_fn: Injectable LLM callable `(system, messages) -> str`.
                     Defaults to `app.core.llm.call_llm`.

    Returns:
        Float in [0, 1]. Returns 0.0 on any LLM error or unparseable output.
    """
    if not doc_content or not symbol_content:
        return 0.0

    if call_llm_fn is None:
        from app.core.llm import call_llm as _default_call
        from app.core.llm_router import pick_model_for_agent
        # Slice 27a — doc_code_linker is Purpose.ROUTING (per-pair confidence
        # scoring is a fast classifier-style call).
        _routed_model = pick_model_for_agent("doc_code_linker")
        async def call_llm_fn(system, messages):  # type: ignore[misc]
            return await _default_call(
                system=system, messages=messages,
                max_tokens=80, model=_routed_model,
            )

    # Truncate aggressively — the judgment doesn't need the whole file.
    doc_snippet = doc_content[:2500]
    sym_snippet = symbol_content[:2500]
    user_content = (
        f"DOC CHUNK:\n```\n{doc_snippet}\n```\n\n"
        f"CODE SYMBOL CHUNK:\n```\n{sym_snippet}\n```\n\n"
        f"Return the JSON with `confidence`."
    )

    try:
        raw = await call_llm_fn(
            _SCORE_SYSTEM_PROMPT,
            [{"role": "user", "content": user_content}],
        )
    except Exception as e:
        logger.warning("score_link: LLM call failed: %s", e)
        return 0.0

    if not isinstance(raw, str):
        return 0.0
    return _parse_confidence(raw)


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

_FindCandidatesFn = Callable[[str, str], list[SymbolCandidate]]
_ScoreLinkFn = Callable[[str, str], Awaitable[float]]
_UpsertEdgeFn = Callable[[str, str, float], None]


async def link_chunks(
    *,
    doc_chunks: list[tuple[str, str]],   # [(doc_chunk_id, doc_content), ...]
    find_candidates_fn: _FindCandidatesFn,
    score_link_fn: _ScoreLinkFn,
    upsert_edge_fn: _UpsertEdgeFn,
    min_confidence: float | None = None,
    max_candidates_per_doc: int | None = None,
) -> LinkerReport:
    """Run the linking pass over the supplied doc chunks.

    All side effects (DB read for candidates, LLM scoring, DB write) flow
    through injected callables — the orchestrator itself is pure.

    Args:
        doc_chunks: Iterable of (doc_chunk_id, doc_content).
        find_candidates_fn: given (doc_chunk_id, doc_content) returns candidate
                            code-symbol chunks.
        score_link_fn: async (doc_content, symbol_content) -> confidence float.
        upsert_edge_fn: (doc_chunk_id, symbol_chunk_id, confidence) -> None.
                        Should be idempotent (INSERT ... ON CONFLICT UPDATE).
        min_confidence: edges below this are skipped (default from settings).
        max_candidates_per_doc: cap on candidates scored per doc (default from
                                settings).

    Returns:
        LinkerReport summary.
    """
    min_conf = min_confidence if min_confidence is not None else settings.doc_code_link_min_confidence
    max_cand = max_candidates_per_doc if max_candidates_per_doc is not None else settings.doc_code_link_max_candidates

    report = LinkerReport()

    for doc_chunk_id, doc_content in doc_chunks:
        report.docs_processed += 1
        report.per_doc_edge_counts.setdefault(doc_chunk_id, 0)

        try:
            candidates = find_candidates_fn(doc_chunk_id, doc_content) or []
        except Exception as e:
            logger.warning("link_chunks: find_candidates failed for %s: %s", doc_chunk_id, e)
            continue

        for candidate in candidates[:max_cand]:
            report.candidates_considered += 1
            try:
                confidence = await score_link_fn(doc_content, candidate.content)
            except Exception as e:
                logger.warning("link_chunks: score_link raised for pair (%s, %s): %s",
                               doc_chunk_id, candidate.symbol_chunk_id, e)
                report.edges_skipped_llm_error += 1
                continue

            if confidence < min_conf:
                report.edges_skipped_low_confidence += 1
                continue

            try:
                upsert_edge_fn(doc_chunk_id, candidate.symbol_chunk_id, confidence)
            except Exception as e:
                logger.warning("link_chunks: upsert failed for pair (%s, %s): %s",
                               doc_chunk_id, candidate.symbol_chunk_id, e)
                continue

            report.edges_written += 1
            report.per_doc_edge_counts[doc_chunk_id] += 1

    logger.info(
        "link_chunks done: docs=%d candidates=%d edges_written=%d skipped_conf=%d skipped_err=%d",
        report.docs_processed, report.candidates_considered,
        report.edges_written, report.edges_skipped_low_confidence,
        report.edges_skipped_llm_error,
    )
    return report
