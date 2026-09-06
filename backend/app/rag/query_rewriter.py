# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Query rewriting — bridge PM vocabulary to the ecosystem's canonical vocabulary.

Problem: the PM writes casual/marketing phrasing ("AutoSplit with Smart
Settlements") but the KB uses the ecosystem's official terms ("split
settlement", "multi-party beneficiary transaction"). Dense + sparse
retrieval on the raw PM prompt misses chunks whose vocabulary is official
rather than PM-casual.

Solution: one fast LLM pass produces 3–5 alternate queries using the
active domain's official terminology, message/API names, error-code
families and regulatory phrasing. The prompt file stays domain-neutral;
the concrete vocabulary arrives from the active pack's
`retrieval_domain_expertise` prompt block (module-level, same pattern as
the agents — the registry resolves without a DB). The semantic retrieval
stage runs all of them + the original feature description, then RRF-merges.

Results cached in Redis by hash(feature_description) for 24h so the same
feature across multi-turn conversations doesn't re-query the LLM.
"""
import hashlib
from app.core.prompts import load_prompt
import json
import logging

from app.core.config import settings
from app.core.domain.registry import prompt_block
from app.core.json_recovery import parse_llm_json
from app.core.llm import call_llm

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h
_CACHE_PREFIX = "rag:query_rewriter:"
_DEFAULT_K = 4
_MAX_K = 6

_REWRITER_SYSTEM = load_prompt("rag/query_rewriter/rewriter_system.md")
# Domain vocabulary (official terms, message names, error-code families) from
# the active pack. Empty for a domain that declares none — the neutral prompt
# alone then asks for the ecosystem's official vocabulary without naming one.
_DOMAIN_EXPERTISE = prompt_block("retrieval_domain_expertise", "")


def _cache_key(feature_description: str, classification: dict, k: int) -> str:
    h = hashlib.sha256()
    h.update(feature_description.strip().encode("utf-8"))
    h.update(b"|")
    h.update(str(classification.get("primary", "")).encode("utf-8"))
    h.update(f"|k={k}".encode())
    return _CACHE_PREFIX + h.hexdigest()[:24]


def _redis_client():
    import redis
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def _cache_get(key: str) -> list[str] | None:
    try:
        raw = _redis_client().get(key)
        if not raw:
            return None
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data if x]
    except Exception as e:
        logger.debug("query_rewriter cache get failed: %s", e)
    return None


def _cache_set(key: str, queries: list[str]) -> None:
    try:
        _redis_client().setex(key, _CACHE_TTL_SECONDS, json.dumps(queries))
    except Exception as e:
        logger.debug("query_rewriter cache set failed: %s", e)


async def rewrite_queries(
    feature_description: str,
    classification: dict | None = None,
    *,
    k: int = _DEFAULT_K,
) -> list[str]:
    """Return k domain-vocab rewritten queries for the feature description.

    Always returns a non-empty list: falls back to [feature_description]
    if the LLM call fails or returns nothing usable.
    """
    if not feature_description or not feature_description.strip():
        return []

    k = max(1, min(k, _MAX_K))
    classification = classification or {}
    key = _cache_key(feature_description, classification, k)

    cached = _cache_get(key)
    if cached:
        logger.debug("query_rewriter cache HIT (%d queries)", len(cached))
        return cached[:k]

    system = _REWRITER_SYSTEM.format(k=k)
    if _DOMAIN_EXPERTISE:
        # Appended AFTER .format() — pack prose may legally contain braces.
        system = f"{system}\n\n## Domain vocabulary\n\n{_DOMAIN_EXPERTISE}"
    user_lines = [f"Feature description:\n{feature_description.strip()}"]
    if classification.get("primary"):
        user_lines.append(f"Taxonomy primary bucket: {classification['primary']}")
    user_content = "\n\n".join(user_lines)

    try:
        raw = await call_llm(
            system=system,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=500,
        )
    except Exception as e:
        logger.warning("query_rewriter LLM call failed: %s", e)
        return [feature_description.strip()]

    parsed = await parse_llm_json(raw, expect_array=True, fallback=None)

    queries: list[str] = []
    if isinstance(parsed, list):
        queries = [str(q).strip() for q in parsed if isinstance(q, (str, int))]
    elif isinstance(parsed, dict):
        # LLM sometimes wraps in {"queries": [...]} — find the first list value
        for v in parsed.values():
            if isinstance(v, list):
                queries = [str(q).strip() for q in v if isinstance(q, (str, int))]
                break

    queries = [q for q in queries if q and 2 <= len(q) <= 200][:k]

    if not queries:
        logger.warning("query_rewriter produced nothing usable, falling back to raw prompt")
        return [feature_description.strip()]

    _cache_set(key, queries)
    logger.info(
        "query_rewriter produced %d queries for %r: %s",
        len(queries), feature_description[:60], queries,
    )
    return queries
