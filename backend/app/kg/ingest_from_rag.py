# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Project existing RAG data (document_chunks + doc_code_links) into the
Apache AGE knowledge graph.

Sub-slice 19a. This is a standalone **post-ingest** pass — not wired into
the production agent pipeline. Call `ingest_from_db(db)` from an admin
endpoint, Celery task, or REPL after a regular ingestion run.

Pipeline shape:
  1. pure builders                   → Cypher strings for MERGE/MATCH ops
  2. pure planner                    → plan_ingest_from_chunks(chunks, links)
                                       returns a list of (kind, cypher) tuples
  3. side-effecting executor         → execute_plan(plan, run_cypher_fn=...)
                                       DI-friendly; used in tests with a fake
  4. convenience full pipeline       → ingest_from_db(db)
                                       loads chunks/links from Postgres + runs

All writes use MERGE so re-runs are idempotent. Missing-target MATCHes
silently drop the edge (logged, counted) — acceptable for v0; Slice 20
(graph retriever) will tolerate sparse edges.

Node coverage this sub-slice:
  - Document   (per source_file for narrative docs)
  - DocChunk   (per doc chunk id)
  - File       (per code source_file)
  - Class      (chunks with symbol_kind in {class, interface})
  - Function   (chunks with symbol_kind in {method, function, constructor})

Edge coverage this sub-slice:
  - DESCRIBES  (DocChunk → Class|Function) from doc_code_links
  - CALLS      (Function → Function, within-file by name) from `calls` column
  - INHERITS   (Class → Class, within-file by name) from `inherits` column
  - IMPLEMENTS (Class → Class, within-file by name) from `implements` column

Not populated here (no data yet, or future sub-slice):
  Repo, Module, Endpoint, Schema, Service, Feature, Capability, Requirement,
  ADR, Ticket, Person, Team; EXPOSES, CONSUMES, PART_OF, OWNED_BY,
  DEPENDS_ON, DEPRECATES, CHANGED_IN, INTRODUCED_IN, BROKEN_BY, EXAMPLE_OF.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.error_taxonomy import client_safe_detail
from app.kg import client as kg_client

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Categorisation
# ──────────────────────────────────────────────────────────────────────────────

CODE_KINDS_CLASS = frozenset({"class", "interface"})
CODE_KINDS_FUNCTION = frozenset({"method", "function", "constructor"})


def classify_chunk(chunk: dict) -> str:
    """Return one of 'class' | 'function' | 'file_level' | 'doc'."""
    kind = (chunk.get("symbol_kind") or "").lower()
    if kind in CODE_KINDS_CLASS:
        return "class"
    if kind in CODE_KINDS_FUNCTION:
        return "function"
    # File-level chunk: explicit 'file' marker, or code doc_category with no
    # symbol (e.g. the regex chunker's whole-file output).
    if kind == "file" or (chunk.get("doc_category") == "java_source" and not kind):
        return "file_level"
    if chunk.get("language") and not kind:
        return "file_level"
    return "doc"


# ──────────────────────────────────────────────────────────────────────────────
# Pure Cypher builders
# ──────────────────────────────────────────────────────────────────────────────

_ESC = kg_client.escape_cypher_literal


def _props_to_cypher_map(props: dict[str, Any]) -> str:
    """Emit a Cypher map literal `{k: v, ...}` from a dict.

    None values are dropped (AGE treats missing keys as "not set", whereas
    `null` in a MERGE key would prevent matching). Empty map returns `{}`.
    """
    parts = []
    for k, v in props.items():
        if v is None:
            continue
        parts.append(f"{k}: {_ESC(v)}")
    return "{" + ", ".join(parts) + "}"


def _props_to_set_clauses(var: str, props: dict[str, Any]) -> str:
    """Emit `SET var.k = v, var.k2 = v2` clauses. None values dropped."""
    clauses = []
    for k, v in props.items():
        if v is None:
            continue
        clauses.append(f"{var}.{k} = {_ESC(v)}")
    if not clauses:
        return ""
    return "SET " + ", ".join(clauses)


def build_merge_node_cypher(
    label: str,
    *,
    key_props: dict[str, Any],
    set_props: dict[str, Any] | None = None,
    var: str = "n",
) -> str:
    """`MERGE (var:Label {key: val}) SET var.prop = val ...`.

    key_props: the unique-identity subset used inside MERGE. Required and
               must have ≥ 1 non-None value (otherwise MERGE would match
               every node and silently duplicate properties).
    set_props: remaining properties applied post-match via SET. Optional.
    """
    if not label or not label.isidentifier():
        raise ValueError(f"invalid node label: {label!r}")
    non_null_keys = {k: v for k, v in key_props.items() if v is not None}
    if not non_null_keys:
        raise ValueError("key_props must have at least one non-None value")
    map_lit = _props_to_cypher_map(non_null_keys)
    cypher = f"MERGE ({var}:{label} {map_lit})"
    if set_props:
        set_clause = _props_to_set_clauses(var, set_props)
        if set_clause:
            cypher += "\n" + set_clause
    return cypher


