# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2A Protocol API — partner auth + task read endpoints.

Slice 8 of the unified A2A SDK refactor TRIMMED this module:
  * Removed: legacy `POST /a2a/tasks/send` (the SDK JSON-RPC at
             /a2a-rpc/rpc handles this now — see
             `app.a2a_common.authority_executor`)
  * Removed: `_process_status_update / _readiness_declaration /
             _change_acknowledgement` — moved to
             `app.a2a_common.authority_handlers`
  * Removed: cert handler imports — same destination

What stays:
  * `POST /a2a/auth`                 — partner exchanges API key for JWT
  * `GET  /a2a/tasks/{task_id}`      — partner polls a Task by id
  * `GET  /a2a/tasks`                — partner lists their Tasks
  * `GET  /a2a/threads`              — operator dashboard (negotiation)
  * `get_agent_card()`               — used by /.well-known/agent.json
                                       served from app.main

These are NOT part of the A2A SDK protocol surface; they're auxiliary
endpoints that complement the SDK mount. The SDK provides its own
JSON-RPC `tasks/get` for partners that prefer that path.
"""
import hashlib
import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import CurrentPartner, CurrentUser, DbDep
from app.core.security import (
    A2A_ACCESS_TOKEN_TTL_S,
    A2A_REFRESH_TOKEN_TTL_S,
    create_partner_refresh_token,
    create_partner_token,
    decode_partner_refresh_token,
)
from app.models.base import generate_uuid, utcnow
from app.models.phase_c import (
    A2AMessage,
    A2ASession,
    PartnerAgent,
    PartnerStatus,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/a2a", tags=["a2a"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class AuthRequest(BaseModel):
    api_key: str


# ── Agent Card ────────────────────────────────────────────────────────────────

def get_agent_card() -> dict:
    """Return the legacy hand-rolled agent card dict.

    Served at `/.well-known/agent.json` from `app.main` for back-compat
    with partners that haven't moved to the SDK card. The SDK serves a
    spec-compliant card at `/.well-known/agent-card.json` (note the
    dash) via routes registered in Slice 3's mount.
    """
    from app.a2a_common.authority_card import _card_name
    from app.core.domain.registry import prompt_block

    return {
        "name": _card_name(),
        "description": (
            f"AI-powered platform for managing {prompt_block('domain_name', 'specification')} "
            "feature changes across the ecosystem. "
            "Handles change communication, partner negotiation, implementation tracking, "
            "and certification testing via the A2A protocol."
        ),
        "url": settings.frontend_url or "http://localhost",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": True},
        "authentication": {
            "schemes": ["apiKey"],
            "apiKeyHeader": "Authorization",
            "instructions": (
                f"1. Register as a partner with {prompt_block('authority', 'the platform operator')} to receive your API key. "
                "2. POST your API key to /api/a2a/auth to receive a JWT (1-hour expiry). "
                "3. Include the JWT as Bearer token in all subsequent Task requests."
            ),
        },
        "skills": [
            {
                "id": "change_communication", "name": "Change Communication",
                "description": "Receive UPI feature change notifications with full Product Kit (BRD, Tech Spec, XSD, test cases, etc.)",
            },
            {
                "id": "negotiation", "name": "Change Negotiation",
                "description": "Submit implementation queries and receive AI-assisted, PO-approved clarifications",
            },
            {
                "id": "status_tracking", "name": "Implementation Status Tracking",
                "description": "Report intermediate implementation progress (design, coding, testing completed)",
            },
            {
                "id": "cert_readiness_declaration", "name": "Cert Readiness Declaration",
                "description": "Declare readiness for certification testing",
            },
            {
                "id": "certification", "name": "Certification Testing",
                "description": "Participate in bidirectional certification testing (NPCI→Partner and Partner→NPCI)",
            },
        ],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
    }


# ── Auth ──────────────────────────────────────────────────────────────────────

@router.post("/auth")
def authenticate_partner(body: AuthRequest, db: DbDep):
    """Partner exchanges API key for an access + refresh JWT pair.

    Slice 9 of A2A security hardening: access tokens are now 15 min,
    refresh tokens are 24h. Clients should cache both, use the access
    token on /a2a-rpc/rpc, and call /a2a/auth/refresh before the
    access expires (or on a 401) to mint a new pair.

    Used by both the legacy POST flow and the SDK's outbound client
    (cert_engine partners) — `app.a2a_common.auth.fetch_bearer_jwt`
    hits this endpoint to populate its per-partner token cache.
    """
    key_hash = hashlib.sha256(body.api_key.encode()).hexdigest()
    partner = db.scalars(
        select(PartnerAgent).where(
            PartnerAgent.api_key_hash == key_hash,
            PartnerAgent.status == PartnerStatus.ACTIVE,
        )
    ).first()

    if not partner:
        logger.warning("A2A auth failed: invalid API key")
        raise HTTPException(status_code=401, detail="Invalid API key or partner inactive")

    access_token = create_partner_token(partner.id)
    refresh_token = create_partner_refresh_token(partner.id)
    access_hash  = hashlib.sha256(access_token.encode()).hexdigest()
    refresh_hash = hashlib.sha256(refresh_token.encode()).hexdigest()

    session = A2ASession(
        id=generate_uuid(),
        partner_id=partner.id,
        jwt_token_hash=access_hash,
        refresh_token_hash=refresh_hash,
        expires_at=utcnow() + timedelta(seconds=A2A_ACCESS_TOKEN_TTL_S),
        created_at=utcnow(),
    )
    db.add(session)
    db.commit()

    logger.info("A2A auth success: partner=%s name='%s'", partner.id, partner.name)

    return {
        "jwt":             access_token,         # legacy field name kept for back-compat
        "access_token":    access_token,
        "refresh_token":   refresh_token,
        "expires_in":      A2A_ACCESS_TOKEN_TTL_S,
        "refresh_expires_in": A2A_REFRESH_TOKEN_TTL_S,
        "partner_id":      partner.id,
        "partner_name":    partner.name,
    }


class RefreshRequest(BaseModel):
    """Body for POST /a2a/auth/refresh (Slice 9)."""
    refresh_token: str


@router.post("/auth/refresh")
def refresh_partner_token(body: RefreshRequest, db: DbDep):
    """Mint a new access+refresh pair from a valid refresh token.

    Rotate-on-use: every successful refresh invalidates the old
    refresh by replacing `a2a_sessions.refresh_token_hash`. If a
    stolen refresh token is used after the legit client has
    refreshed, the stolen call sees a hash mismatch and 401s. This
    is the standard "refresh token rotation" pattern.

    Returns 401 with structured error codes for each failure mode so
    SDKs can branch on the cause:
      invalid_refresh   — bad signature, wrong type, or expired
      session_unknown   — refresh wasn't registered (possible theft)
      session_revoked   — admin revoked the session
      partner_inactive  — partner status flipped since the original /auth
    """
    partner_id = decode_partner_refresh_token(body.refresh_token)
    if not partner_id:
        raise HTTPException(status_code=401, detail={"error": "invalid_refresh"})

    refresh_hash = hashlib.sha256(body.refresh_token.encode()).hexdigest()

    session = db.scalars(
        select(A2ASession).where(
            A2ASession.partner_id == partner_id,
            A2ASession.refresh_token_hash == refresh_hash,
        )
    ).first()
    if session is None:
        # Either the refresh was never issued (forgery) OR the legit
        # client already refreshed and we're seeing a stale (or stolen)
        # one. Both are 401; structured field tells operators apart in
        # the audit trail.
        logger.warning(
            "A2A refresh failed: session_unknown partner=%s (possible refresh-token theft)",
            partner_id,
        )
        raise HTTPException(status_code=401, detail={"error": "session_unknown"})
    if getattr(session, "revoked_at", None) is not None:
        raise HTTPException(status_code=401, detail={"error": "session_revoked"})

    partner = db.get(PartnerAgent, partner_id)
    if not partner or partner.status != PartnerStatus.ACTIVE:
        raise HTTPException(status_code=401, detail={"error": "partner_inactive"})

    # Mint the new pair. Replace BOTH hashes on the session row so
    # the previous refresh becomes invalid (rotate-on-use).
    new_access  = create_partner_token(partner_id)
    new_refresh = create_partner_refresh_token(partner_id)
    session.jwt_token_hash     = hashlib.sha256(new_access.encode()).hexdigest()
    session.refresh_token_hash = hashlib.sha256(new_refresh.encode()).hexdigest()
    session.expires_at  = utcnow() + timedelta(seconds=A2A_ACCESS_TOKEN_TTL_S)
    session.refreshed_at = utcnow()
    db.commit()

    logger.info("A2A refresh success: partner=%s", partner_id)

    return {
        "jwt":                new_access,
        "access_token":       new_access,
        "refresh_token":      new_refresh,
        "expires_in":         A2A_ACCESS_TOKEN_TTL_S,
        "refresh_expires_in": A2A_REFRESH_TOKEN_TTL_S,
        "partner_id":         partner_id,
    }


# ── Task read endpoints (auxiliary; the SDK has its own tasks/get) ───────────

@router.get("/tasks/{task_id}")
def get_task_status(task_id: str, db: DbDep, partner: CurrentPartner = None):
    """Poll the status of a previously-submitted A2A Task.

    Partners can only view their own tasks. Returns the platform-side
    audit row (`A2AMessage`); partners that need the SDK Task lifecycle
    state should use the SDK's JSON-RPC `tasks/get` instead.
    """
    message = db.get(A2AMessage, task_id)
    if not message:
        raise HTTPException(status_code=404, detail="Task not found")

    if message.partner_id != partner.id:
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "task_id": message.id,
        "status": message.status,
        "task_type": message.task_type.value if hasattr(message.task_type, 'value') else message.task_type,
        "direction": message.direction.value if hasattr(message.direction, 'value') else message.direction,
        "change_id": message.change_request_id,
        "payload": message.payload,
        "created_at": message.created_at.isoformat(),
    }


@router.get("/tasks")
def list_tasks(
    db: DbDep,
    partner: CurrentPartner = None,
    change_id: str | None = None,
    limit: int = 50,
):
    """List all A2A tasks for the authenticated partner."""
    query = select(A2AMessage).where(A2AMessage.partner_id == partner.id)
    if change_id:
        query = query.where(A2AMessage.change_request_id == change_id)
    query = query.order_by(A2AMessage.created_at.desc()).limit(limit)

    messages = db.scalars(query).all()
    return [
        {
            "task_id": m.id,
            "status": m.status,
            "task_type": m.task_type.value if hasattr(m.task_type, 'value') else m.task_type,
            "direction": m.direction.value if hasattr(m.direction, 'value') else m.direction,
            "change_id": m.change_request_id,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]


# ── Operator dashboard: all negotiation threads ──────────────────────────────

@router.get("/threads")
def list_all_threads(db: DbDep, _: CurrentUser, kind: str | None = None):
    """Return all negotiation threads across all changes and partners.

    Used by the Agent Messaging sidebar to build the full thread list and
    compute total unread counts without requiring the caller to enumerate
    all change–partner combinations manually.

    Args:
        kind: Optional channel filter — 'general' or 'cert'. Omit to
              return all kinds. Frontend segment control passes this
              through so each tab sees only its own channel's threads.
    """
    from sqlalchemy.orm import selectinload

    from app.models.change_request import ChangeRequest
    from app.models.phase_c import NegotiationMessage, NegotiationThread

    # A14 (architecture review High #7, "N+1 Query Pattern in Thread Listing").
    # The original loop issued 1 (threads) + 3N (per-thread ChangeRequest get +
    # PartnerAgent get + NegotiationMessage select) queries — 301 round-trips
    # for 100 threads. `NegotiationThread` has no ORM `relationship()` to
    # `ChangeRequest`/`PartnerAgent` (only to `NegotiationMessage`), so instead
    # of adding relationships (a model/migration change out of scope for a
    # read-path fix), this batches the three N+1 sites into three total
    # queries: one for threads (with eager-loaded messages via the existing
    # `messages` relationship), and one each for the distinct change/partner
    # ids referenced — collapsing 1+3N into a constant 3 queries regardless
    # of N. Reference: EA_Skills.md P6 "avoidance of chatty access and N+1
    # queries".
    q = select(NegotiationThread).options(selectinload(NegotiationThread.messages))
    if kind in ("general", "cert"):
        q = q.where(NegotiationThread.kind == kind)
    threads = db.scalars(q).all()
    result = []

    change_ids = {t.change_request_id for t in threads}
    partner_ids = {t.partner_id for t in threads}
    crs_by_id = {
        cr.id: cr for cr in db.scalars(
            select(ChangeRequest).where(ChangeRequest.id.in_(change_ids))
        ).all()
    } if change_ids else {}
    partners_by_id = {
        p.id: p for p in db.scalars(
            select(PartnerAgent).where(PartnerAgent.id.in_(partner_ids))
        ).all()
    } if partner_ids else {}

    for t in threads:
        cr = crs_by_id.get(t.change_request_id)
        partner = partners_by_id.get(t.partner_id)
        if not cr or not partner:
            continue

        msgs = sorted(t.messages, key=lambda m: m.created_at)

        # Unread = partner messages with NO PO-approved reply after them.
        # Naive count-difference (len(partner) - len(approved)) is wrong
        # when an admin sends an unprompted reply or asks twice in a row,
        # because totals diverge from chronology. Walk the timeline and
        # count partner messages whose successor list contains no
        # po_approved/npci/approved row.
        def _role(m):
            return m.role.value if hasattr(m.role, "value") else m.role
        REPLY_ROLES = {"po_approved", "npci", "approved"}
        unread = 0
        for i, m in enumerate(msgs):
            if _role(m) != "partner":
                continue
            has_reply_after = any(_role(n) in REPLY_ROLES for n in msgs[i + 1:])
            if not has_reply_after:
                unread += 1

        latest = msgs[-1] if msgs else None
        latest_preview = (latest.content or '')[:80] if latest else ''
        latest_role = (latest.role.value if hasattr(latest.role, 'value') else latest.role) if latest else None

        result.append({
            "thread_id":   t.id,
            "kind":        t.kind or "general",
            "change_id":   t.change_request_id,
            "change_title": cr.title or "",
            "change_status": cr.status.value if hasattr(cr.status, 'value') else str(cr.status),
            "partner_id":  t.partner_id,
            "partner_name": partner.name,
            "partner_types": partner.partner_type or ["bank"],
            "thread_status": t.status.value if hasattr(t.status, 'value') else str(t.status),
            "message_count": len(msgs),
            "unread_count":  unread,
            "latest_preview": latest_preview,
            "latest_role":    latest_role,
            "latest_at":   latest.created_at.isoformat() if latest else None,
        })

    # Sort: threads with unread first, then by latest message time desc
    result.sort(key=lambda x: (-(x["unread_count"] > 0), x["latest_at"] or ""), reverse=False)
    result.sort(key=lambda x: x["unread_count"], reverse=True)

    total_unread = sum(r["unread_count"] for r in result)
    return {"threads": result, "total_unread": total_unread}
