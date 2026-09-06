# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Impact analyzer — compute the blast radius of a proposed code change.

Slice 21. Standalone module, no production wiring this slice.

Contrast with Slice 20's `graph_retriever`:
  - graph_retriever does OUTBOUND traversal: "what does my query talk about?"
  - impact_analyzer does INBOUND traversal: "who depends on the symbols I'm
    about to modify?"

Four classes of inbound edges are checked per target:
  - Callers         (inbound CALLS, multi-hop BFS up to `max_hops`)
  - Subclasses      (inbound INHERITS, multi-hop — subclass-of-subclass)
  - Implementations (inbound IMPLEMENTS, 1-hop — interfaces rarely chain)
  - Documenting     (inbound DESCRIBES from DocChunk, 1-hop — docs that
                     need to be re-verified after the change)

Target resolution accepts (in priority order):
  - explicit `target_chunk_ids` (skip resolution)
  - `target_symbols` as list of (symbol_name, source_file) tuples
  - `change_description` — seeds extracted via Slice 20's extractor, each
    seed resolved to zero-or-more chunk_ids by symbol_name lookup

Fail-open at every boundary (empty input, AGE unavailable, Cypher errors,
transaction aborts). Per-query Postgres rollback prevents the hydration
SELECT from hitting InFailedSqlTransaction — same guard as Slice 20.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.kg import client as kg_client
from app.rag.graph_retriever import _extract_query_seeds

logger = logging.getLogger(__name__)

_ESC = kg_client.escape_cypher_literal


# ──────────────────────────────────────────────────────────────────────────────
# Cypher builders (pure)
# ──────────────────────────────────────────────────────────────────────────────

def build_resolve_symbol_cypher(symbol_name: str, source_file: str | None = None) -> str:
    """Return chunk_ids for nodes matching symbol_name (+ optional source_file)."""
    if source_file:
        return (
            f"MATCH (n) WHERE n.symbol_name = {_ESC(symbol_name)} "
            f"AND n.source_file = {_ESC(source_file)}\n"
            f"RETURN DISTINCT n.chunk_id AS cid"
        )
    return (
        f"MATCH (n) WHERE n.symbol_name = {_ESC(symbol_name)}\n"
        f"RETURN DISTINCT n.chunk_id AS cid"
    )


def build_callers_cypher(target_chunk_id: str) -> str:
    """1-hop inbound CALLS: who calls the target?"""
    return (
        f"MATCH (x:Function)-[r:CALLS]->(t) "
        f"WHERE t.chunk_id = {_ESC(target_chunk_id)}\n"
        f"RETURN DISTINCT x.chunk_id AS cid"
    )


def build_subclasses_cypher(target_chunk_id: str) -> str:
    """1-hop inbound INHERITS: who extends the target?"""
    return (
        f"MATCH (x:Class)-[r:INHERITS]->(t) "
        f"WHERE t.chunk_id = {_ESC(target_chunk_id)}\n"
        f"RETURN DISTINCT x.chunk_id AS cid"
    )


def build_implementations_cypher(target_chunk_id: str) -> str:
    """1-hop inbound IMPLEMENTS: who implements the target (when interface)?"""
    return (
        f"MATCH (x:Class)-[r:IMPLEMENTS]->(t) "
        f"WHERE t.chunk_id = {_ESC(target_chunk_id)}\n"
        f"RETURN DISTINCT x.chunk_id AS cid"
    )


def build_documenting_cypher(target_chunk_id: str) -> str:
    """1-hop inbound DESCRIBES: which DocChunks describe the target?"""
    return (
        f"MATCH (x:DocChunk)-[r:DESCRIBES]->(t) "
        f"WHERE t.chunk_id = {_ESC(target_chunk_id)}\n"
        f"RETURN DISTINCT x.chunk_id AS cid"
    )


def build_source_files_cypher(chunk_ids: list[str]) -> str:
    """Collect source_file for a set of chunk_ids (any label)."""
    if not chunk_ids:
        raise ValueError("chunk_ids must be non-empty")
    literal_list = "[" + ", ".join(_ESC(c) for c in chunk_ids) + "]"
    return (
        f"MATCH (n) WHERE n.chunk_id IN {literal_list}\n"
        f"RETURN DISTINCT n.source_file AS source_file"
    )


# ──────────────────────────────────────────────────────────────────────────────
# BFS helpers (pure)
# ──────────────────────────────────────────────────────────────────────────────

