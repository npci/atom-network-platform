# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Feasibility resolver API — authority-side recommendation for partner messages.

Endpoints:
  GET  /changes/{id}/resolver-recommendation?partner_id=X   — latest recommendation for this partner
  GET  /changes/{id}/resolver-recommendations?partner_id=X  — all of them, newest first
  POST /changes/{id}/resolve/{a2a_message_id}              — manual (re-)run of the resolver
"""
import json
import logging

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.agents.feasibility_resolver import auto_resolve_background
from app.core.deps import AdminUser, DbDep
from app.models.resolver_recommendation import ResolverRecommendation

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/changes", tags=["resolver"])


@router.get("/{change_id}/resolver-recommendation")
def get_latest_recommendation(
    change_id: str,
    partner_id: str = Query(...),
    db: DbDep = None,
    _: AdminUser = None,
):
    """Latest resolver recommendation for a (change, partner) pair. The Phase C
    UI calls this to render the resolver card above the composer."""
    row = db.execute(
        select(ResolverRecommendation)
        .where(
            ResolverRecommendation.change_request_id == change_id,
            ResolverRecommendation.partner_id == partner_id,
        )
        .order_by(ResolverRecommendation.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no recommendation yet")
    try:
        rec = json.loads(row.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="stored recommendation is unreadable")
    return {
        "id": row.id,
        "change_request_id": change_id,
        "partner_id": partner_id,
        "a2a_message_id": row.a2a_message_id,
        "message_type": row.message_type,
        "version": row.version,
        "model_used": row.model_used,
        "created_at": row.created_at.isoformat(),
        "recommendation": rec,
    }


@router.get("/{change_id}/resolver-recommendations")
def list_recommendations(
    change_id: str,
    partner_id: str = Query(...),
    db: DbDep = None,
    _: AdminUser = None,
):
    """Every resolver recommendation for a (change, partner), newest first.

    The singular endpoint above returns only the latest, so once a partner had
    more than one query open the earlier queries' suggestions still existed but
    were unreachable — the PM saw a recommendation for the newest query only.
    Each row carries the `correlation_id` of the query it answers so the UI can
    pair it with the negotiation thread. A query with several versions appears
    once per version; newest-first ordering means the first hit is the current one.
    """
    rows = db.execute(
        select(ResolverRecommendation)
        .where(
            ResolverRecommendation.change_request_id == change_id,
            ResolverRecommendation.partner_id == partner_id,
        )
        .order_by(ResolverRecommendation.created_at.desc())
    ).scalars().all()

    # The recommendation is keyed on the inbound A2A row; the correlation_id the
    # rest of Phase C pairs on lives in that row's payload.
    corr_by_msg: dict[str, str | None] = {}
    if rows:
        from app.models.phase_c import A2AMessage
        msgs = db.execute(
            select(A2AMessage).where(A2AMessage.id.in_([r.a2a_message_id for r in rows]))
        ).scalars().all()
        for m in msgs:
            payload = m.payload or {}
            inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
            corr_by_msg[m.id] = (inner or {}).get("correlation_id")

    out = []
    for row in rows:
        try:
            rec = json.loads(row.content)
        except json.JSONDecodeError:
            logger.warning("resolver recommendation %s is unreadable — skipping", row.id)
            continue
        out.append({
            "id": row.id,
            "change_request_id": change_id,
            "partner_id": partner_id,
            "a2a_message_id": row.a2a_message_id,
            "correlation_id": corr_by_msg.get(row.a2a_message_id),
            "message_type": row.message_type,
            "version": row.version,
            "model_used": row.model_used,
            "created_at": row.created_at.isoformat(),
            "recommendation": rec,
        })
    return {"recommendations": out}


@router.post("/{change_id}/resolve/{a2a_message_id}")
async def run_resolver(
    change_id: str,
    a2a_message_id: str,
    _: AdminUser = None,
):
    """Manually trigger (or re-trigger) the resolver for a specific message.
    Runs in the request coroutine so the PM sees the result immediately.

    For the demo, this is a simple awaited call. Production would enqueue
    a celery task and return a 202 with a poll URL.
    """
    await auto_resolve_background(a2a_message_id, change_id)
    return {"status": "ok", "a2a_message_id": a2a_message_id}
