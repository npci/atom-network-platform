# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Query-understanding step (Slice 5).

Given a raw user query, one LLM call produces three enrichments:
  - **Sub-questions** — decomposition into ≤3 atomic sub-queries. Each becomes
    an additional pass through `hybrid_retrieve`.
  - **Entities** — technical identifiers / API / component names mentioned.
    Captured for future graph-lookup (plan §6.2); not used for retrieval yet
    because no knowledge graph exists (Slice 19).
  - **HyDE hypothetical answer** — 1–2 sentence plausible answer as if we knew
    the facts. Used as a separate dense-retrieval pass; per plan §6.2
    significantly improves recall over embedding the raw question.

Fail-open at multiple levels:
  - Whole LLM call fails  → `EnrichedQuery(original=query)` with empty fields
    (caller falls back to single-pass retrieval).
  - JSON parse fails      → same fallback.
  - Individual field missing → that field is empty; others keep their values.

Used by `app.rag.retrieval.retrieve()` when
`settings.use_query_understanding` is True.
"""
from __future__ import annotations
from app.core.prompts import load_prompt
from app.core.domain.registry import prompt_block as _registry_prompt_block

import hashlib
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_SUB_QUESTIONS = 3
MAX_OUTPUT_TOKENS = 500

# ── Phase 2.2 — Enriched-query LRU cache ──────────────────────────────────────
# Process-local. Keyed on SHA256(query) so we never store user PII as a key.
# Bounded by `settings.query_understanding_cache_size` (read at call time so
# tests can flip the size without re-importing).
_enriched_cache: "OrderedDict[str, EnrichedQuery]" = OrderedDict()
_enriched_cache_lock = threading.RLock()
_enriched_cache_hits = 0
_enriched_cache_misses = 0


def _query_key(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()


def _enriched_cache_capacity() -> int:
    try:
        from app.core.config import settings
        return int(getattr(settings, "query_understanding_cache_size", 1024) or 0)
    except Exception:
        return 1024


def _enriched_cache_get(query: str) -> "EnrichedQuery | None":
    global _enriched_cache_hits, _enriched_cache_misses
    cap = _enriched_cache_capacity()
    if cap <= 0:
        return None
    key = _query_key(query)
    with _enriched_cache_lock:
        val = _enriched_cache.get(key)
        if val is None:
            _enriched_cache_misses += 1
            return None
        # Touch the entry — move to MRU end.
        _enriched_cache.move_to_end(key)
        _enriched_cache_hits += 1
        return val


def _enriched_cache_put(query: str, value: "EnrichedQuery") -> None:
    cap = _enriched_cache_capacity()
    if cap <= 0:
        return
    key = _query_key(query)
    with _enriched_cache_lock:
        if key in _enriched_cache:
            _enriched_cache.move_to_end(key)
            _enriched_cache[key] = value
            return
        _enriched_cache[key] = value
        while len(_enriched_cache) > cap:
            _enriched_cache.popitem(last=False)


def query_understanding_cache_stats() -> dict:
    """Return {size, capacity, hits, misses}. For ops dashboards / tests."""
    with _enriched_cache_lock:
        return {
            "size":     len(_enriched_cache),
            "capacity": _enriched_cache_capacity(),
            "hits":     _enriched_cache_hits,
            "misses":   _enriched_cache_misses,
        }


def _reset_query_understanding_cache_for_tests() -> None:
    """Clear cache + counters. Test-only hook."""
    global _enriched_cache_hits, _enriched_cache_misses
    with _enriched_cache_lock:
        _enriched_cache.clear()
        _enriched_cache_hits = 0
        _enriched_cache_misses = 0

# The prompt file is domain-neutral; the active pack's own description of its
# ecosystem is appended here (module-level, same pattern as the agents) so
# entities and the HyDE decoy come out in the right domain's vocabulary. A
# pack that supplies no description leaves the prompt fully neutral.
_SYSTEM_PROMPT = load_prompt("rag/query_understanding/system_prompt.md")
_DOMAIN_CONTEXT = _registry_prompt_block("ecosystem_description", "")
if _DOMAIN_CONTEXT:
    _SYSTEM_PROMPT = f"{_SYSTEM_PROMPT}\n\nDomain context: {_DOMAIN_CONTEXT}"


@dataclass
class EnrichedQuery:
    """Result of `enrich(query)`. Any field may be empty on partial failure."""
    original: str
    sub_questions: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    hyde_text: str = ""

    def variants_for_retrieval(self) -> list[str]:
        """Ordered list of query strings to run through hybrid_retrieve.

        Always starts with the original query (the safety net — never dropped
        even when enrichment fails). HyDE follows (best dense-recall lift per
        plan §6.2). Sub-questions tail-end.
        """
        out = [self.original]
        if self.hyde_text:
            out.append(self.hyde_text)
        out.extend(self.sub_questions[:MAX_SUB_QUESTIONS])
        # De-dup while preserving order (in case HyDE text happens to equal a
        # sub-question, or LLM echoes the original).
        seen: set[str] = set()
        deduped: list[str] = []
        for q in out:
            key = q.strip()
            if key and key not in seen:
                seen.add(key)
                deduped.append(q)
        return deduped


async def enrich(query: str) -> EnrichedQuery:
    """Produce an EnrichedQuery. Returns the raw-query-only fallback on any failure.

    Args:
        query: User's raw search string.

    Returns:
        EnrichedQuery. `original` is always set. Other fields may be empty on
        LLM / parse failure.
    """
    fallback = EnrichedQuery(original=query)
    if not query or not query.strip():
        return fallback

    try:
        from app.core.llm import call_llm
        from app.core.llm_router import pick_model_for_agent
        from app.core.json_recovery import parse_llm_json

        # Slice 27a — query_understanding is a Purpose.ROUTING workload.
        raw = await call_llm(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Query: {query.strip()}"}],
            max_tokens=MAX_OUTPUT_TOKENS,
            model=pick_model_for_agent("query_understanding"),
            agent_name="query_understanding",
        )
    except Exception as e:
        logger.warning("query_understanding.enrich: LLM call failed — falling back: %s", e)
        return fallback

    parsed = await parse_llm_json(raw, fallback=None, llm_self_correct=False)
    if not isinstance(parsed, dict):
        logger.warning("query_understanding.enrich: non-dict LLM output — falling back")
        return fallback

    return EnrichedQuery(
        original=query,
        sub_questions=_normalize_list(parsed.get("sub_questions"), max_items=MAX_SUB_QUESTIONS),
        entities=_normalize_list(parsed.get("entities"), max_items=10),
        hyde_text=_normalize_str(parsed.get("hypothetical_answer")),
    )


def enrich_sync(query: str) -> EnrichedQuery:
    """Sync entrypoint. Phase 2.2 — checks the process-local LRU first.

    On cache miss, delegates to `_enrich_sync_uncached` (the original
    sync-bridge implementation) and stores the result. On cache hit,
    returns the stored EnrichedQuery without touching the LLM.
    """
    if not query or not query.strip():
        return EnrichedQuery(original=query)
    cached = _enriched_cache_get(query)
    if cached is not None:
        return cached
    enriched = _enrich_sync_uncached(query)
    # Don't cache the all-empty fallback — its cost was negligible and
    # caching it freezes a transient LLM/network failure into the LRU.
    if enriched.hyde_text or enriched.sub_questions or enriched.entities:
        _enriched_cache_put(query, enriched)
    return enriched


def _enrich_sync_uncached(query: str) -> EnrichedQuery:
    """Original `enrich_sync` body (no cache).

    Behaviour:
      - No running loop  → `asyncio.run(enrich(query))` directly. Cheapest path.
      - Running loop     → run `enrich` on a fresh background thread with its
        own event loop, then block-wait for the result. This recovers the
        multi-pass enrichment when `retrieve` is called from inside an async
        FastAPI/WS handler (e.g. `code_change`, BRD docgen). Without this
        path, every retrieval inside an async caller silently fell back to
        single-pass, losing HyDE + sub-question expansion.

    Returns the raw-query fallback if the thread-bridged call also fails.
    """
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False

    if not running:
        try:
            return asyncio.run(enrich(query))
        except Exception as e:
            logger.warning("enrich_sync (no-loop path) failed: %s", e)
            return EnrichedQuery(original=query)

    # In a running loop — bridge to a worker thread that runs its own loop.
    def _runner() -> EnrichedQuery:
        return asyncio.run(enrich(query))

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_runner)
            return future.result(timeout=20.0)
    except concurrent.futures.TimeoutError:
        logger.warning("enrich_sync thread-bridge timed out — using raw query")
        return EnrichedQuery(original=query)
    except Exception as e:
        logger.warning("enrich_sync thread-bridge failed: %s", e)
        return EnrichedQuery(original=query)


# ──────────────────────────────────────────────────────────────────────────────
# Normalizers — defensive against LLM drift
# ──────────────────────────────────────────────────────────────────────────────

def _normalize_list(value, max_items: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:max_items]:
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
    return out


def _normalize_str(value) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
