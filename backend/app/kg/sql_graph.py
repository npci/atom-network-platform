# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pure-SQL graph helpers — Phase 1.5.

The Apache AGE-backed `graph_retriever.py` and `impact_analyzer.py`
do graph traversal via Cypher inside Postgres. The data they traverse
is *already* materialised in `document_chunks` JSON columns
(`calls`, `cross_file_calls`, `inherits`, `implements`, `imports`),
so the same traversal can be done with one SQL query — no AGE,
no Cypher escaping, no per-query rollback dance.

This module exposes the two operations the rest of the codebase
actually uses:

  - `expand_neighbors(db, seeds, max_hops=1)` — find chunks for the
    seed symbols and their 1-hop CALLS / INHERITS / IMPLEMENTS
    neighbours. Used by retrieval-side graph fusion.

  - `inbound_callers(db, target_symbols)` — find chunks that CALL
    any of the target symbols. Used by impact analysis.

Both helpers fail-soft (empty input → []; missing column → []).
They are designed to be consumed BEHIND the existing AGE-backed
modules via a feature flag, so we can A/B them against AGE on the
eval harness before deleting AGE entirely.

JSON column shapes used:
  - `calls`            — `list[str]`  (within-file callee symbol names)
  - `cross_file_calls` — `list[{callee_symbol, callee_path, line, ...}]`
  - `inherits`         — `str`        (single parent class name)
  - `implements`       — `list[str]`  (interface names)
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _hydrate(db: Session, chunk_ids: Iterable[str]) -> list[dict]:
    """Fetch chunk rows by id. Same shape as `hybrid_search._hydrate_chunks`
    so retrieval-side fusion can consume the result without adapters.
    """
    ids = list(dict.fromkeys(chunk_ids))  # de-dup preserve order
    if not ids:
        return []
    sql = text("""
        SELECT id, source_file, doc_category, content, chunk_index,
               parent_symbol_id, symbol_name, symbol_kind, language
        FROM document_chunks
        WHERE id = ANY(:ids)
          AND (deprecated IS NOT TRUE)
    """)
    rows = db.execute(sql, {"ids": ids}).fetchall()
    return [
        {
            "id":               r.id,
            "source_file":      r.source_file,
            "doc_category":     r.doc_category,
            "content":          r.content,
            "chunk_index":      r.chunk_index,
            "parent_symbol_id": r.parent_symbol_id,
            "symbol_name":      r.symbol_name,
            "symbol_kind":      r.symbol_kind,
            "language":         r.language,
        }
        for r in rows
    ]


def find_chunks_by_symbol_name(db: Session, symbols: Iterable[str]) -> list[dict]:
    """Return chunk rows whose `symbol_name` matches any of `symbols`."""
    syms = [s for s in symbols if s]
    if not syms:
        return []
    sql = text("""
        SELECT id, source_file, doc_category, content, chunk_index,
               parent_symbol_id, symbol_name, symbol_kind, language
        FROM document_chunks
        WHERE symbol_name = ANY(:syms)
          AND (deprecated IS NOT TRUE)
    """)
    try:
        # SAVEPOINT-scoped so a query failure (schema drift, a malformed row)
        # rolls back ONLY this statement, never the caller's transaction. A bare
        # swallow here would leave the session in Postgres' aborted state and
        # poison the next unrelated write (the deep-research tx-poison bug).
        with db.begin_nested():
            rows = db.execute(sql, {"syms": syms}).fetchall()
    except Exception as e:
        logger.warning("find_chunks_by_symbol_name failed: %s", e)
        return []
    return [
        {
            "id":               r.id,
            "source_file":      r.source_file,
            "doc_category":     r.doc_category,
            "content":          r.content,
            "chunk_index":      r.chunk_index,
            "parent_symbol_id": r.parent_symbol_id,
            "symbol_name":      r.symbol_name,
            "symbol_kind":      r.symbol_kind,
            "language":         r.language,
        }
        for r in rows
    ]


# ── Outbound 1-hop: who do these symbols call/inherit/implement? ─────────────