def _bfs_inbound(
    seeds: Iterable[str],
    *,
    cypher_builder: Callable[[str], str],
    run_cypher_fn: Callable[[str], list[dict]],
    max_hops: int,
    excluded: set[str] | None = None,
) -> dict[str, int]:
    """BFS inbound from each seed, up to max_hops. Returns {chunk_id: min_hop}.

    `cypher_builder(chunk_id) -> str` must produce a Cypher that returns rows
    with key `cid` (matching our standard return_cols shape).

    `excluded` is a set of chunk_ids to never include in the output (typically
    the target chunk_ids themselves so they don't show up as their own
    callers through a cycle).
    """
    excluded = set(excluded or ())
    visited: dict[str, int] = {}
    frontier: set[str] = set(seeds)

    for hop in range(1, max_hops + 1):
        next_frontier: set[str] = set()
        for tid in frontier:
            try:
                rows = run_cypher_fn(cypher_builder(tid)) or []
            except Exception as e:
                logger.debug("impact BFS cypher failed (hop=%d, target=%s): %s", hop, tid, e)
                continue
            for row in rows:
                cid = row.get("cid")
                if not cid or not isinstance(cid, str):
                    continue
                if cid in excluded:
                    continue
                if cid in visited:
                    continue
                visited[cid] = hop
                next_frontier.add(cid)
        if not next_frontier:
            break
        frontier = next_frontier

    return visited


def _one_hop(
    seeds: Iterable[str],
    *,
    cypher_builder: Callable[[str], str],
    run_cypher_fn: Callable[[str], list[dict]],
    excluded: set[str] | None = None,
) -> set[str]:
    """One-hop inbound union across seeds. Returns dedup'd chunk_id set."""
    excluded = set(excluded or ())
    collected: set[str] = set()
    for tid in seeds:
        try:
            rows = run_cypher_fn(cypher_builder(tid)) or []
        except Exception as e:
            logger.debug("impact one-hop cypher failed (target=%s): %s", tid, e)
            continue
        for row in rows:
            cid = row.get("cid")
            if cid and isinstance(cid, str) and cid not in excluded:
                collected.add(cid)
    return collected


# ──────────────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ImpactReport:
    """Blast-radius report.

    `callers` and `subclasses` are `{chunk_id: hop_distance}` (min hop where
    the chunk first appeared). `implementations` and `documenting` are
    sets/lists of chunk_ids (1-hop only, no hop distance needed).

    `files_affected` is the union of source_files across targets + all
    impacted chunks — the practical "what files might need review" set.

    `failures` captures per-stage exceptions (e.g., "AGE unavailable",
    "all BFS cyphers errored for target X"). Stage-level, not per-query,
    since per-query failures are swallowed inside BFS.
    """
    targets: list[str] = field(default_factory=list)
    callers: dict[str, int] = field(default_factory=dict)
    subclasses: dict[str, int] = field(default_factory=dict)
    implementations: list[str] = field(default_factory=list)
    documenting: list[str] = field(default_factory=list)
    files_affected: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)

    def total_impacted(self) -> int:
        """Total distinct impacted chunks (excluding targets)."""
        impacted = set(self.callers) | set(self.subclasses) | set(self.implementations) | set(self.documenting)
        return len(impacted)

    def impacted_chunk_ids(self) -> list[str]:
        """Union of all impacted chunk_ids (excluding targets), sorted."""
        impacted = set(self.callers) | set(self.subclasses) | set(self.implementations) | set(self.documenting)
        return sorted(impacted)


# ──────────────────────────────────────────────────────────────────────────────
# Target resolution
# ──────────────────────────────────────────────────────────────────────────────

def resolve_targets(
    *,
    target_chunk_ids: list[str] | None = None,
    target_symbols: list[tuple[str, str | None]] | None = None,
    change_description: str | None = None,
    run_cypher_fn: Callable[[str], list[dict]] | None = None,
) -> list[str]:
    """Resolve inputs to a deduplicated list of target chunk_ids.

    Priority: explicit chunk_ids → symbol tuples → description-seed extraction.
    When symbols or description are used, `run_cypher_fn` is required (the
    resolution is itself Cypher-driven).

    Returns an empty list if nothing resolves — caller decides whether that's
    an error condition.
    """
    if target_chunk_ids:
        # dedup preserving order
        seen: set[str] = set()
        out: list[str] = []
        for cid in target_chunk_ids:
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out

    if run_cypher_fn is None:
        return []

    resolved: list[str] = []
    seen_ids: set[str] = set()

    def _add(cid: str) -> None:
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            resolved.append(cid)

    if target_symbols:
        for sym_name, src in target_symbols:
            try:
                rows = run_cypher_fn(build_resolve_symbol_cypher(sym_name, src)) or []
            except Exception as e:
                logger.debug("target resolve cypher failed (sym=%s, src=%s): %s", sym_name, src, e)
                continue
            for row in rows:
                cid = row.get("cid")
                if isinstance(cid, str):
                    _add(cid)

    elif change_description:
        seeds = _extract_query_seeds(change_description)
        for seed in seeds:
            try:
                rows = run_cypher_fn(build_resolve_symbol_cypher(seed)) or []
            except Exception as e:
                logger.debug("description-seed resolve cypher failed (seed=%s): %s", seed, e)
                continue
            for row in rows:
                cid = row.get("cid")
                if isinstance(cid, str):
                    _add(cid)

    return resolved


