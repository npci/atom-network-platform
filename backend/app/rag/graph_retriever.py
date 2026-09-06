# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Graph-traversal retrieval pass (Slice 20).

Third retrieval path alongside dense (pgvector) and sparse (BM25). When on,
`graph_retrieve(query)`:
  1. Extracts symbol-name seeds from the query (CamelCase identifiers,
     dotted paths, backticked names).
  2. For each seed, runs 3 Cypher queries against the AGE `npci_kg` graph:
     - self-match on symbol_name
     - structural 1-hop (CALLS / INHERITS / IMPLEMENTS, both directions)
     - DESCRIBES reverse (DocChunks describing the symbol)
  3. Deduplicates chunk_ids with hop-distance-based scores:
     self=1.0, describes=0.8, neighbor=0.7 (keeps max when a chunk is hit
     multiple ways).
  4. Hydrates chunk_ids into DocumentChunk rows via one `WHERE id IN (...)`
     Postgres query, returning dicts shaped like hybrid_retrieve output so
     RRF fusion downstream "just works".

Fail-open at every boundary:
  - Empty query / no seeds → []
  - AGE unavailable → []
  - Individual Cypher failure → that seed/hop skipped, others continue
  - Chunk hydration error → []

No production caller this slice. Consumers will wrap this as an extra
pass inside `retrieval._multi_pass_retrieve` in a follow-up slice.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.kg import client as kg_client

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Seed extraction (pure)
# ──────────────────────────────────────────────────────────────────────────────

# English stopwords + RAG noise — dropped from seed extraction so "the" /
# domain acronyms / "API" don't become fake symbol seeds. Uppercase-acronym-only
# terms are kept out because they're rarely class names in our corpus.
_GENERIC_QUERY_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "of", "in", "on", "to", "for",
    "with", "by", "at", "from", "as", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "can", "could", "should",
    "would", "will", "may", "might", "must", "this", "that", "these", "those",
    "what", "which", "who", "whom", "when", "where", "why", "how",
    # RAG-noise English terms that look symbol-ish but aren't
    "api", "apis", "json", "xml", "pdf", "doc", "file",
    "code", "test", "tests", "class", "method", "function", "object", "type",
    "error", "errors", "spec", "specs", "data", "service", "system",
})


def _domain_acronym_stopwords() -> frozenset[str]:
    """Domain acronyms from the ACTIVE pack, not hardcoded ("upi"/"npci"/"psp"
    used to sit in the list above): the pack key plus every all-uppercase word
    in a participant label ("PSP Bank" → "psp"). Fail-open — this module never
    raises — so a broken pack just means acronyms aren't filtered.
    """
    try:
        from app.core.domain.contract import participants_of
        from app.core.domain.registry import get_active_pack

        pack = get_active_pack()
        tokens = {str(pack.key).lower()}
        for participant in participants_of(pack):
            for word in re.findall(r"[A-Za-z]{2,}", participant.label or ""):
                if word.isupper():
                    tokens.add(word.lower())
        return frozenset(tokens)
    except Exception:  # noqa: BLE001 — fail-open like every other boundary here
        return frozenset()


_QUERY_STOPWORDS = _GENERIC_QUERY_STOPWORDS | _domain_acronym_stopwords()

# CamelCase: starts with uppercase + has at least one lowercase letter OR
# another uppercase boundary later. Accepts "Foo", "FooBar", "UPIService".
# Pure lower-case words and single-letter tokens don't match.
_CAMEL_CASE_RE = re.compile(r"\b([A-Z][a-zA-Z0-9_]*[a-z][A-Za-z0-9_]*|[A-Z][a-zA-Z0-9_]+)\b")

# Dotted path: `Foo.bar` / `foo.bar.baz` / `Foo.Bar`. Captures the full
# dotted expression AND each component for the seed pool.
_DOTTED_PATH_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b")