def build_merge_edge_cypher(
    *,
    src_label: str,
    src_key: dict[str, Any],
    dst_label: str,
    dst_key: dict[str, Any],
    edge_label: str,
    edge_props: dict[str, Any] | None = None,
) -> str:
    """`MATCH (a:L {..}) MATCH (b:L2 {..}) MERGE (a)-[r:EDGE]->(b) SET r.x = ..`.

    Missing-target MATCHes return zero rows → edge silently not created.
    Callers that need a hard-fail lookup should use build_merge_node_cypher
    to pre-materialise endpoints.
    """
    for lbl in (src_label, dst_label, edge_label):
        if not lbl or not lbl.isidentifier():
            raise ValueError(f"invalid label: {lbl!r}")
    if not src_key or not dst_key:
        raise ValueError("src_key and dst_key must be non-empty dicts")
    src_map = _props_to_cypher_map({k: v for k, v in src_key.items() if v is not None})
    dst_map = _props_to_cypher_map({k: v for k, v in dst_key.items() if v is not None})
    cypher = (
        f"MATCH (a:{src_label} {src_map})\n"
        f"MATCH (b:{dst_label} {dst_map})\n"
        f"MERGE (a)-[r:{edge_label}]->(b)"
    )
    if edge_props:
        set_clause = _props_to_set_clauses("r", edge_props)
        if set_clause:
            cypher += "\n" + set_clause
    return cypher


# ──────────────────────────────────────────────────────────────────────────────
# Node builders (return cypher strings; pure)
# ──────────────────────────────────────────────────────────────────────────────

def build_document_node(chunk: dict) -> str:
    """MERGE Document keyed on source_file."""
    return build_merge_node_cypher(
        "Document",
        key_props={"source_file": chunk.get("source_file")},
        set_props={
            "doc_category":   chunk.get("doc_category"),
            "product_area":   chunk.get("product_area"),
            "author":         chunk.get("author"),
            "last_modified":  _isoformat(chunk.get("last_modified")),
            "deprecated":     bool(chunk.get("deprecated")) if chunk.get("deprecated") is not None else None,
        },
    )


def build_doc_chunk_node(chunk: dict) -> str:
    """MERGE DocChunk keyed on chunk_id."""
    return build_merge_node_cypher(
        "DocChunk",
        key_props={"chunk_id": chunk["id"]},
        set_props={
            "source_file":       chunk.get("source_file"),
            "doc_category":      chunk.get("doc_category"),
            "chunk_index":       chunk.get("chunk_index"),
            "title_breadcrumb":  chunk.get("title_breadcrumb"),
            "freshness_score":   chunk.get("freshness_score"),
        },
    )


def build_file_node(chunk: dict) -> str:
    """MERGE File keyed on source_file."""
    return build_merge_node_cypher(
        "File",
        key_props={"source_file": chunk.get("source_file")},
        set_props={
            "language":  chunk.get("language"),
            "imports":   chunk.get("imports"),
        },
    )


def build_class_node(chunk: dict) -> str:
    """MERGE Class keyed on chunk_id."""
    return build_merge_node_cypher(
        "Class",
        key_props={"chunk_id": chunk["id"]},
        set_props={
            "symbol_name":  chunk.get("symbol_name"),
            "source_file":  chunk.get("source_file"),
            "signature":    chunk.get("signature"),
            "line_start":   chunk.get("line_start"),
            "line_end":     chunk.get("line_end"),
            "language":     chunk.get("language"),
        },
    )