def expand_neighbors(
    db: Session,
    seeds: Iterable[str],
    *,
    max_hops: int = 1,
    max_results: int = 200,
) -> list[dict]:
    """Return chunks for `seeds` plus 1-hop CALLS / INHERITS / IMPLEMENTS.

    `max_hops` is currently capped to 1 — multi-hop is rarely used in the
    AGE caller set and adds significant SQL complexity. The signature is
    forward-compatible.

    Returned chunks are de-duplicated by id; no scoring is applied here
    (callers like `graph_retriever` apply their own hop-distance scores).
    """
    seeds = [s for s in seeds if s]
    if not seeds or max_hops < 1:
        return []

    # Step 1: self-match.
    self_rows = find_chunks_by_symbol_name(db, seeds)
    seen: dict[str, dict] = {r["id"]: r for r in self_rows}
    if len(seen) >= max_results:
        return list(seen.values())

    # Step 2: outbound CALLS — chunks whose `calls` array contains any seed.
    # Postgres jsonb has `?` (key existence on object) and `?|` (any of array
    # of texts). For a JSON array of strings, `to_jsonb(symbol) <@ calls`
    # works; we use the `?|` operator on the array form for portability.
    try:
        # `?|` (and jsonb_array_elements) require a jsonb ARRAY on the left.
        # Some rows store `calls` / `implements` / `cross_file_calls` as a jsonb
        # SCALAR (non-code chunks, legacy ingestion), which makes those operators
        # raise "cannot extract elements from a scalar" and ABORT the transaction.
        # Guard each with jsonb_typeof so scalar rows are skipped, not errored.
        sql = text("""
            SELECT id, source_file, doc_category, content, chunk_index,
                   parent_symbol_id, symbol_name, symbol_kind, language
            FROM document_chunks
            WHERE (deprecated IS NOT TRUE)
              AND (
                   (jsonb_typeof(calls::jsonb) = 'array' AND calls::jsonb ?| :seeds)
                OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements(
                           CASE WHEN jsonb_typeof(cross_file_calls::jsonb) = 'array'
                                THEN cross_file_calls::jsonb ELSE '[]'::jsonb END
                       ) e
                       WHERE e->>'callee_symbol' = ANY(:seeds)
                   )
                OR inherits = ANY(:seeds)
                OR (jsonb_typeof(implements::jsonb) = 'array' AND implements::jsonb ?| :seeds)
              )
            LIMIT :limit
        """)
        # SAVEPOINT-scoped: even with the guards above, any residual failure rolls
        # back only this query and leaves the caller's transaction usable.
        with db.begin_nested():
            rows = db.execute(sql, {
                "seeds": list(seeds),
                "limit": max_results,
            }).fetchall()
    except Exception as e:
        logger.warning("expand_neighbors outbound query failed: %s", e)
        rows = []

    for r in rows:
        if r.id in seen:
            continue
        seen[r.id] = {
            "id":               r.id,
            "source_file":      r.source_file,
            "doc_category":     r.doc_category,
            "content":          r.content,
            "chunk_index":      r.chunk_index,
            "parent_symbol_id": r.parent_symbol_id,
            "symbol_name":      r.symbol_name,
            "symbol_kind":      r.symbol_kind,
            "language":         r.language,
        }
        if len(seen) >= max_results:
            break

    return list(seen.values())


# ── Inbound 1-hop: who calls these symbols? ───────────────────────────────────

def inbound_callers(
    db: Session,
    target_symbols: Iterable[str],
    *,
    max_results: int = 500,
) -> list[dict]:
    """Return chunks that CALL any of the target symbols.

    Looks at both `calls` (within-file) and `cross_file_calls` (LSP).
    For impact analysis: pass the symbols you're about to modify and
    receive the blast radius.
    """
    targets = [s for s in target_symbols if s]
    if not targets:
        return []
    try:
        # Same jsonb-scalar guard as expand_neighbors: `?|` and
        # jsonb_array_elements abort the transaction on a scalar operand, so
        # skip non-array rows instead of erroring.
        sql = text("""
            SELECT id, source_file, doc_category, content, chunk_index,
                   parent_symbol_id, symbol_name, symbol_kind, language
            FROM document_chunks
            WHERE (deprecated IS NOT TRUE)
              AND (
                   (jsonb_typeof(calls::jsonb) = 'array' AND calls::jsonb ?| :targets)
                OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements(
                           CASE WHEN jsonb_typeof(cross_file_calls::jsonb) = 'array'
                                THEN cross_file_calls::jsonb ELSE '[]'::jsonb END
                       ) e
                       WHERE e->>'callee_symbol' = ANY(:targets)
                   )
              )
            LIMIT :limit
        """)
        # SAVEPOINT-scoped so a residual failure can't poison the caller's session.
        with db.begin_nested():
            rows = db.execute(sql, {
                "targets": targets,
                "limit": max_results,
            }).fetchall()
    except Exception as e:
        logger.warning("inbound_callers query failed: %s", e)
        return []
    return [
        {
            "id":               r.id,
            "source_file":      r.source_file,
            "doc_category":     r.doc_category,
            "content":          r.content,
            "chunk_index":      r.chunk_index,
            "parent_symbol_id": r.parent_symbol_id,
            "symbol_name":      r.symbol_name,
            "symbol_kind":      r.symbol_kind,
            "language":         r.language,
        }
        for r in rows
    ]
