# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-change-request context cache service.

One row per change request in `change_request_contexts` holds:
  - taxonomy classification (from agents/taxonomy.py)
  - retrieved chunks  (from rag/hybrid_search.py, 3-stage)
  - structured proposals (from agents/proposals_extractor.py)

This cache is the single source of truth consumed by BRD, Tech Spec, XSD,
and Product Kit agents — so every downstream artefact draws from the same
ground truth (same API names, same error codes, same FR numbering).

Typical flow:
  - End of Research stage  → `build(change_id, refresh=True)`  (proactive)
  - First BRD generation   → `get_or_build(change_id)`          (lazy fallback)
  - User clicks "Refresh"  → `build(change_id, refresh=True)`   (explicit)

Safe to call concurrently for different change requests. For the same
change request, the last write wins (no row-level locking); acceptable
because the cache is idempotent given the same research output.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.party_inference import infer_parties
from app.agents.proposals_extractor import (
    extract_proposals,
    score_proposals_confidence,
)
from app.agents.taxonomy import classify as classify_feature
from app.models.change_request import ChangeRequest
from app.models.change_request_context import ChangeRequestContext
from app.models.research import ResearchOutput
from app.models.canvas import ProductCanvas
from app.rag.hybrid_search import build_context_with_taxonomy

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

async def get_or_build(
    change_id: str,
    db: Session,
    *,
    refresh: bool = False,
    pm_confirmed: bool | None = None,
) -> ChangeRequestContext | None:
    """Return the cached context, building it on first use.

    If refresh=True, rebuild from scratch regardless of any cached value.
    If pm_confirmed is None, auto-detect from the latest Clarification row.
    """
    # Auto-detect PM confirmation from clarification status
    if pm_confirmed is None:
        try:
            from app.services.clarification_loader import has_answers
            pm_confirmed = has_answers(change_id, db)
        except Exception:
            pm_confirmed = False

    cached = db.get(ChangeRequestContext, change_id)
    if cached and not refresh:
        logger.debug("context_cache: hit for change=%s (pm_confirmed=%s)", change_id, pm_confirmed)
        # If PM has since answered clarifications, upgrade the confidence tier
        if pm_confirmed and cached.proposals_confidence in ("medium-high", "medium"):
            from app.agents.proposals_extractor import score_proposals_confidence
            cached.proposals_confidence = score_proposals_confidence(
                cached.proposals,
                had_corpus_context=bool(cached.retrieved_chunks),
                pm_confirmed=True,
            )
            db.commit()
            db.refresh(cached)
        return cached

    return await _build(change_id, db, pm_confirmed=pm_confirmed)


async def refresh(change_id: str, db: Session, *, pm_confirmed: bool = False) -> ChangeRequestContext | None:
    """Force a rebuild and return the new cache row."""
    return await _build(change_id, db, pm_confirmed=pm_confirmed)


