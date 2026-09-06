# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin endpoints for Apache AGE knowledge-graph management (sub-slice 20a).

Exposes the Slice 19 + 19a + 20 + 21 capabilities to operators:
  - GET  /api/admin/kg/status      — AGE availability + per-label node counts
  - POST /api/admin/kg/initialise  — idempotent label creation
  - POST /api/admin/kg/ingest      — project DocumentChunks + DocCodeLinks
                                     from Postgres into the graph
  - POST /api/admin/kg/impact      — compute blast radius for a target
                                     symbol / change description (Slice 21)

All endpoints require admin auth. Every operation is fail-open: AGE
unreachable → 503 with a structured reason (not a 500 traceback).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.config import settings
from app.core.deps import AdminUser, DbDep
from app.kg import client as kg_client
from app.kg import schema as kg_schema
from app.kg.impact_analyzer import analyze_impact
from app.kg.ingest_from_rag import ingest_from_db
from app.models.document_chunk import DocumentChunk
from app.models.document_link import DocCodeLink

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/kg", tags=["kg-admin"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class KgStatusResponse(BaseModel):
    graph_name: str
    age_available: bool
    chunks_total: int
    doc_links_total: int
    node_counts: dict[str, int] = Field(default_factory=dict)


class KgInitResponse(BaseModel):
    graph_name: str
    vlabels_created: list[str]
    elabels_created: list[str]
    vlabels_skipped: list[str]
    elabels_skipped: list[str]
    failures: list[dict]


class KgIngestResponse(BaseModel):
    graph_name: str
    counts: dict[str, int]
    successes: int
    failures_count: int
    failures: list[dict]


class KgImpactRequest(BaseModel):
    target_chunk_ids: list[str] | None = None
    target_symbols: list[tuple[str, str | None]] | None = None
    change_description: str | None = None
    max_hops: int | None = None


class KgImpactResponse(BaseModel):
    targets: list[str]
    callers: dict[str, int]
    subclasses: dict[str, int]
    implementations: list[str]
    documenting: list[str]
    files_affected: list[str]
    total_impacted: int
    failures: list[dict]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count_nodes_per_label(db) -> dict[str, int]:
    """Run `MATCH (n:Label) RETURN count(n)` for each configured label.

    Swallows per-label failures so one broken label doesn't void the lot.
    Rolls back on error to keep the transaction usable.
    """
    counts: dict[str, int] = {}
    for label in kg_schema.NODE_LABELS:
        cypher = f"MATCH (n:{label}) RETURN count(n) AS cid"
        try:
            rows = kg_client.run_cypher(
                db, cypher, return_cols=[("cid", "agtype")],
            )
            if rows:
                raw = rows[0].get("cid")
                counts[label] = int(raw) if isinstance(raw, (int, float)) else int(str(raw))
        except Exception as e:
            logger.debug("count(%s) failed: %s", label, e)
            try:
                db.rollback()
            except Exception:
                pass
    return counts


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/status", response_model=KgStatusResponse)
def kg_status(db: DbDep, _: AdminUser):
    """Return graph name + AGE availability + per-label node counts + RAG row counts."""
    age_available = kg_client.is_age_available(db)

    chunks_total = db.scalar(select(func.count()).select_from(DocumentChunk)) or 0
    links_total = db.scalar(select(func.count()).select_from(DocCodeLink)) or 0

    node_counts: dict[str, int] = {}
    if age_available:
        node_counts = _count_nodes_per_label(db)

    return KgStatusResponse(
        graph_name=settings.kg_graph_name,
        age_available=age_available,
        chunks_total=int(chunks_total),
        doc_links_total=int(links_total),
        node_counts=node_counts,
    )


@router.post("/initialise", response_model=KgInitResponse)
def kg_initialise(db: DbDep, _: AdminUser):
    """Create any missing vertex/edge labels. Idempotent."""
    if not kg_client.is_age_available(db):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Apache AGE is not reachable — check the Postgres container + migration 0020",
        )
    report = kg_schema.initialise_graph(db)
    return KgInitResponse(**report)


@router.post("/ingest", response_model=KgIngestResponse)
def kg_ingest(db: DbDep, _: AdminUser):
    """Project DocumentChunks + DocCodeLinks into the graph.

    Idempotent (MERGE-based). Long-running on large corpora — consider a
    Celery task in a follow-up. For now we run synchronously and return
    the full report.
    """
    if not kg_client.is_age_available(db):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Apache AGE is not reachable",
        )
    report = ingest_from_db(db)
    return KgIngestResponse(
        graph_name=settings.kg_graph_name,
        counts=report.counts,
        successes=report.total_successes(),
        failures_count=report.total_failures(),
        failures=report.failures,
    )


@router.post("/impact", response_model=KgImpactResponse)
def kg_impact(body: KgImpactRequest, db: DbDep, _: AdminUser):
    """Compute the blast radius for the given targets.

    At least one of `target_chunk_ids`, `target_symbols`, or
    `change_description` must be provided.
    """
    if not (body.target_chunk_ids or body.target_symbols or body.change_description):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide target_chunk_ids, target_symbols, or change_description",
        )
    if not kg_client.is_age_available(db):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Apache AGE is not reachable",
        )
    report = analyze_impact(
        db=db,
        target_chunk_ids=body.target_chunk_ids,
        target_symbols=body.target_symbols,
        change_description=body.change_description,
        max_hops=body.max_hops,
    )
    return KgImpactResponse(
        targets=report.targets,
        callers=report.callers,
        subclasses=report.subclasses,
        implementations=report.implementations,
        documenting=report.documenting,
        files_affected=report.files_affected,
        total_impacted=report.total_impacted(),
        failures=report.failures,
    )