# Backticked identifier: `foo` / `Foo.bar` / `Foo()` — strip the backticks
# and the trailing `()` if present.
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_\.]*)\(?\)?`")


def _extract_query_seeds(query: str, *, max_seeds: int | None = None) -> list[str]:
    """Extract likely symbol-name tokens from a natural-language query.

    Returns an ordered, deduplicated list. Seeds come from three patterns:
      - Backticked identifiers (highest signal; stripped of () suffix)
      - Dotted paths (captured whole + split into components)
      - CamelCase words (Foo, FooBar, UPIService)

    Tokens are filtered:
      - length ≥ 2
      - not in _QUERY_STOPWORDS (case-insensitive)

    `max_seeds` caps the return list (defaults to
    `settings.graph_retrieval_max_seeds`).
    """
    if not query or not isinstance(query, str):
        return []

    seen: set[str] = set()
    seeds: list[str] = []

    def _add(tok: str) -> None:
        tok = tok.strip()
        if len(tok) < 2:
            return
        if tok.lower() in _QUERY_STOPWORDS:
            return
        if tok in seen:
            return
        seen.add(tok)
        seeds.append(tok)

    for m in _BACKTICK_RE.finditer(query):
        raw = m.group(1)
        _add(raw)
        # Split dotted backticked expression into parts too.
        if "." in raw:
            for part in raw.split("."):
                _add(part)

    for m in _DOTTED_PATH_RE.finditer(query):
        raw = m.group(1)
        _add(raw)
        for part in raw.split("."):
            _add(part)

    for m in _CAMEL_CASE_RE.finditer(query):
        _add(m.group(1))

    cap = max_seeds if max_seeds is not None else settings.graph_retrieval_max_seeds
    return seeds[:cap]


# ──────────────────────────────────────────────────────────────────────────────
# Cypher builders (pure)
# ──────────────────────────────────────────────────────────────────────────────

_ESC = kg_client.escape_cypher_literal


def build_self_cypher(seed: str) -> str:
    """Match nodes whose `symbol_name` equals the seed. Returns `cid`."""
    return (
        f"MATCH (n) WHERE n.symbol_name = {_ESC(seed)}\n"
        f"RETURN DISTINCT n.chunk_id AS cid"
    )


# AGE's Cypher parser (as of PG16 build) rejects pipe-union edge syntax
# like `[:CALLS|INHERITS|IMPLEMENTS]`, so we emit one query per edge label.
# Tuple captured in module scope so traverse_seeds can iterate.
_NEIGHBOR_EDGE_LABELS: tuple[str, ...] = ("CALLS", "INHERITS", "IMPLEMENTS")


def build_neighbors_cypher(seed: str, edge_label: str = "CALLS") -> str:
    """Match 1-hop structural neighbors of the seed by a single edge label.

    Undirected `-[r:LABEL]- ` picks up both outbound and inbound.
    """
    if edge_label not in _NEIGHBOR_EDGE_LABELS:
        raise ValueError(f"unsupported edge label: {edge_label!r}")
    return (
        f"MATCH (n) WHERE n.symbol_name = {_ESC(seed)}\n"
        f"MATCH (n)-[r:{edge_label}]-(m)\n"
        f"RETURN DISTINCT m.chunk_id AS cid"
    )


def build_describes_cypher(seed: str) -> str:
    """Match DocChunks that DESCRIBE any node whose symbol_name equals seed."""
    return (
        f"MATCH (n) WHERE n.symbol_name = {_ESC(seed)}\n"
        f"MATCH (d:DocChunk)-[r:DESCRIBES]->(n)\n"
        f"RETURN DISTINCT d.chunk_id AS cid"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Traversal (DI-friendly)
# ──────────────────────────────────────────────────────────────────────────────

# Hop-distance scores — tuned so describes edges rank above generic
# structural neighbors (doc→code links are human-curated and usually more
# directly relevant to the query).
_SCORE_SELF = 1.0
_SCORE_DESCRIBES = 0.8
_SCORE_NEIGHBOR = 0.7


def traverse_seeds(
    seeds: list[str],
    *,
    run_cypher_fn: Callable[[str], list[dict]],
) -> dict[str, float]:
    """For each seed, run self + neighbor + describes queries and collect
    chunk_ids with their best hop-score.

    Per-query failures are swallowed so one bad seed doesn't void the lot.
    `run_cypher_fn(cypher) -> list[{"cid": "..."}]` — injectable for tests.

    Returns `{chunk_id: max_score}`.
    """
    scored: dict[str, float] = {}

    def _accumulate(rows: Iterable[dict], score: float) -> None:
        for row in rows:
            cid = row.get("cid")
            if not cid or not isinstance(cid, str):
                continue
            prev = scored.get(cid)
            if prev is None or score > prev:
                scored[cid] = score

    for seed in seeds:
        # Cypher queries run per seed:
        #   1 self + 1 describes + N-per-edge-label neighbor (AGE parser
        #   doesn't accept pipe-union edge syntax, so we split).
        planned: list[tuple[str, float]] = [
            (build_self_cypher(seed),      _SCORE_SELF),
            (build_describes_cypher(seed), _SCORE_DESCRIBES),
        ]
        for edge_label in _NEIGHBOR_EDGE_LABELS:
            planned.append((build_neighbors_cypher(seed, edge_label), _SCORE_NEIGHBOR))

        for cypher, score in planned:
            try:
                rows = run_cypher_fn(cypher) or []
            except Exception as e:
                logger.debug("graph traversal cypher failed (seed=%r): %s", seed, e)
                continue
            _accumulate(rows, score)

    return scored


# ──────────────────────────────────────────────────────────────────────────────
# Hydration (pure-ish — takes a db session but no Cypher)
# ──────────────────────────────────────────────────────────────────────────────

def hydrate_chunks(
    db: Session,
    scored: dict[str, float],
) -> list[dict]:
    """Resolve chunk_ids to DocumentChunk rows shaped like hybrid_retrieve output.

    Sorted by score descending. Missing chunk_ids (the graph knew a node by
    chunk_id but the row was deleted) are silently dropped.

    Fail-open: returns `[]` on any DB error.
    """
    if not scored:
        return []

    from app.models.document_chunk import DocumentChunk

    try:
        rows = db.execute(
            select(DocumentChunk).where(DocumentChunk.id.in_(list(scored.keys())))
        ).scalars().all()
    except Exception as e:
        logger.warning("graph retriever: chunk hydration failed: %s", e)
        # Rollback to clear the aborted-transaction state — without this,
        # the caller's session is poisoned (next commit fails with
        # InFailedSqlTransaction). Common trigger: DocumentChunk ORM model
        # references columns that don't exist in the DB (uat/main drift).
        try:
            db.rollback()
        except Exception:
            pass
        return []

    hydrated: list[dict] = []
    for row in rows:
        score = scored.get(row.id, 0.0)
        hydrated.append({
            "id":                row.id,
            "source_file":       row.source_file,
            "doc_category":      row.doc_category,
            "content":           row.content,
            "chunk_index":       row.chunk_index,
            "score":             score,
            "parent_symbol_id":  row.parent_symbol_id,
            "graph_score":       score,   # debugging field, preserved post-fusion
        })

    hydrated.sort(key=lambda c: c["score"], reverse=True)
    return hydrated


# ──────────────────────────────────────────────────────────────────────────────
# Main entry
# ──────────────────────────────────────────────────────────────────────────────

def _graph_retrieve_via_sql(
    seeds: list[str],
    db: Session,
    *,
    top_k: int,
) -> list[dict]:
    """Phase 1.5 — pure-SQL alternative to the AGE Cypher traversal.

    Uses `app.kg.sql_graph.expand_neighbors` against `document_chunks`
    JSON edge columns. Hop scores match the AGE path so RRF fusion
    sees identical numeric ranges.

    Fail-soft: returns [] on any error so the caller's fail-open
    contract is preserved.
    """
    try:
        from app.kg import sql_graph
        rows = sql_graph.expand_neighbors(db, seeds, max_hops=1, max_results=top_k * 5)
    except Exception as e:
        logger.debug("graph_retrieve(sql): expand_neighbors failed: %s", e)
        # Same session-poisoning concern as the hydration except block above.
        try:
            db.rollback()
        except Exception:
            pass
        return []

    seed_set = {s for s in seeds if s}
    out: list[dict] = []
    for r in rows:
        if r.get("symbol_name") in seed_set:
            score = _SCORE_SELF
        else:
            score = _SCORE_NEIGHBOR
        out.append({
            "id":                r["id"],
            "source_file":       r["source_file"],
            "doc_category":      r["doc_category"],
            "content":           r["content"],
            "chunk_index":       r["chunk_index"],
            "score":             score,
            "parent_symbol_id":  r["parent_symbol_id"],
            "graph_score":       score,
        })
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:top_k]


def graph_retrieve(
    query: str,
    db: Session,
    *,
    top_k: int = 10,
    graph_name: str | None = None,
) -> list[dict]:
    """Run the full graph-traversal retrieval pass.

    Returns `[]` on any failure (empty query / no seeds / AGE unavailable /
    all Cypher errors / hydration error). Always safe to fuse into a
    multi-pass retriever — never raises, never blocks other passes.

    Phase 1.5 — when `settings.graph_backend == "sql"`, dispatches to the
    pure-SQL backend (`app.kg.sql_graph`). Default "age" preserves the
    existing AGE Cypher path.
    """
    if not query or not isinstance(query, str):
        return []

    seeds = _extract_query_seeds(query)
    if not seeds:
        return []

    # Phase 1.5 — pure-SQL backend dispatch.
    try:
        from app.core.config import settings as _settings
        if getattr(_settings, "graph_backend", "age") == "sql":
            return _graph_retrieve_via_sql(seeds, db, top_k=top_k)
    except Exception as _gb_err:
        logger.debug("graph_backend dispatch skipped: %s", _gb_err)

    if not kg_client.is_age_available(db):
        logger.debug("graph_retrieve: AGE unavailable, skipping")
        return []

    graph_name = graph_name or settings.kg_graph_name

    def _run(cypher: str) -> list[dict]:
        """Execute one Cypher query. On error, rollback so the next query
        (or the downstream hydration SELECT) isn't blocked by Postgres'
        aborted-transaction state. Returns [] on any failure — the
        traverse_seeds exception path still logs the error.
        """
        try:
            return kg_client.run_cypher(
                db, cypher,
                graph_name=graph_name,
                return_cols=[("cid", "agtype")],
            )
        except Exception as e:
            logger.debug("graph_retrieve: cypher failed, rolling back: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
            return []

    scored = traverse_seeds(seeds, run_cypher_fn=_run)
    hydrated = hydrate_chunks(db, scored)
    return hydrated[:top_k]