# ──────────────────────────────────────────────────────────────────────────────
# Main entry
# ──────────────────────────────────────────────────────────────────────────────

def analyze_impact(
    *,
    db: Session,
    target_chunk_ids: list[str] | None = None,
    target_symbols: list[tuple[str, str | None]] | None = None,
    change_description: str | None = None,
    max_hops: int | None = None,
    graph_name: str | None = None,
) -> ImpactReport:
    """Compute the blast radius of a proposed change.

    Fail-open at every stage:
      - AGE unavailable → empty report with single failure entry
      - Target resolution yields nothing → empty report
      - Per-query Cypher error → swallowed inside BFS/one_hop helpers
      - Postgres transaction aborts from a bad Cypher → rolled back
        between queries via the `_run` wrapper
    """
    report = ImpactReport()

    if not kg_client.is_age_available(db):
        report.failures.append({
            "stage": "age_unavailable",
            "error": "is_age_available() returned False",
        })
        return report

    graph_name = graph_name or settings.kg_graph_name
    max_hops = max_hops if max_hops is not None else settings.impact_max_hops

    def _run(cypher: str) -> list[dict]:
        try:
            return kg_client.run_cypher(
                db, cypher,
                graph_name=graph_name,
                return_cols=[("cid", "agtype")],
            )
        except Exception as e:
            logger.debug("impact_analyzer: cypher failed, rolling back: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
            return []

    def _run_named_col(cypher: str, col: str) -> list[dict]:
        try:
            return kg_client.run_cypher(
                db, cypher,
                graph_name=graph_name,
                return_cols=[(col, "agtype")],
            )
        except Exception as e:
            logger.debug("impact_analyzer: cypher failed, rolling back: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
            return []

    # 1) Resolve targets
    targets = resolve_targets(
        target_chunk_ids=target_chunk_ids,
        target_symbols=target_symbols,
        change_description=change_description,
        run_cypher_fn=_run,
    )
    if not targets:
        report.failures.append({
            "stage": "target_resolution",
            "error": "no targets resolved from provided inputs",
        })
        return report
    report.targets = targets
    target_set = set(targets)

    # 2) BFS callers (multi-hop)
    report.callers = _bfs_inbound(
        targets,
        cypher_builder=build_callers_cypher,
        run_cypher_fn=_run,
        max_hops=max_hops,
        excluded=target_set,
    )

    # 3) BFS subclasses (multi-hop)
    report.subclasses = _bfs_inbound(
        targets,
        cypher_builder=build_subclasses_cypher,
        run_cypher_fn=_run,
        max_hops=max_hops,
        excluded=target_set,
    )

    # 4) 1-hop implementations
    impl = _one_hop(
        targets,
        cypher_builder=build_implementations_cypher,
        run_cypher_fn=_run,
        excluded=target_set,
    )
    report.implementations = sorted(impl)

    # 5) 1-hop documenting DocChunks
    docs = _one_hop(
        targets,
        cypher_builder=build_documenting_cypher,
        run_cypher_fn=_run,
        excluded=target_set,
    )
    report.documenting = sorted(docs)

    # 6) files_affected — union across all chunks (targets + impacted)
    all_chunks = list(target_set | set(report.callers) | set(report.subclasses)
                      | set(report.implementations) | set(report.documenting))
    if all_chunks:
        try:
            rows = _run_named_col(build_source_files_cypher(all_chunks), "source_file")
        except Exception as e:
            rows = []
            report.failures.append({
                "stage": "files_affected",
                "error": str(e),
            })
        files: set[str] = set()
        for row in rows:
            sf = row.get("source_file")
            if isinstance(sf, str) and sf:
                files.add(sf)
        report.files_affected = sorted(files)

    return report