def invalidate(change_id: str, db: Session) -> bool:
    """Drop any cached row for this change request. Returns True if deleted."""
    row = db.get(ChangeRequestContext, change_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    logger.info("context_cache: invalidated change=%s", change_id)
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Internal: rebuild
# ──────────────────────────────────────────────────────────────────────────────

async def _build(
    change_id: str,
    db: Session,
    *,
    pm_confirmed: bool = False,
) -> ChangeRequestContext | None:
    cr = db.get(ChangeRequest, change_id)
    if cr is None:
        logger.warning("context_cache: change=%s not found", change_id)
        return None

    # 1. Gather the best available feature description: enriched_prompt takes
    #    precedence; fall back to initial_prompt when enrichment hasn't run.
    feature_description = (cr.enhanced_prompt or cr.initial_prompt or "").strip()
    if not feature_description:
        logger.warning("context_cache: change=%s has no prompt — aborting build", change_id)
        return None

    # Optionally augment with the latest research report — gives the proposals
    # extractor a richer set of signals (esp. scalability / compliance notes).
    research = (
        db.query(ResearchOutput)
        .filter(ResearchOutput.change_request_id == change_id)
        .order_by(ResearchOutput.version.desc())
        .first()
    )
    research_version = research.version if research else None
    augmented_description = feature_description
    if research and research.combined_report:
        augmented_description = (
            f"{feature_description}\n\n---\n"
            f"# Research summary\n{research.combined_report[:3000]}"
        )

    # Latest canvas is useful for proposal-shape hints too
    canvas = (
        db.query(ProductCanvas)
        .filter(ProductCanvas.change_request_id == change_id)
        .order_by(ProductCanvas.version.desc())
        .first()
    )
    if canvas and canvas.content:
        augmented_description = f"{augmented_description}\n\n---\n# Product canvas\n{canvas.content[:2000]}"

    # 2. Classify into the network taxonomy
    classification = await classify_feature(feature_description)
    logger.info(
        "context_cache build: change=%s taxonomy=%s (conf=%.2f)",
        change_id, classification.get("primary"), classification.get("confidence", 0.0),
    )

    # 3. 3-stage hybrid retrieval (analogue queries + semantic search)
    chunks, _ = build_context_with_taxonomy(
        feature_description=augmented_description,
        classification=classification,
        db=db,
        per_query_top_k=3,
        overall_top_k=12,
    )

    # Guarantee the API-design source of truth feeds the proposals extractor
    # (ground-truth API names / error codes / FRs). Broad retrieval above can
    # bury api_design_knowledge in a large corpus, so pull it scoped and prepend
    # (deduped) — the extractor caps to the first 8 chunks, so prepending keeps
    # the design corpus in scope.
    from app.models.document_chunk import DocCategory
    from app.rag.retrieval import retrieve as _retrieve
    _api_design_chunks = _retrieve(
        augmented_description, db, top_k=6,
        categories=[DocCategory.API_DESIGN_KNOWLEDGE],
    )
    if _api_design_chunks:
        _seen = {c.get("id") for c in chunks}
        chunks = [c for c in _api_design_chunks if c.get("id") not in _seen] + chunks

    # Strip large float arrays etc. before persisting to JSONB
    persistable_chunks = [
        {
            "id":           c.get("id"),
            "source_file":  c.get("source_file"),
            "doc_category": c.get("doc_category"),
            "chunk_index":  c.get("chunk_index"),
            "score":        c.get("score"),
            "dense_score":  c.get("dense_score"),
            "bm25_score":   c.get("bm25_score"),
            "stage":        c.get("stage"),
            # Truncate content to keep row size reasonable
            "content":      (c.get("content") or "")[:2000],
        }
        for c in chunks
    ]

    # 4. Extract structured proposals
    proposals = await extract_proposals(
        feature_description=augmented_description,
        classification=classification,
        retrieved_chunks=chunks,
    )
    confidence = score_proposals_confidence(
        proposals,
        had_corpus_context=bool(chunks),
        pm_confirmed=pm_confirmed,
    )

    # 4a. Infer canonical the network parties in-scope from the same artifact
    # bundle. Consumed by question_generator.build_scope_signal_questions
    # to pre-check the "parties involved" multi-select clarification
    # (v3 — replaces the 4-yes/no fan-out). Fail-open by design and
    # further gated by `agentic_clarification_infer_parties` so an
    # operator can disable the LLM call without touching code — the
    # question widget still renders, just with all four parties
    # pre-checked as the safe default.
    from app.core.config import settings as _settings
    from app.models.brd import BRD
    party_inf = None
    if getattr(_settings, "agentic_clarification_infer_parties", True):
        _brd = (
            db.query(BRD)
            .filter(BRD.change_request_id == change_id)
            .order_by(BRD.version.desc())
            .first()
        )
        party_inf = await infer_parties(
            enhanced_prompt=feature_description,
            research_report=(research.combined_report if research else ""),
            canvas_content=(canvas.content if canvas else ""),
            brd_content=(_brd.content if _brd else ""),
        )

    # 5. Upsert the row
    row = db.get(ChangeRequestContext, change_id)
    if row is None:
        row = ChangeRequestContext(change_request_id=change_id)
        db.add(row)

    row.taxonomy_primary     = classification.get("primary")
    row.taxonomy_labels      = list(classification.get("labels") or [])
    row.taxonomy_confidence  = float(classification.get("confidence", 0.0))
    row.taxonomy_rationale   = classification.get("rationale")
    row.retrieved_chunks     = persistable_chunks
    row.proposals            = proposals
    row.proposals_confidence = confidence
    row.parties_inference    = party_inf.model_dump() if party_inf else None
    row.last_refreshed_at    = datetime.now(timezone.utc)
    row.source_version       = research_version

    db.commit()
    db.refresh(row)
    logger.info(
        "context_cache: stored change=%s chunks=%d proposals_keys=%s confidence=%s",
        change_id, len(persistable_chunks),
        list((proposals or {}).keys())[:8],
        confidence,
    )
    return row
