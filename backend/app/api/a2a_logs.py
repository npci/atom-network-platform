# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin A2A communications log — Slice 25.

A read-only listing endpoint that powers the new admin UI page. Surfaces
every `a2a_messages` row with both halves of the round-trip
(request_body = the existing `payload` column; response_body = the new
column added in alembic 0042). Filters by change_request_id, change
title (substring), partner name (substring), direction, and task_type.
Paginated.

The endpoint is admin-gated. Operators use it to:
  * Audit a specific partner's recent activity
  * Find every message related to a change request
  * Reproduce a failure by inspecting the exact request body that hit
    the receiver and the receiver's reply
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from app.core.deps import AdminUser, DbDep
from app.models.change_request import ChangeRequest
from app.models.phase_c import (
    A2ADirection, A2AMessage, A2ATaskType, PartnerAgent,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/a2a-logs", tags=["a2a-logs"])


@router.get("")
def list_a2a_messages(
    db: DbDep,
    _: AdminUser,
    # Filters — all optional; AND'd together
    change_request_id: Optional[str] = Query(None, description="Exact match"),
    change_title:      Optional[str] = Query(None, description="Substring, case-insensitive"),
    partner_name:      Optional[str] = Query(None, description="Substring, case-insensitive"),
    direction:         Optional[str] = Query(None, pattern="^(inbound|outbound)$"),
    task_type:         Optional[str] = Query(None, description="A2ATaskType value"),
    success_only:      Optional[bool] = Query(None, description="When true, exclude error_code IS NOT NULL rows"),
    # Pagination
    limit:  int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Return a paginated, filtered list of A2A audit rows + total count.

    Response shape:

        {
          "total":  <int>,                 # rows matching filters (pre-pagination)
          "limit":  <int>,
          "offset": <int>,
          "items": [ { ... per-row dict ... }, ... ]
        }

    Each item carries enough joined context for the UI to render
    without follow-up calls (partner name + change title alongside the
    foreign keys + bodies).
    """
    P = aliased(PartnerAgent)
    C = aliased(ChangeRequest)

    base = (
        select(A2AMessage, P.name.label("partner_name"), C.title.label("change_title"))
        .join(P, P.id == A2AMessage.partner_id)
        .outerjoin(C, C.id == A2AMessage.change_request_id)
    )

    where = []
    if change_request_id:
        where.append(A2AMessage.change_request_id == change_request_id)
    if change_title:
        where.append(func.lower(C.title).contains(change_title.lower()))
    if partner_name:
        where.append(func.lower(P.name).contains(partner_name.lower()))
    if direction:
        where.append(A2AMessage.direction == A2ADirection(direction))
    if task_type:
        # Validate against BOTH vocabularies — the protocol-v1 set the UI now
        # offers and the legacy enum still on historical rows — the same union
        # the executor accepts. (Legacy-only validation made every new v1
        # filter silently return 0 rows.)
        from app.a2a_common import protocol as _proto
        _known = {t.value for t in A2ATaskType} | {t.value for t in _proto.A2ATaskType}
        if task_type not in _known:
            # Unknown task_type → return empty rather than error; the UI
            # can show "no matches" without a 400 round-trip.
            return {"total": 0, "limit": limit, "offset": offset, "items": []}
        where.append(A2AMessage.task_type == task_type)
    if success_only:
        where.append(A2AMessage.error_code.is_(None))

    if where:
        base = base.where(and_(*where))

    # Total count (apply same filters; drop the SELECT columns).
    count_stmt = (
        select(func.count(A2AMessage.id))
        .select_from(A2AMessage)
        .join(P, P.id == A2AMessage.partner_id)
        .outerjoin(C, C.id == A2AMessage.change_request_id)
    )
    if where:
        count_stmt = count_stmt.where(and_(*where))
    total = db.scalar(count_stmt) or 0

    rows = db.execute(
        base.order_by(A2AMessage.created_at.desc()).limit(limit).offset(offset)
    ).all()

    items = []
    for msg, partner_name_v, change_title_v in rows:
        items.append({
            "id":                 msg.id,
            "created_at":         msg.created_at.isoformat() if msg.created_at else None,
            "direction":          msg.direction.value if hasattr(msg.direction, "value") else msg.direction,
            "task_type":          msg.task_type.value  if hasattr(msg.task_type,  "value") else msg.task_type,
            "status":             msg.status,
            "task_state":         msg.task_state,
            "protocol_ver":       msg.protocol_ver,
            # Foreign keys + joined display values. UI shows whichever it has.
            "change_request_id":  msg.change_request_id,
            "change_title":       change_title_v,
            "partner_id":         msg.partner_id,
            "partner_name":       partner_name_v,
            # Audit columns from Slice 8
            "caller_ip":          str(msg.caller_ip) if msg.caller_ip else None,
            "jwt_sub":            msg.jwt_sub,
            "latency_ms":         msg.latency_ms,
            "error_code":         msg.error_code,
            "client_cert_fingerprint": msg.client_cert_fingerprint,
            # Delivery-retry state (alembic 0095) — lets the UI tell "still being retried
            # automatically" apart from "given up, needs a manual resend".
            "attempts":           msg.attempts,
            "next_retry_at":      msg.next_retry_at.isoformat() if msg.next_retry_at else None,
            "last_error_at":      msg.last_error_at.isoformat() if msg.last_error_at else None,
            # Bodies — both halves of the round-trip (Slice 25)
            "request_body":       msg.payload,        # legacy column name
            "response_body":      msg.response_body,
        })

    return {
        "total":  int(total),
        "limit":  limit,
        "offset": offset,
        "items":  items,
    }


@router.get("/stats")
def a2a_logs_stats(db: DbDep, _: AdminUser):
    """Headline counters for the admin page header.

    Cheap aggregate scan; runs on every page-open. If volume grows
    enough to make this slow, fold into a materialised view.
    """
    total = db.scalar(select(func.count(A2AMessage.id))) or 0
    inbound = db.scalar(
        select(func.count(A2AMessage.id))
        .where(A2AMessage.direction == A2ADirection.INBOUND)
    ) or 0
    outbound = db.scalar(
        select(func.count(A2AMessage.id))
        .where(A2AMessage.direction == A2ADirection.OUTBOUND)
    ) or 0
    failed = db.scalar(
        select(func.count(A2AMessage.id))
        .where(A2AMessage.error_code.is_not(None))
    ) or 0
    return {
        "total":    int(total),
        "inbound":  int(inbound),
        "outbound": int(outbound),
        "failed":   int(failed),
    }


@router.get("/{message_id}/integrity")
def check_a2a_message_integrity(message_id: str, db: DbDep, _: AdminUser):
    """Closes THREAT_MODEL.md T1 ("No at-rest integrity check on
    A2AMessage.payload"). Recomputes sha256(JSON(payload)) and compares
    it against the `payload_sha256` recorded at receipt time
    (SdkHmacMiddleware, at the moment the raw request body was verified/
    read) — a mismatch means the row was altered AFTER receipt (a
    compromised DB credential or an internal actor editing it directly),
    since the in-transit HMAC only ever proved integrity for the moment
    of receipt.

    Returns `status="no_baseline"` for rows persisted before this
    remediation (payload_sha256 is NULL) or for OUTBOUND rows (the
    hash is only computed on the INBOUND receipt path) — these are not
    tamper findings, just rows this control does not cover.
    """
    import hashlib
    import json
    from fastapi import HTTPException

    message = db.get(A2AMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    if not message.payload_sha256:
        return {
            "message_id": message_id,
            "status": "no_baseline",
            "detail": "No payload_sha256 recorded for this row (pre-remediation "
                      "row, or an OUTBOUND message — this control covers INBOUND "
                      "receipt only).",
        }

    # payload_sha256 was computed at receipt time
    # (authority_executor.py) over the SAME canonical serialization
    # (sort_keys=True, no whitespace) used here, of the SAME `data`
    # object that became this row's `payload` column — so this
    # recomputation is an exact, deterministic check, not an
    # approximation: any difference means the `payload` column's
    # CONTENT changed since it was first persisted (at-rest tampering),
    # not a serialization-format artifact.
    current_hash = hashlib.sha256(
        json.dumps(message.payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    match = current_hash == message.payload_sha256

    result = {
        "message_id": message_id,
        "status": "match" if match else "MISMATCH",
        "recorded_payload_sha256": message.payload_sha256,
        "recomputed_sha256": current_hash,
        "hmac_signature_present": bool(message.hmac_signature),
        "hmac_key_version": message.hmac_key_version,
    }
    if not match:
        logger.warning(
            "SECURITY_EVENT event=payload_integrity_mismatch severity=high "
            "message_id=%s partner_id=%s",
            message_id, message.partner_id,
        )
    return result


@router.post("/{message_id}/resend")
async def resend_a2a_message(message_id: str, db: DbDep, _: AdminUser):
    """Manually re-attempt delivery of a failed outbound message.

    Complements the automatic retry sweeper (`a2a.retry_failed_deliveries`) for the cases
    it deliberately will not touch: attempts exhausted, or a non-retryable 4xx that an
    operator has since fixed (e.g. corrected the partner's endpoint or credentials).

    Re-attempts the SAME row, so the audit trail stays one record per logical send.
    """
    from fastapi import HTTPException
    from app.services.a2a_client import resend_message

    msg = db.get(A2AMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    direction = msg.direction.value if hasattr(msg.direction, "value") else msg.direction
    if direction != A2ADirection.OUTBOUND.value:
        raise HTTPException(status_code=400, detail="only outbound messages can be resent")
    if msg.status == "delivered":
        raise HTTPException(status_code=409, detail="message already delivered")

    out = await resend_message(db, msg)
    return {
        "id":            out.id,
        "status":        out.status,
        "attempts":      out.attempts,
        "error_code":    out.error_code,
        "next_retry_at": out.next_retry_at.isoformat() if out.next_retry_at else None,
        "delivered":     out.status == "delivered",
    }
