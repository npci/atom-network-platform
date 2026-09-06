# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Service layer for partner-query negotiation AI drafts.

Used by:
- `app.api.a2a.receive_task` — fires this as a BackgroundTask when a partner submits a QUERY.
- `app.api.phase_c.receive_partner_query` — manual fallback endpoint retained for retries.

Both call sites share `generate_ai_draft_for_query` so there is a single source of truth
for thread creation, context assembly, and draft persistence.
"""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.negotiation import draft_negotiation_response
from app.models.base import generate_uuid
from app.models.brd import BRD
from app.models.change_request import ChangeRequest
from app.models.phase_c import (
    A2AMessage, A2ATaskType,
    NegotiationThread, NegotiationMessage, NegotiationRole, ThreadStatus,
)
from app.models.tech_spec import TechSpec

logger = logging.getLogger(__name__)


async def generate_ai_draft_for_query(
    query_message_id: str,
    db: Session,
    thread_kind: str = "general",
) -> str | None:
    """Produce an AI draft response for a partner's QUERY / CERT_QUERY A2A message.

    Creates/locates the NegotiationThread (filtered by `thread_kind` so general
    and cert channels never collide), saves the partner message, calls the
    draft agent, stores the AI_DRAFT message, and marks the A2AMessage as
    'working'.

    Idempotent: if the same partner-query text already exists in the thread the
    draft generation is skipped (retry from partner is a no-op beyond status bump).

    Args:
        query_message_id: a2a_messages.id of the inbound QUERY / CERT_QUERY row.
        db:               SQLAlchemy session.
        thread_kind:      'general' or 'cert' — partition key for
                          NegotiationThread lookup/creation. Must match the
                          A2A task_type that triggered the call.

    Returns the draft text on success, None on failure / skip. Does not raise.
    """
    try:
        msg = db.get(A2AMessage, query_message_id)
        if not msg:
            logger.warning("Auto-draft: A2AMessage %s not found", query_message_id)
            return None
        if msg.task_type not in (A2ATaskType.QUERY, A2ATaskType.CERT_QUERY):
            return None
        if msg.status != "submitted":
            logger.info("Auto-draft: message %s already in status=%s, skipping", query_message_id, msg.status)
            return None

        # Inbound payload is the full A2A wrapper envelope: {from, task_type,
        # change_id, payload: {message: "...", correlation_id: "..."}}.
        # The query text lives nested under .payload.payload.message.
        # Older callers that pre-unwrapped remain supported via the
        # top-level fallback. correlation_id is the partner's OutgoingQuery.id
        # — we persist it on the NegotiationMessage so the matching
        # CLARIFICATION_RESPONSE can echo it back and the partner attaches
        # the answer to the exact originating query.
        outer = msg.payload or {}
        inner = outer.get("payload") if isinstance(outer.get("payload"), dict) else None
        source = inner or outer or {}
        query_text = source.get("message", "")
        correlation_id = source.get("correlation_id") or None
        if not query_text:
            logger.warning("Auto-draft: empty query payload for %s", query_message_id)
            return None

        change_id = msg.change_request_id
        partner_id = msg.partner_id
        if not change_id or not partner_id:
            logger.warning("Auto-draft: missing change or partner on message %s", query_message_id)
            return None

        cr = db.get(ChangeRequest, change_id)
        if not cr:
            logger.warning("Auto-draft: change %s not found", change_id)
            return None

        # Get or create thread — partitioned by kind so general / cert
        # have strictly separate thread rows even on the same (change,
        # partner) pair.
        thread = db.scalars(
            select(NegotiationThread).where(
                NegotiationThread.change_request_id == change_id,
                NegotiationThread.partner_id == partner_id,
                NegotiationThread.kind == thread_kind,
            )
        ).first()
        if not thread:
            thread = NegotiationThread(
                id=generate_uuid(),
                change_request_id=change_id,
                partner_id=partner_id,
                kind=thread_kind,
                status=ThreadStatus.OPEN,
            )
            db.add(thread)
            db.commit()
            db.refresh(thread)

        # Idempotency: prefer matching by the partner's correlation_id when
        # it's present (the protocol-correct key — survives whitespace /
        # encoding differences across SDK retries). Fall back to a
        # whitespace-normalised content match for messages from older
        # partner builds that don't yet send correlation_id.
        existing_partner_msg = None
        if correlation_id:
            existing_partner_msg = db.scalars(
                select(NegotiationMessage).where(
                    NegotiationMessage.thread_id == thread.id,
                    NegotiationMessage.role == NegotiationRole.PARTNER,
                    NegotiationMessage.correlation_id == correlation_id,
                )
            ).first()
        if existing_partner_msg is None:
            # Legacy fallback — same content-equality as before. Only fires
            # for queries from partner builds that don't yet send
            # correlation_id; new sends are dedup'd by ID above.
            existing_partner_msg = db.scalars(
                select(NegotiationMessage).where(
                    NegotiationMessage.thread_id == thread.id,
                    NegotiationMessage.role == NegotiationRole.PARTNER,
                    NegotiationMessage.correlation_id.is_(None),
                    NegotiationMessage.content == query_text,
                )
            ).first()
        if existing_partner_msg:
            logger.info("Auto-draft: partner query already recorded for thread=%s — marking working", thread.id)
            msg.status = "working"
            db.commit()
            return None

        partner_msg = NegotiationMessage(
            id=generate_uuid(),
            thread_id=thread.id,
            role=NegotiationRole.PARTNER,
            content=query_text,
            correlation_id=correlation_id,
        )
        db.add(partner_msg)
        db.commit()

        brd = db.scalars(
            select(BRD).where(BRD.change_request_id == change_id).order_by(BRD.version.desc())
        ).first()
        tech_spec = db.scalars(
            select(TechSpec).where(TechSpec.change_request_id == change_id).order_by(TechSpec.version.desc())
        ).first()
        thread_msgs = db.scalars(
            select(NegotiationMessage)
            .where(NegotiationMessage.thread_id == thread.id)
            .order_by(NegotiationMessage.created_at)
        ).all()
        history = [{"role": m.role.value, "content": m.content} for m in thread_msgs[:-1]]

        logger.info(
            "Auto-draft: generating change=%s partner=%s query_len=%d",
            change_id, partner_id, len(query_text),
        )

        ai_draft = await draft_negotiation_response(
            query=query_text,
            change_title=cr.title or "",
            brd_content=brd.content if brd else "",
            tech_spec_content=tech_spec.content if tech_spec else "",
            enhanced_prompt=cr.enhanced_prompt or "",
            thread_history=history,
            db=db,
        )

        draft_msg = NegotiationMessage(
            id=generate_uuid(),
            thread_id=thread.id,
            role=NegotiationRole.AI_DRAFT,
            content=ai_draft,
            ai_draft=ai_draft,
            # Pair the draft to the query it answers. NULL here orphaned every
            # draft, so the UI could only ever surface the most recent one.
            correlation_id=correlation_id,
        )
        db.add(draft_msg)
        msg.status = "working"
        db.commit()

        logger.info(
            "Auto-draft: done change=%s partner=%s len=%d",
            change_id, partner_id, len(ai_draft),
        )
        return ai_draft
    except Exception:
        logger.exception("Auto-draft failed for message %s", query_message_id)
        try:
            db.rollback()
        except Exception:
            pass
        return None


async def auto_draft_background(query_message_id: str, thread_kind: str = "general") -> None:
    """BackgroundTask wrapper — opens its own DB session and delegates.

    Runs after the A2A receive_task response has been returned to the partner,
    so LLM latency never blocks the partner's HTTP call.

    The executor passes thread_kind='general' for QUERY and 'cert' for
    CERT_QUERY — keeps the channels strictly partitioned at thread-row
    level.
    """
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        await generate_ai_draft_for_query(query_message_id, db, thread_kind=thread_kind)
    finally:
        db.close()


def record_partner_query(
    message: A2AMessage,
    db: Session,
    thread_kind: str = "general",
) -> NegotiationThread | None:
    """Record a partner's QUERY onto its NegotiationThread — WITHOUT a draft.

    The general-query path no longer auto-drafts (the feasibility resolver
    owns the PM-facing draft now), but the PM's approve-and-respond endpoint
    (`POST .../negotiation/respond`) still requires the thread + partner
    message to exist — it 404s otherwise. This is the thread-side-effect that
    `generate_ai_draft_for_query` used to perform inline, split out so it can
    run on its own.

    Synchronous, on the caller's session: the writes are cheap and the thread
    must exist by the time the partner's HTTP call returns (the PM may poll
    immediately). Idempotent on correlation_id / query text. Never raises —
    a failure here must not fail the partner's task.

    Leaves the A2AMessage status as-is ('submitted'). The resolver runs
    out-of-band and the PM UI keys its "resolver analysing…" placeholder on
    a still-'submitted' inbound query — flipping to 'working' here would
    suppress that spinner and leave the PM staring at a silent poll loop.
    The respond endpoint marks it 'completed' when the PM answers.
    """
    try:
        if message.task_type not in (A2ATaskType.QUERY, A2ATaskType.CERT_QUERY):
            return None
        outer = message.payload or {}
        inner = outer.get("payload") if isinstance(outer.get("payload"), dict) else None
        source = inner or outer or {}
        query_text = source.get("message", "")
        correlation_id = source.get("correlation_id") or None
        if not query_text:
            logger.warning("record_partner_query: empty query payload for %s", message.id)
            return None
        change_id = message.change_request_id
        partner_id = message.partner_id
        if not change_id or not partner_id:
            logger.warning("record_partner_query: missing change/partner on %s", message.id)
            return None

        thread = get_or_create_thread(db, change_id, partner_id, kind=thread_kind)

        # Idempotency mirrors generate_ai_draft_for_query: prefer the partner's
        # correlation_id, fall back to a content match for older builds.
        existing = None
        if correlation_id:
            existing = db.scalars(
                select(NegotiationMessage).where(
                    NegotiationMessage.thread_id == thread.id,
                    NegotiationMessage.role == NegotiationRole.PARTNER,
                    NegotiationMessage.correlation_id == correlation_id,
                )
            ).first()
        if existing is None:
            existing = db.scalars(
                select(NegotiationMessage).where(
                    NegotiationMessage.thread_id == thread.id,
                    NegotiationMessage.role == NegotiationRole.PARTNER,
                    NegotiationMessage.correlation_id.is_(None),
                    NegotiationMessage.content == query_text,
                )
            ).first()
        if existing is None:
            db.add(NegotiationMessage(
                id=generate_uuid(),
                thread_id=thread.id,
                role=NegotiationRole.PARTNER,
                content=query_text,
                correlation_id=correlation_id,
            ))
            db.commit()
        return thread
    except Exception:
        logger.exception("record_partner_query failed for message %s", message.id)
        try:
            db.rollback()
        except Exception:
            pass
        return None


# ── Unified-conversation dual-write helper ───────────────────────────────────
# Used by every code path that creates or resolves a CounterProposal or
# Blocker, so each structured event also has a NegotiationMessage row
# attached. After the Step 4 cutover, this NM is what the renderer
# reads — synthetic event emission is removed. Until then, the synthetic
# events still drive the UI and these rows are dedup-skipped by
# `get_negotiation_thread` (so we never render the same event twice).

def get_or_create_thread(
    db: Session,
    change_request_id: str,
    partner_id: str,
    kind: str = "general",
) -> NegotiationThread:
    """Get the (change, partner, kind) thread, creating one if absent.

    Used by the dual-write helpers below — a counter or blocker may
    arrive before the partner has sent any Q&A on this change, so the
    thread row may not exist yet.
    """
    thread = db.scalars(
        select(NegotiationThread).where(
            NegotiationThread.change_request_id == change_request_id,
            NegotiationThread.partner_id == partner_id,
            NegotiationThread.kind == kind,
        )
    ).first()
    if thread is None:
        thread = NegotiationThread(
            id=generate_uuid(),
            change_request_id=change_request_id,
            partner_id=partner_id,
            kind=kind,
            status=ThreadStatus.OPEN,
        )
        db.add(thread)
        db.flush()
    return thread


def record_linked_message(
    db: Session,
    *,
    change_request_id: str,
    partner_id: str,
    role: str,                       # 'partner' | 'po_approved'
    content: str,
    event_kind: str,                 # 'proposal' | 'resolution' | 'blocker' | 'blocker_resolution'
    counter_proposal_id: str | None = None,
    blocker_id: str | None = None,
    approved_by: str | None = None,
    correlation_id: str | None = None,
    thread_kind: str = "general",
) -> NegotiationMessage | None:
    """Create a NegotiationMessage row linked to a CP or Blocker.

    Wrapped in try/except by the caller — a failure here must NEVER
    block the structured record's own creation/resolution. The NM
    is for audit/timeline continuity only.
    """
    thread = get_or_create_thread(db, change_request_id, partner_id, kind=thread_kind)
    role_value = (
        NegotiationRole.PARTNER if role == "partner"
        else NegotiationRole.PO_APPROVED
    )
    msg = NegotiationMessage(
        id=generate_uuid(),
        thread_id=thread.id,
        role=role_value,
        content=content,
        approved_by=approved_by,
        correlation_id=correlation_id,
        counter_proposal_id=counter_proposal_id,
        blocker_id=blocker_id,
        event_kind=event_kind,
    )
    db.add(msg)
    db.flush()
    return msg