def build_function_node(chunk: dict) -> str:
    """MERGE Function keyed on chunk_id."""
    return build_merge_node_cypher(
        "Function",
        key_props={"chunk_id": chunk["id"]},
        set_props={
            "symbol_name":       chunk.get("symbol_name"),
            "source_file":       chunk.get("source_file"),
            "signature":         chunk.get("signature"),
            "line_start":        chunk.get("line_start"),
            "line_end":          chunk.get("line_end"),
            "language":          chunk.get("language"),
            "parent_symbol_id":  chunk.get("parent_symbol_id"),
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Edge builders
# ──────────────────────────────────────────────────────────────────────────────

def build_describes_edge(doc_chunk_id: str, symbol_chunk_id: str, confidence: float) -> str:
    """DESCRIBES edge: DocChunk → any labeled node with chunk_id = symbol_chunk_id.

    We don't know a priori whether the target is a Class or Function — the
    simplest portable approach is to MATCH without a label on the target.
    AGE allows that: `MATCH (b {chunk_id: 'x'})`.
    """
    src_map = _props_to_cypher_map({"chunk_id": doc_chunk_id})
    dst_map = _props_to_cypher_map({"chunk_id": symbol_chunk_id})
    props_clause = _props_to_set_clauses("r", {"confidence": confidence})
    cypher = (
        f"MATCH (a:DocChunk {src_map})\n"
        f"MATCH (b {dst_map})\n"
        f"MERGE (a)-[r:DESCRIBES]->(b)"
    )
    if props_clause:
        cypher += "\n" + props_clause
    return cypher


def build_calls_edge_within_file(
    caller_chunk_id: str,
    callee_symbol_name: str,
    source_file: str,
) -> str:
    """CALLS edge within one file: caller Function → callee Function by name."""
    return build_merge_edge_cypher(
        src_label="Function",
        src_key={"chunk_id": caller_chunk_id},
        dst_label="Function",
        dst_key={"source_file": source_file, "symbol_name": callee_symbol_name},
        edge_label="CALLS",
    )


def build_inherits_edge_within_file(
    child_chunk_id: str,
    parent_symbol_name: str,
    source_file: str,
) -> str:
    """INHERITS edge within one file: child Class → parent Class by name.

    Cross-file inheritance is silently dropped in v0 (no global symbol
    index). Slice 22+ language-specific LSPs are expected to backfill.
    """
    return build_merge_edge_cypher(
        src_label="Class",
        src_key={"chunk_id": child_chunk_id},
        dst_label="Class",
        dst_key={"source_file": source_file, "symbol_name": parent_symbol_name},
        edge_label="INHERITS",
    )


def build_implements_edge_within_file(
    class_chunk_id: str,
    interface_symbol_name: str,
    source_file: str,
) -> str:
    """IMPLEMENTS edge within one file: Class → interface Class by name."""
    return build_merge_edge_cypher(
        src_label="Class",
        src_key={"chunk_id": class_chunk_id},
        dst_label="Class",
        dst_key={"source_file": source_file, "symbol_name": interface_symbol_name},
        edge_label="IMPLEMENTS",
    )


def build_cross_file_call_edge(
    caller_chunk_id: str,
    callee_path: str,
    callee_symbol: str,
) -> str:
    """CALLS edge across files: caller Function → callee resolved by
    (source_file, symbol_name).

    Sub-slice 23a — projects each entry of `chunk.cross_file_calls` (set by
    Slice 23's Python LSP) as a graph edge. Uses the same `CALLS` label as
    within-file calls so graph_retriever + impact_analyzer pick them up
    without any code changes — the only difference is whether the resolved
    callee lives in the same file or a different one.

    Callee MATCH omits the label so it works whether the resolved target is
    a `Function` (the common case) or a class-level callable (`__init__`,
    `__call__`, etc. land on `Class` nodes).
    """
    src_map = _props_to_cypher_map({"chunk_id": caller_chunk_id})
    dst_map = _props_to_cypher_map({
        "source_file": callee_path,
        "symbol_name": callee_symbol,
    })
    return (
        f"MATCH (a:Function {src_map})\n"
        f"MATCH (b {dst_map})\n"
        f"MERGE (a)-[r:CALLS]->(b)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Planner (pure)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class IngestPlan:
    """Ordered sequence of (operation_kind, cypher) pairs.

    Ordering matters: all node MERGEs run before any edge MATCH+MERGE so
    edge MATCHes can find their endpoints. Within nodes and within edges,
    order is insertion-order for deterministic testing.

    `operation_kind` is a short tag like 'document' / 'class' / 'calls' that
    the executor uses to tally the IngestReport.
    """
    operations: list[tuple[str, str]] = field(default_factory=list)

    def add(self, kind: str, cypher: str) -> None:
        self.operations.append((kind, cypher))

    def __len__(self) -> int:
        return len(self.operations)


def plan_ingest_from_chunks(
    chunks: Iterable[dict],
    links: Iterable[dict] | None = None,
) -> IngestPlan:
    """Build an IngestPlan from chunk + link dicts.

    Each chunk dict must have an `id` key. Optional keys per node-type are
    handled via `.get()` — extra keys are ignored.

    Each link dict must have `doc_chunk_id`, `symbol_chunk_id`, `confidence`.
    """
    plan = IngestPlan()

    # First pass: node operations, deduped by (label, key).
    seen_docs: set[str] = set()
    seen_files: set[str] = set()
    class_chunks: list[dict] = []      # kept for Phase 2 (edges)
    function_chunks: list[dict] = []

    for chunk in chunks:
        if "id" not in chunk:
            continue
        role = classify_chunk(chunk)
        sf = chunk.get("source_file")

        if role == "doc":
            if sf and sf not in seen_docs:
                plan.add("document", build_document_node(chunk))
                seen_docs.add(sf)
            plan.add("doc_chunk", build_doc_chunk_node(chunk))

        elif role == "file_level":
            if sf and sf not in seen_files:
                plan.add("file", build_file_node(chunk))
                seen_files.add(sf)

        elif role == "class":
            if sf and sf not in seen_files:
                plan.add("file", build_file_node(chunk))
                seen_files.add(sf)
            plan.add("class", build_class_node(chunk))
            class_chunks.append(chunk)

        elif role == "function":
            if sf and sf not in seen_files:
                plan.add("file", build_file_node(chunk))
                seen_files.add(sf)
            plan.add("function", build_function_node(chunk))
            function_chunks.append(chunk)

    # Second pass: edges. Order: within-file code edges first, then DESCRIBES.

    # INHERITS (Class -> Class by name in same file)
    for cls in class_chunks:
        parent = cls.get("inherits")
        sf = cls.get("source_file")
        if parent and sf:
            plan.add(
                "inherits",
                build_inherits_edge_within_file(cls["id"], parent, sf),
            )

    # IMPLEMENTS (Class -> Class by name in same file, per-interface)
    for cls in class_chunks:
        ifaces = cls.get("implements") or []
        sf = cls.get("source_file")
        if not sf:
            continue
        for iface in ifaces:
            if iface:
                plan.add(
                    "implements",
                    build_implements_edge_within_file(cls["id"], iface, sf),
                )

    # CALLS (Function -> Function by name in same file)
    for fn in function_chunks:
        callees = fn.get("calls") or []
        sf = fn.get("source_file")
        if not sf:
            continue
        for callee in callees:
            if callee:
                plan.add(
                    "calls",
                    build_calls_edge_within_file(fn["id"], callee, sf),
                )

    # Sub-slice 23a — Cross-file CALLS from LSP-resolved entries. Same
    # `CALLS` edge label so graph_retriever / impact_analyzer pick them up
    # without changes. Each entry: `{"callee_symbol", "callee_path", ...}`.
    # Skips entries lacking a callee_path or callee_symbol — incomplete
    # resolutions silently dropped (LSP partial outputs are common).
    for fn in function_chunks:
        cross = fn.get("cross_file_calls") or []
        for entry in cross:
            if not isinstance(entry, dict):
                continue
            callee_path = entry.get("callee_path")
            callee_symbol = entry.get("callee_symbol")
            if not callee_path or not callee_symbol:
                continue
            plan.add(
                "cross_file_calls",
                build_cross_file_call_edge(
                    fn["id"], callee_path, callee_symbol,
                ),
            )

    # DESCRIBES (DocChunk -> target chunk)
    for link in (links or []):
        doc_id = link.get("doc_chunk_id")
        sym_id = link.get("symbol_chunk_id")
        conf = link.get("confidence")
        if doc_id and sym_id and conf is not None:
            plan.add(
                "describes",
                build_describes_edge(doc_id, sym_id, float(conf)),
            )

    return plan


# ──────────────────────────────────────────────────────────────────────────────
# Executor (side-effecting, DI)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class IngestReport:
    """Tally of execute_plan outcomes.

    - counts: operation_kind → successful cypher invocations
    - failures: per-op exception info for triage
    """
    counts: dict[str, int] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)

    def total_successes(self) -> int:
        return sum(self.counts.values())

    def total_failures(self) -> int:
        return len(self.failures)


def execute_plan(
    plan: IngestPlan,
    *,
    run_cypher_fn: Callable[[str], Any],
) -> IngestReport:
    """Run every op in `plan` via `run_cypher_fn`. Never raises.

    Each op's exception is caught and recorded in the report; the rest
    continue. The injected `run_cypher_fn` takes a single cypher string —
    its return value is ignored (we only care about "did it throw").
    """
    report = IngestReport()
    for kind, cypher in plan.operations:
        try:
            run_cypher_fn(cypher)
        except Exception as e:
            # SCR #6: `report.failures` is returned verbatim by
            # POST /api/kg/ingest (KgIngestResponse.failures), so BOTH fields
            # here were client-visible:
            #   * str(e) renders the psycopg2/AGE error with the statement;
            #   * `cypher_excerpt` shipped 200 characters of generated query
            #     UNCONDITIONALLY — graph labels, property names and the
            #     interpolated source_file path.
            # The excerpt is a debugging aid for operators, not something a
            # caller can act on, so it moves to the log line and the response
            # keeps only the operation kind plus a safe label.
            logger.warning(
                "kg ingest op failed: kind=%s error=%s cypher=%s",
                kind, e, cypher[:200],
            )
            report.failures.append({
                "kind": kind,
                "error": client_safe_detail(e),
                # Key retained (callers and tests index it) but always empty:
                # the query text itself is the disclosure.
                "cypher_excerpt": "",
            })
            continue
        report.counts[kind] = report.counts.get(kind, 0) + 1
    return report


# ──────────────────────────────────────────────────────────────────────────────
# Full pipeline (convenience; side-effecting)
# ──────────────────────────────────────────────────────────────────────────────

def ingest_from_db(
    db: Session,
    *,
    graph_name: str | None = None,
) -> IngestReport:
    """Load chunks + links from Postgres and project them into the AGE graph.

    Reads (read-only) from `document_chunks` and `doc_code_links`. Writes
    only to the AGE graph.

    Fail-open: if AGE isn't reachable, returns an empty report with a
    single 'age_unavailable' failure entry instead of raising.
    """
    from sqlalchemy import select

    from app.models.document_chunk import DocumentChunk
    from app.models.document_link import DocCodeLink

    graph_name = graph_name or settings.kg_graph_name

    if not kg_client.is_age_available(db):
        logger.warning("KG ingestion skipped — Apache AGE not reachable")
        report = IngestReport()
        report.failures.append({
            "kind": "age_unavailable",
            "error": "is_age_available() returned False",
            "cypher_excerpt": "",
        })
        return report

    chunk_rows = db.execute(select(DocumentChunk)).scalars().all()
    chunks = [_chunk_to_dict(c) for c in chunk_rows]
    logger.info("KG ingest: loaded %d document_chunks", len(chunks))

    link_rows = db.execute(select(DocCodeLink)).scalars().all()
    links = [
        {
            "doc_chunk_id":     l.doc_chunk_id,
            "symbol_chunk_id":  l.symbol_chunk_id,
            "confidence":       l.confidence,
        }
        for l in link_rows
    ]
    logger.info("KG ingest: loaded %d doc_code_links", len(links))

    plan = plan_ingest_from_chunks(chunks, links)
    logger.info("KG ingest: planned %d operations", len(plan))

    def _run(cypher: str) -> None:
        kg_client.run_cypher(db, cypher, graph_name=graph_name)

    report = execute_plan(plan, run_cypher_fn=_run)
    try:
        db.commit()
    except Exception as e:
        logger.warning("KG ingest: commit failed: %s", e)
        db.rollback()

    logger.info(
        "KG ingest: complete. successes=%d failures=%d by_kind=%s",
        report.total_successes(), report.total_failures(), report.counts,
    )
    return report


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _chunk_to_dict(c: Any) -> dict:
    """Project a DocumentChunk ORM row to the dict shape the planner expects."""
    return {
        "id":                 c.id,
        "source_file":        c.source_file,
        "doc_category":       c.doc_category,
        "chunk_index":        c.chunk_index,
        "symbol_kind":        c.symbol_kind,
        "symbol_name":        c.symbol_name,
        "signature":          c.signature,
        "line_start":         c.line_start,
        "line_end":           c.line_end,
        "language":           c.language,
        "parent_symbol_id":   c.parent_symbol_id,
        "title_breadcrumb":   c.title_breadcrumb,
        "author":             c.author,
        "product_area":       c.product_area,
        "last_modified":      c.last_modified,
        "freshness_score":    c.freshness_score,
        "deprecated":         c.deprecated,
        "imports":            c.imports,
        "inherits":           c.inherits,
        "implements":         c.implements,
        "calls":              c.calls,
        "called_by":          c.called_by,
        # Sub-slice 23a — cross-file calls from Python LSP. Same JSON shape
        # `[{"callee_symbol", "callee_path", "line", "language"}, ...]`.
        "cross_file_calls":   getattr(c, "cross_file_calls", None),
    }


def _isoformat(dt: Any) -> str | None:
    """Render a datetime (or None) to ISO-8601 for Cypher storage."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        return dt.isoformat()
    except Exception:
        return None
