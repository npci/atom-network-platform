# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase C API — Partner Collaboration workflow endpoints.

Covers:
  - Partner assignment to change requests
  - Change Communication (Product Kit delivery via A2A)
  - Phase C status dashboard
  - A2A message audit log
"""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func

from app.core.deps import DbDep, CurrentUser
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.phase_c import (
    PartnerAgent, PartnerStatus, ChangePartnerAssignment, AssignmentStatus,
    A2AMessage, A2ADirection, A2ATaskType,
    NegotiationThread, NegotiationMessage, NegotiationRole, ThreadStatus,
    CounterProposal, CounterProposalStatus,
    Blocker, BlockerSeverity, BlockerStatus,
    PartnerProgress, ProgressStep,
    CertRun, CertRunStatus, CertTestResult, CertDirection, CertTestStatus,
    CertTriage, TriageVerdict,
)
from app.models.product_kit import ProductKitDocType
from app.models.brd import BRD
from app.models.tech_spec import TechSpec
from app.models.base import generate_uuid, utcnow
from app.agents.cert_triage import triage_failed_tests
from app.services.a2a_client import send_task_to_partner  # certification sends only
from app.services.partner_dispatch import notify_partner
from app.services.assignment_status import set_status
from app.services.product_kit_query import latest_kit_doc
from app.agents.negotiation import draft_negotiation_response
from app.services.evaluation.checkpoints import CheckpointId
from app.services.evaluation.contracts import get_contract
from app.services.evaluation.policy import decide_gate, get_policy_mode
from app.services.evaluation.runner import run_advisory
from app.services.evaluation.store import count_runs, get_latest

logger = logging.getLogger(__name__)
router = APIRouter(tags=["phase-c"])


def _query_row_in_channel(row, kind: str) -> bool:
    """Whether an inbound query A2A row belongs to the given negotiation channel.

    Protocol-v1 folds cert_query into query+phase, so a cert-channel query is
    either the legacy task_type='cert_query' OR task_type='query' with payload
    phase='cert'. General queries are task_type='query' without phase='cert'.
    Used by the negotiation read/close paths so channels stay separate after
    the fold (kind=='cert' → cert channel, anything else → general).
    """
    tt = row.task_type
    if tt not in ("query", "cert_query"):
        return False
    raw = row.payload or {}
    inner = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    phase = str((inner or {}).get("phase") or "").strip().lower()
    is_cert = tt == "cert_query" or phase == "cert"
    return is_cert if kind == "cert" else not is_cert


# ── Schemas ───────────────────────────────────────────────────────────────────

class AssignPartnersRequest(BaseModel):
    partner_ids: list[str]


class ShipKitRequest(BaseModel):
    # Banks to ship to. Any not yet assigned to the change are assigned first.
    partner_ids: list[str]
    # Existing published version to ship. None = current latest. We never
    # generate a new version here (that flow is parked).
    negotiation_version: int | None = None
    # Per-item shipment filter (migration 0115). When present, only these
    # doc_types are packed into the envelope. None = ship every eligible doc
    # (legacy behaviour — preserves programmatic callers).
    include_doc_types: list[str] | None = None


class CommunicateChangeRequest(BaseModel):
    eval_acknowledged_verdict_id: str | None = None


# ── Partner Assignment ────────────────────────────────────────────────────────

@router.get("/changes/{change_id}/partners")
def list_assigned_partners(change_id: str, db: DbDep, _: CurrentUser):
    """List all partners assigned to a change request with their status."""
    assignments = db.scalars(
        select(ChangePartnerAssignment)
        .where(ChangePartnerAssignment.change_request_id == change_id)
        .order_by(ChangePartnerAssignment.assigned_at)
    ).all()

    result = []
    for a in assignments:
        partner = db.get(PartnerAgent, a.partner_id)
        if partner:
            open_counters = db.scalar(
                select(func.count(CounterProposal.id)).where(
                    CounterProposal.assignment_id == a.id,
                    CounterProposal.status == CounterProposalStatus.OPEN,
                )
            ) or 0
            open_blockers = db.scalar(
                select(func.count(Blocker.id)).where(
                    Blocker.assignment_id == a.id,
                    Blocker.status == BlockerStatus.OPEN,
                )
            ) or 0
            result.append({
                "assignment_id": a.id,
                "partner_id": partner.id,
                "name": partner.name,
                "partner_type": partner.partner_type.value if hasattr(partner.partner_type, 'value') else partner.partner_type,
                "endpoint_url": partner.endpoint_url,
                "status": a.status.value if hasattr(a.status, 'value') else a.status,
                "assigned_at": a.assigned_at.isoformat(),
                "blocked": a.blocked_at is not None,
                "blocked_at": a.blocked_at.isoformat() if a.blocked_at else None,
                "blocked_reason": a.blocked_reason,
                # Structured rollout-contract metadata. Two top-level
                # blocks: `acknowledged` (auto-receipt with kit-file
                # checksum verification) and `accepted` (PROPOSAL_ACCEPTANCE
                # with named approver + CAB ref + timeline). Either or
                # both may be present; null until partner has interacted.
                "acceptance_meta": a.acceptance_meta,
                "open_counters_count": open_counters,
                "open_blockers_count": open_blockers,
            })
    return result


@router.post("/changes/{change_id}/partners")
def assign_partners(change_id: str, body: AssignPartnersRequest, db: DbDep, _: CurrentUser):
    """Assign partners to a change request."""
    cr = db.get(ChangeRequest, change_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")

    assigned = []
    for pid in body.partner_ids:
        partner = db.get(PartnerAgent, pid)
        if not partner or partner.status != PartnerStatus.ACTIVE:
            continue

        # Skip if already assigned
        existing = db.scalars(
            select(ChangePartnerAssignment).where(
                ChangePartnerAssignment.change_request_id == change_id,
                ChangePartnerAssignment.partner_id == pid,
            )
        ).first()
        if existing:
            continue

        assignment = ChangePartnerAssignment(
            id=generate_uuid(),
            change_request_id=change_id,
            partner_id=pid,
            status=AssignmentStatus.ASSIGNED,
        )
        db.add(assignment)
        assigned.append(partner.name)

    db.commit()
    logger.info("Partners assigned: change=%s partners=%s", change_id, assigned)
    return {"assigned": assigned, "count": len(assigned)}


@router.delete("/changes/{change_id}/partners/{partner_id}")
def remove_partner_assignment(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """Remove a partner assignment from a change request."""
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    db.delete(assignment)
    db.commit()
    logger.info("Partner removed: change=%s partner=%s", change_id, partner_id)
    return {"removed": True}


# ── Change Communication ─────────────────────────────────────────────────────

@router.post("/changes/{change_id}/phase-c/communicate")
async def communicate_change(
    change_id: str,
    db: DbDep,
    user: CurrentUser,
    background: BackgroundTasks,
    negotiation_version: int | None = Query(
        None,
        description="Version the operator intends to ship. Must match the change's "
                    "current version — communication always ships the current latest "
                    "documents. Provided so a stale UI is rejected rather than silently "
                    "shipping a newer version than the operator saw.",
    ),
    body: CommunicateChangeRequest | None = None,
):
    """
    Package the Product Kit and send it to all assigned partners via A2A.

    This triggers Step 1 of Phase C: Change Communication. Always ships the
    current latest documents as the change's current `negotiation_version`.
    """
    cr = db.get(ChangeRequest, change_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    if cr.status != ChangeStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Phase A must be completed before communicating to partners")

    # The UI lets the operator see/pick the version; we only ever ship the
    # current latest docs (see endpoint docstring). If the picked version no
    # longer matches the change's current version (e.g. someone published a new
    # version in another tab), reject so the operator refreshes instead of
    # unknowingly shipping the newer one.
    current_version = getattr(cr, "negotiation_version", 1) or 1
    if negotiation_version is not None and negotiation_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This change is now at v{current_version}; you selected "
                f"v{negotiation_version}. Refresh and communicate the current version."
            ),
        )

    # Get assigned partners
    assignments = db.scalars(
        select(ChangePartnerAssignment)
        .where(ChangePartnerAssignment.change_request_id == change_id)
    ).all()

    # Communicate only when at least one partner is actually assigned and
    # still pending delivery (status ASSIGNED). Without this guard the
    # endpoint would package and "communicate" the kit whenever any
    # assignment row exists — including changes where every partner was
    # already communicated or removed — returning a misleading success
    # with zero recipients.
    if not any(a.status == AssignmentStatus.ASSIGNED for a in assignments):
        raise HTTPException(status_code=400, detail="No partners assigned to this change request")

    # ── Advisory eval gate (PRODUCT_KIT_TO_PHASE_C) ────────────────────────────
    # Preflight the kit against the eval harness before shipping. ADVISORY by
    # default (never blocks); blocks only on checkpoints an operator promoted to
    # a gate mode. Re-homed from the eval branch onto main's change_dispatch flow:
    # the eval branch read a since-removed `kit_docs` local, so we source the same
    # latest-per-doc_type list build_kit_envelope uses.
    from app.services.product_kit_query import latest_kit_docs

    kit_docs = latest_kit_docs(db, change_id)
    doc_map: dict[str, dict] = {}
    source_artifact_ids: list[str] = []
    for doc in kit_docs:
        if not doc.content:
            continue
        doc_type = doc.doc_type.value if hasattr(doc.doc_type, "value") else str(doc.doc_type)
        doc_map[doc_type] = {"type": doc_type, "content": doc.content}
        source_artifact_ids.append(doc.id)

    expected_documents = sorted(doc_map.keys())
    merged_document_text = "\n\n".join(
        e.get("content", "") for e in doc_map.values() if e.get("content")
    )

    verdict_row = None
    try:
        verdict_row = await run_advisory(
            db=db,
            change_request_id=change_id,
            checkpoint_id=CheckpointId.PRODUCT_KIT_TO_PHASE_C,
            source_artifacts={
                "product_kit_manifest": {
                    "type": "manifest",
                    "manifest": {"expected_documents": expected_documents},
                    "documents": doc_map,
                },
                "product_kit_documents": {
                    "type": "product_kit_documents",
                    "documents": doc_map,
                    "content": merged_document_text,
                },
            },
            target_artifacts={
                "a2a_communication_payload": {
                    "type": "a2a_payload",
                    "manifest": {"expected_documents": expected_documents},
                    "documents": doc_map,
                    "content": merged_document_text,
                }
            },
            source_artifact_ids=source_artifact_ids,
            target_artifact_ids=[a.id for a in assignments],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Phase C advisory preflight failed non-blocking: change=%s error=%s",
            change_id, exc,
        )

    contract = get_contract(CheckpointId.PRODUCT_KIT_TO_PHASE_C)
    policy_mode = get_policy_mode(
        db, CheckpointId.PRODUCT_KIT_TO_PHASE_C, fallback=contract.policy_mode,
    )
    latest_verdict = verdict_row or get_latest(
        db, change_id, CheckpointId.PRODUCT_KIT_TO_PHASE_C,
    )
    retries_used = max(
        count_runs(db, change_id, CheckpointId.PRODUCT_KIT_TO_PHASE_C,
                   include_overrides=False) - 1,
        0,
    )
    decision = decide_gate(
        checkpoint_id=CheckpointId.PRODUCT_KIT_TO_PHASE_C,
        policy_mode=policy_mode,
        verdict=latest_verdict,
        acknowledged_verdict_id=(body.eval_acknowledged_verdict_id if body else None),
        retry_allowed=contract.retry_allowed,
        retries_used=retries_used,
        override_allowed=bool(contract.override_allowed_roles),
    )
    if decision.blocked:
        raise HTTPException(status_code=409, detail=decision.to_debug_dict())

    # Package the kit from the latest version of each doc, snapshot it as the
    # publication for the current negotiation_version (v1 on first send), then
    # dispatch. See app.services.change_dispatch.
    from app.services.change_dispatch import (
        build_kit_envelope, dispatch_kit_to_partners, snapshot_publication,
    )
    product_kit = build_kit_envelope(cr, db)
    snapshot_publication(
        cr, product_kit, db,
        published_by=user.id if hasattr(user, "id") else None,
    )

    logger.info(
        "Change communication started: change=%s partners=%d docs=%d v=%d",
        change_id, len(assignments), len(product_kit["documents"]),
        product_kit["negotiation_version"],
    )

    dispatch = await dispatch_kit_to_partners(
        cr, product_kit, assignments, db,
        user.id if hasattr(user, "id") else None,
        mode="initial",
    )
    # First kit ship → auto-segregate BRD requirements (idempotent; runs once).
    from app.services.brd_requirements import auto_segregate_brd_requirements
    background.add_task(auto_segregate_brd_requirements, change_id)
    return {"change_id": change_id, **dispatch}


@router.post("/changes/{change_id}/phase-c/ship-kit")
async def ship_kit(change_id: str, body: ShipKitRequest, db: DbDep, user: CurrentUser, background: BackgroundTasks):
    """Unified communication: assign the selected banks (if needed) and ship a
    chosen EXISTING kit version to them in one action.

    Ships versions we already hold — an older version is re-sent from its exact
    published snapshot; the current version with no snapshot yet (first
    communication) is built + snapshotted. Never generates a new version.

    TODO(eval): this unified ship-kit flow post-dates the eval branch,
    so it is NOT gated by the PRODUCT_KIT_TO_PHASE_C advisory eval. The gate
    currently lives only on communicate_change. Decide whether ship-kit should
    also preflight the gate before dispatching to selected banks.
    """
    from app.models.kit_publication import KitPublication
    from app.services.change_dispatch import build_kit_envelope, snapshot_publication

    cr = db.get(ChangeRequest, change_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    if cr.status != ChangeStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Phase A must be completed before communicating to partners")
    if not body.partner_ids:
        raise HTTPException(status_code=400, detail="Select at least one bank to ship to")

    current_version = getattr(cr, "negotiation_version", 1) or 1
    version = body.negotiation_version or current_version

    # If a generated revision plan exists for this version, its summary is the
    # "summary of changes" document shipped alongside the kit.
    from app.models.kit_revision_plan import KitRevisionPlan, RP_STATUS_SHIPPED
    rev_plan = db.scalars(
        select(KitRevisionPlan).where(
            KitRevisionPlan.change_request_id == change_id,
            KitRevisionPlan.target_version == version,
        )
    ).first()
    plan_summary = (rev_plan.summary if rev_plan else None) or None

    include_doc_types = set(body.include_doc_types) if body.include_doc_types else None

    # Resolve the envelope for the requested (existing) version.
    pub = db.scalars(
        select(KitPublication).where(
            KitPublication.change_request_id == change_id,
            KitPublication.negotiation_version == version,
        )
    ).first()
    if pub is not None and include_doc_types is None:
        product_kit = pub.envelope
    elif version == current_version:
        # Fresh build. When include_doc_types is set we ALWAYS take this path
        # (even if a snapshot exists) so the user's item selection actually
        # takes effect — the older snapshot has already been sent verbatim.
        product_kit = build_kit_envelope(
            cr, db,
            change_summary=plan_summary,
            include_doc_types=include_doc_types,
        )
        if include_doc_types is None:
            snapshot_publication(
                cr, product_kit, db,
                revision_reason=(plan_summary[:480] if plan_summary else None),
                published_by=user.id if hasattr(user, "id") else None,
            )
    else:
        raise HTTPException(status_code=404, detail=f"Version v{version} was never published — nothing to ship")

    results, skipped = [], []
    for pid in body.partner_ids:
        partner = db.get(PartnerAgent, pid)
        if not partner or partner.status != PartnerStatus.ACTIVE:
            skipped.append(pid)
            continue
        assignment = db.scalars(
            select(ChangePartnerAssignment).where(
                ChangePartnerAssignment.change_request_id == change_id,
                ChangePartnerAssignment.partner_id == pid,
            )
        ).first()
        if assignment is None:
            assignment = ChangePartnerAssignment(
                id=generate_uuid(),
                change_request_id=change_id,
                partner_id=pid,
                status=AssignmentStatus.ASSIGNED,
            )
            db.add(assignment)
            db.commit()

        message = await notify_partner(
            partner.id,
            A2ATaskType.CHANGE_COMMUNICATION.value,
            product_kit,
            change_id=change_id,
            label=partner.name,
            context="product kit dispatch",
        )
        # already-communicated bank leaves the lifecycle untouched (re-acceptance
        # is gated partner-side on negotiation_version_accepted).
        if assignment.status == AssignmentStatus.ASSIGNED:
            set_status(
                assignment, AssignmentStatus.RECEIVED, db,
                actor_user_id=user.id if hasattr(user, "id") else None,
                reason=f"Product Kit v{version} dispatched",
            )
            db.commit()
        results.append({
            "partner_id": partner.id, "partner_name": partner.name,
            "task_id": message.reference if message else None,
            "delivery_status": message.status if message else "no_channel",
        })

    # Mark the revision plan shipped once its version has gone out.
    if rev_plan and rev_plan.status != RP_STATUS_SHIPPED and results:
        rev_plan.status = RP_STATUS_SHIPPED
        db.commit()

    # First kit ship → auto-segregate BRD requirements (idempotent; runs once).
    from app.services.brd_requirements import auto_segregate_brd_requirements
    background.add_task(auto_segregate_brd_requirements, change_id)

    logger.info(
        "Ship-kit: change=%s v=%d notified=%d skipped=%d",
        change_id, version, len(results), len(skipped),
    )
    return {
        "change_id": change_id,
        "negotiation_version": version,
        "partners_notified": len(results),
        "partners_skipped": len(skipped),
        "results": results,
    }


# ── Phase C Status Dashboard ─────────────────────────────────────────────────

@router.get("/changes/{change_id}/phase-c/status")
def get_phase_c_status(change_id: str, db: DbDep, _: CurrentUser):
    """Get the full Phase C status for a change request — all partners and their progress."""
    assignments = db.scalars(
        select(ChangePartnerAssignment)
        .where(ChangePartnerAssignment.change_request_id == change_id)
    ).all()

    partners_status = []
    for a in assignments:
        partner = db.get(PartnerAgent, a.partner_id)
        if not partner:
            continue

        # Count messages
        msg_count = len(db.scalars(
            select(A2AMessage).where(
                A2AMessage.change_request_id == change_id,
                A2AMessage.partner_id == partner.id,
            )
        ).all())

        partners_status.append({
            "partner_id": partner.id,
            "name": partner.name,
            "partner_type": partner.partner_type.value if hasattr(partner.partner_type, 'value') else partner.partner_type,
            "status": a.status.value if hasattr(a.status, 'value') else a.status,
            "message_count": msg_count,
            "assigned_at": a.assigned_at.isoformat(),
        })

    return {
        "change_id": change_id,
        "total_partners": len(partners_status),
        "partners": partners_status,
    }


# ── A2A Message Audit Log ────────────────────────────────────────────────────

@router.get("/changes/{change_id}/phase-c/messages")
def list_a2a_messages(change_id: str, db: DbDep, _: CurrentUser, limit: int = 100):
    """List all A2A messages for a change request (audit trail)."""
    messages = db.scalars(
        select(A2AMessage)
        .where(A2AMessage.change_request_id == change_id)
        .order_by(A2AMessage.created_at.desc())
        .limit(limit)
    ).all()

    result = []
    for m in messages:
        partner = db.get(PartnerAgent, m.partner_id)
        result.append({
            "task_id": m.id,
            "partner_name": partner.name if partner else "Unknown",
            "direction": m.direction.value if hasattr(m.direction, 'value') else m.direction,
            "task_type": m.task_type.value if hasattr(m.task_type, 'value') else m.task_type,
            "status": m.status,
            "payload": m.payload,
            "partner_id": m.partner_id,
            "created_at": m.created_at.isoformat(),
        })

    return result


# ── Negotiation ──────────────────────────────────────────────────────────────

class NegotiationRespondRequest(BaseModel):
    response_text: str
    # Target ONE counter-proposal instead of every open one. Without this, a reply swept
    # every open partner counter for the (change, partner) into ACCEPTED — so answering
    # one question silently accepted every other outstanding ask from that bank, each
    # stamped with the same unrelated resolution text. None keeps the legacy sweep.
    counter_proposal_id: str | None = None
    # Target ONE blocker (kind='blocker'). The reply resolves it and sends
    # BLOCKER_RESOLUTION — the structured Resolve card is gone, so this is the single
    # action that closes a blocker and clears the assignment's blocked flag.
    blocker_id: str | None = None
    # Which partner QUERY this reply answers, as that query's correlation_id.
    # Without it the endpoint could only guess "the newest open query", so
    # answering the first of several in-flight queries went out stamped with
    # the last one's id and the partner marked the WRONG query answered.
    # None keeps the legacy newest-wins fallback for older clients.
    query_correlation_id: str | None = None


def _pick_correlation_id(partner_msgs, requested: str | None) -> str | None:
    """The correlation_id the outbound clarification_response must echo.

    `partner_msgs` are this thread's correlated partner queries, NEWEST FIRST.
    An explicit `requested` wins when it names one of them; an unknown id is
    ignored rather than echoed, so a stale client can't address a query that
    isn't in this thread. Falling back to newest-first preserves the old
    behaviour, which is correct whenever only one query is open."""
    ids = [m.correlation_id for m in partner_msgs if getattr(m, "correlation_id", None)]
    if requested and requested in ids:
        return requested
    return ids[0] if ids else None


@router.get("/changes/{change_id}/partners/{partner_id}/negotiation")
def get_negotiation_thread(
    change_id: str,
    partner_id: str,
    db: DbDep,
    _: CurrentUser,
    kind: str = "general",
):
    """Get the negotiation Q&A thread for a partner on a change request.

    Args:
        kind: 'general' (default — Phase C clarifications), 'cert' (cert-channel
              clarifications), or 'blocker' (operational escalations). Threads are
              partitioned by kind so the same (change, partner) pair holds parallel
              inboxes that never share rows.

              'blocker' must be accepted here: blockers now dual-write to their own
              thread, so without it the UI has no way to read them back and a reported
              blocker would be invisible in the message stream.
    """
    if kind not in ("general", "cert", "blocker"):
        kind = "general"

    thread = db.scalars(
        select(NegotiationThread).where(
            NegotiationThread.change_request_id == change_id,
            NegotiationThread.partner_id == partner_id,
            NegotiationThread.kind == kind,
        )
    ).first()

    # A (change, partner) pair can have counter-proposals or blockers
    # without ever having a NegotiationMessage thread (partner skipped
    # Q&A and went straight to counter). Don't early-return when
    # `thread is None` — let the extras section below surface the rest.
    msgs = []
    if thread:
        # Step 4 cutover: NegotiationMessage is the single source of
        # truth for the timeline. Linked NMs (counter_proposal_id or
        # blocker_id set) represent structured events; unlinked NMs
        # are plain Q&A chat. Both are fetched here and serialized
        # below — no more synthetic event emission from CP/Blocker
        # tables.
        msgs = db.scalars(
            select(NegotiationMessage)
            .where(NegotiationMessage.thread_id == thread.id)
            .order_by(NegotiationMessage.created_at)
        ).all()

    # For the chat UI to tag counter-proposals, fetch all inbound
    # query A2A messages for this partner+change and index by created_at.
    # We then match each PARTNER thread message to the nearest A2A row
    # (within ~5s) — the partner thread message is created from the A2A
    # message in process_partner_query, so timestamps align closely.
    # Protocol-v1 cert_query fold: cert and general queries now share
    # task_type='query' (distinguished by payload phase), with legacy
    # task_type='cert_query' rows still present. Fetch both and partition
    # by channel in Python so the message_kind lookup stays channel-scoped.
    _query_rows = db.scalars(
        select(A2AMessage)
        .where(
            A2AMessage.change_request_id == change_id,
            A2AMessage.partner_id == partner_id,
            A2AMessage.task_type.in_(["query", "cert_query"]),
            A2AMessage.direction == A2ADirection.INBOUND,
        )
        .order_by(A2AMessage.created_at)
    ).all()
    a2a_rows = [r for r in _query_rows if _query_row_in_channel(r, kind)]

    def _kind_for(m_created_at):
        """Find the most recent A2A query row whose row pre-dates this
        partner thread message and return its payload.message_kind
        (e.g. 'COUNTER_PROPOSAL'). Returns None if no A2A row matches.

        Match-by-ordering rather than match-by-timestamp-window: the
        gap between partner-sends-query and the Authority-clicks-Generate-AI
        can be hours in real-world use, so a tight time window misses.
        Each NegotiationMessage is created from the latest pending A2A
        row, so the most recent A2A row that pre-dates the thread row
        is the right pairing.
        """
        candidates = [r for r in a2a_rows if r.created_at <= m_created_at]
        if not candidates:
            return None
        latest = max(candidates, key=lambda r: r.created_at)
        raw = latest.payload or {}
        inner = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        return inner.get("message_kind")

    # Scoped to 'general' kind — cert threads don't carry counters /
    # blockers today (those are rollout-stage concepts). Use the
    # request `kind` rather than `thread.kind` because `thread` is None
    # for any (change, partner) pair that has counter-proposals or
    # blockers but no Q&A history yet (the most common state for a
    # partner that jumped straight to counter).
    #
    # Step 4 cutover: CP and Blocker rows are still fetched so the
    # status_strip block below knows which are open, and so we can
    # inline structured metadata (round, status, severity) onto the
    # corresponding linked NM rows. But the synthetic-event emission
    # is gone — every CP/Blocker that should appear in the timeline
    # already has a `negotiation_messages` row pointing back at it
    # (written by dual-write at create/resolve time, or by migration
    # 0055 for historical rows).
    cp_rows: list[CounterProposal] = []
    blocker_rows: list[Blocker] = []
    if kind == "general":
        cp_rows = db.scalars(
            select(CounterProposal).where(
                CounterProposal.change_request_id == change_id,
                CounterProposal.partner_id == partner_id,
            ).order_by(CounterProposal.created_at)
        ).all()
    # Blockers must load for the BLOCKER thread too — they were previously fetched only
    # for kind='general', which was correct when blockers lived in that thread. Now they
    # have their own, so the lookup map was empty exactly where it is needed: every
    # blocker row fell through to a bare 'po_approved' bubble with no badge and no
    # in-reply-to context.
    if kind in ("general", "blocker"):
        blocker_rows = db.scalars(
            select(Blocker).where(
                Blocker.change_request_id == change_id,
                Blocker.partner_id == partner_id,
            ).order_by(Blocker.created_at)
        ).all()

    # Lookup maps for inline-enrichment of linked NM rows below.
    cp_by_id = {cp.id: cp for cp in cp_rows}
    blocker_by_id = {b.id: b for b in blocker_rows}

    def _serialize(m: NegotiationMessage) -> dict:
        """Build the wire dict for a NegotiationMessage. When the row
        is linked to a CP or Blocker (via event_kind + FK), inline the
        structured metadata under synthetic role names the FE already
        knows ('counter_proposal' / 'counter_resolution' / 'blocker' /
        'blocker_resolution') — keeps the FE's bubble + badge logic
        unchanged across the cutover."""
        base_role = m.role.value if hasattr(m.role, 'value') else m.role
        entry: dict = {
            "id": m.id,
            "role": base_role,
            "content": m.content,
            "ai_draft": m.ai_draft,
            # Which query this row belongs to: set on the partner's query, on the
            # AI draft written for it, and on the PO reply that answered it. The UI
            # pairs them on this — without it every draft but the last was orphaned.
            "correlation_id": m.correlation_id,
            "approved_by": m.approved_by,
            "created_at": m.created_at.isoformat(),
            "message_kind": (
                _kind_for(m.created_at) if base_role == "partner" else None
            ),
        }
        if m.event_kind == "proposal" and m.counter_proposal_id:
            cp = cp_by_id.get(m.counter_proposal_id)
            if cp is not None:
                status = cp.status.value if hasattr(cp.status, "value") else cp.status
                entry.update({
                    "role":                "counter_proposal",
                    "originator":          cp.originator,
                    "status":              status,
                    "round":               cp.negotiation_round,
                    "counter_proposal_id": cp.counter_proposal_id,
                    "message_kind":        "COUNTER_PROPOSAL",
                })
        elif m.event_kind == "resolution" and m.counter_proposal_id:
            cp = cp_by_id.get(m.counter_proposal_id)
            if cp is not None:
                status = cp.status.value if hasattr(cp.status, "value") else cp.status
                is_system = (cp.resolution_text or "") in {
                    "Closed by partner's new counter",
                    "Accepted as-is",
                }
                entry.update({
                    "role":                "counter_resolution",
                    "originator":          "npci" if cp.originator == "partner" else "partner",
                    "status":              status,
                    "round":               cp.negotiation_round,
                    "counter_proposal_id": cp.counter_proposal_id,
                    "message_kind":        "COUNTER_RESOLUTION",
                    "kind":                "system" if is_system else "message",
                })
        elif m.event_kind == "blocker" and m.blocker_id:
            b = blocker_by_id.get(m.blocker_id)
            if b is not None:
                sev = b.severity.value if hasattr(b.severity, "value") else b.severity
                bst = b.status.value if hasattr(b.status, "value") else b.status
                entry.update({
                    "role":       "blocker",
                    "originator": "partner",
                    "status":     bst,
                    "severity":   sev,
                    "blocker_id": b.blocker_id,
                    "message_kind": "BLOCKER",
                })
        elif m.event_kind == "blocker_resolution" and m.blocker_id:
            b = blocker_by_id.get(m.blocker_id)
            if b is not None:
                bst = b.status.value if hasattr(b.status, "value") else b.status
                entry.update({
                    "role":       "blocker_resolution",
                    "originator": "npci",
                    "status":     bst,
                    "blocker_id": b.blocker_id,
                    "message_kind": "BLOCKER_RESOLUTION",
                })
        # "In reply to" excerpt. A targeted PM reply stores the CP/Blocker it answers;
        # surface a short quote so the timeline shows WHICH message it responds to
        # instead of an unattached bubble.
        _src = None
        if m.counter_proposal_id:
            _cp = cp_by_id.get(m.counter_proposal_id)
            _src = getattr(_cp, "justification", None)
        elif m.blocker_id:
            _bk = blocker_by_id.get(m.blocker_id)
            _src = getattr(_bk, "description", None)
        if _src and entry.get("role") not in ("counter_proposal", "blocker"):
            entry["in_reply_to"] = (_src or "")[:120]
        return entry

    msg_entries = [_serialize(m) for m in msgs]

    # Fix #1 carried forward: when a counter-back creates a
    # resolution NM and an immediately-following proposal NM with
    # the same content (PM typed once, two rows landed), render once.
    # `extras` is now always empty — kept for back-compat with anything
    # external that grew a dependency on the variable name.
    filtered: list[dict] = []
    for i, entry in enumerate(msg_entries):
        if entry.get("role") == "counter_resolution":
            nxt = msg_entries[i + 1] if i + 1 < len(msg_entries) else None
            if (
                nxt is not None
                and nxt.get("role") == "counter_proposal"
                and nxt.get("content") == entry.get("content")
                and nxt.get("originator") == entry.get("originator")
                and (nxt.get("round") or 0) == (entry.get("round") or 0) + 1
            ):
                continue
        filtered.append(entry)

    # Sort by created_at so the unified chat reads chronologically.
    # (Already ordered by query, but linked-NM creation times can
    # tie with their structured record's created_at — explicit sort
    # keeps the contract stable.)
    unified = sorted(filtered, key=lambda x: x["created_at"])

    # ── Status strip — at-a-glance "what needs my attention" ───────────────
    # authority-side viewer. "awaiting_action" enumerates items the PM has to
    # action: open partner-originated counters, open blockers, and the
    # tail of the Q&A thread when the last message is from the partner.
    # Frontend renders this as a sticky header above the timeline so a
    # blocker raised three days ago doesn't get lost in scrollback.
    open_counters = sum(
        1 for cp in cp_rows if cp.status == CounterProposalStatus.OPEN
    )
    open_blockers = sum(
        1 for b in blocker_rows if b.status == BlockerStatus.OPEN
    )
    awaiting_action: list[dict] = []
    for cp in cp_rows:
        if cp.status == CounterProposalStatus.OPEN and cp.originator == "partner":
            awaiting_action.append({
                "kind":  "counter",
                "ref":   cp.counter_proposal_id,
                "label": f"Counter · Round {cp.negotiation_round}",
            })
    for b in blocker_rows:
        if b.status == BlockerStatus.OPEN:
            sev = b.severity.value if hasattr(b.severity, "value") else b.severity
            awaiting_action.append({
                "kind":  "blocker",
                "ref":   b.blocker_id,
                "label": f"Blocker · {str(sev).upper()}",
            })
    if msgs:
        last = msgs[-1]
        last_role = last.role.value if hasattr(last.role, "value") else last.role
        if last_role == "partner":
            awaiting_action.append({
                "kind":  "query",
                "ref":   last.id,
                "label": "Partner question awaiting reply",
            })

    return {
        "thread_id": thread.id if thread else None,
        "kind": (thread.kind if thread else None) or kind,
        "status": (
            thread.status.value if (thread and hasattr(thread.status, 'value'))
            else (thread.status if thread else None)
        ),
        "messages": unified,
        "status_strip": {
            "open_counters":   open_counters,
            "open_blockers":   open_blockers,
            "awaiting_action": awaiting_action,
        },
    }


@router.post("/changes/{change_id}/partners/{partner_id}/negotiation/query")
async def receive_partner_query(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """
    Process a pending partner query — generate AI draft response.

    Triggered when a partner sends a QUERY task via A2A (or simulated by admin).
    """
    pending_query = db.scalars(
        select(A2AMessage).where(
            A2AMessage.change_request_id == change_id,
            A2AMessage.partner_id == partner_id,
            A2AMessage.task_type == A2ATaskType.QUERY,
            A2AMessage.status == "submitted",
        ).order_by(A2AMessage.created_at.desc())
    ).first()

    if not pending_query or not pending_query.payload:
        raise HTTPException(status_code=404, detail="No pending query from this partner")

    # Post-Slice-8 the audit row stores the full A2A wrapper:
    # `{task_type, change_id, payload: {...}, from}`. Pre-Slice-8 rows
    # had the inner payload directly. Read both shapes so old +
    # COUNTER_PROPOSAL rows (which carry `justification` instead of
    # `message`) all work.
    raw = pending_query.payload or {}
    inner = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    query_text = inner.get("message") or inner.get("justification") or ""
    if not query_text:
        raise HTTPException(status_code=400, detail="Query message is empty")
    # The partner's OutgoingQuery id — the key everything downstream pairs on.
    query_correlation_id = inner.get("correlation_id") or raw.get("correlation_id") or None

    cr = db.get(ChangeRequest, change_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")

    # Get or create thread
    thread = db.scalars(
        select(NegotiationThread).where(
            NegotiationThread.change_request_id == change_id,
            NegotiationThread.partner_id == partner_id,
        )
    ).first()

    if not thread:
        thread = NegotiationThread(
            id=generate_uuid(),
            change_request_id=change_id,
            partner_id=partner_id,
            status=ThreadStatus.OPEN,
        )
        db.add(thread)
        db.commit()
        db.refresh(thread)

    # Save partner query
    partner_msg = NegotiationMessage(
        id=generate_uuid(),
        thread_id=thread.id,
        role=NegotiationRole.PARTNER,
        content=query_text,
        correlation_id=query_correlation_id,
    )
    db.add(partner_msg)
    db.commit()

    # Load context
    brd = db.scalars(select(BRD).where(BRD.change_request_id == change_id).order_by(BRD.version.desc())).first()
    tech_spec = db.scalars(select(TechSpec).where(TechSpec.change_request_id == change_id).order_by(TechSpec.version.desc())).first()

    thread_msgs = db.scalars(
        select(NegotiationMessage).where(NegotiationMessage.thread_id == thread.id).order_by(NegotiationMessage.created_at)
    ).all()
    history = [{"role": m.role.value, "content": m.content} for m in thread_msgs[:-1]]

    logger.info("Negotiation AI draft: change=%s partner=%s query_len=%d", change_id, partner_id, len(query_text))

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
        correlation_id=query_correlation_id,
    )
    db.add(draft_msg)
    pending_query.status = "working"
    db.commit()

    logger.info("Negotiation AI draft ready: change=%s partner=%s draft_len=%d", change_id, partner_id, len(ai_draft))

    return {
        "query": query_text,
        "ai_draft": ai_draft,
        "draft_message_id": draft_msg.id,
        "thread_id": thread.id,
    }


@router.post("/changes/{change_id}/partners/{partner_id}/negotiation/respond")
async def approve_and_respond(
    change_id: str,
    partner_id: str,
    body: NegotiationRespondRequest,
    db: DbDep,
    user: CurrentUser,
    kind: str = "general",
):
    """PO approves and sends a response to the partner's query.

    Args:
        kind: 'general' | 'cert' | 'blocker' — selects the channel-specific thread
              and the matching inbound task_type to mark completed.
              The outbound CLARIFICATION_RESPONSE always carries the
              `channel` discriminator in payload so the partner UI can
              route it to the correct inbox.

              'blocker' is a free-text note ON a blocker: it lands in the blocker
              thread, goes out as BLOCKER_STATUS_UPDATE (interim, non-terminal), and
              deliberately does NOT settle counter-proposals or close the blocker —
              only the structured /resolve endpoint closes one. This keeps the blocker
              conversation and the negotiation conversation genuinely separate.
    """
    if kind not in ("general", "cert", "blocker"):
        kind = "general"

    thread = db.scalars(
        select(NegotiationThread).where(
            NegotiationThread.change_request_id == change_id,
            NegotiationThread.partner_id == partner_id,
            NegotiationThread.kind == kind,
        )
    ).first()

    if not thread:
        thread = NegotiationThread(
            id=generate_uuid(),
            change_request_id=change_id,
            partner_id=partner_id,
            kind=kind,
            status=ThreadStatus.OPEN,
        )
        db.add(thread)
        db.flush()

    approved_msg = NegotiationMessage(
        id=generate_uuid(),
        thread_id=thread.id,
        role=NegotiationRole.PO_APPROVED,
        content=body.response_text,
        approved_by=user.id,
        # Link the reply to what it answers, so the timeline can show "in reply to X".
        # Without this a PM reply was an unattached bubble — with several counters open
        # there was no way to tell which one it addressed.
        counter_proposal_id=body.counter_proposal_id if kind == "general" else None,
    )
    db.add(approved_msg)

    # Look up the correlation_id of the most-recent partner message in
    # this thread — that's the OutgoingQuery the partner sent and is
    # awaiting an answer on. Echoing it back lets the partner handler
    # attach the response to the EXACT query, not "most recent in
    # channel" (which silently mis-routed responses when more than one
    # query was in flight). NULL is fine — the partner falls back to
    # its legacy matching for in-flight queries from older builds.
    # Pick the most-recent partner query that actually carries a correlation_id
    # — i.e. the OutgoingQuery awaiting an answer. We filter out null-correlation
    # rows (counter-proposals, and any duplicate partner rows written without a
    # correlation_id) so a stray null row can't shadow the real query and break
    # the echo. Falls back to None only when no correlated query exists.
    partner_queries = db.scalars(
        select(NegotiationMessage)
        .where(
            NegotiationMessage.thread_id == thread.id,
            NegotiationMessage.role == NegotiationRole.PARTNER,
            NegotiationMessage.correlation_id.is_not(None),
        )
        .order_by(NegotiationMessage.created_at.desc())
    ).all()
    correlation_id = _pick_correlation_id(partner_queries, body.query_correlation_id)
    # Stamp the reply with the query it answered so the thread records the pairing
    # (and the UI can tell which queries are still outstanding).
    approved_msg.correlation_id = correlation_id

    partner = db.get(PartnerAgent, partner_id)
    if partner:
        # v1.1: spec-shaped clarification_response (A2A v1.0 §clarification_response).
        # query_id/responded_at/approver/generation_mode/references are populated
        # for real; interpretation_confirmed/grounded_in/amends_change/amendment_ref
        # are spec-conformant defaults pending upstream capture (Phase 1). The
        # legacy `response`/`channel`/`correlation_id` are retained for the current
        # partner handler until the query_id cutover completes.
        _now = utcnow().isoformat()
        payload = {
            "change_id": change_id,
            "query_id": correlation_id,
            "responded_at": _now,
            "response": body.response_text,
            "interpretation_confirmed": None,
            "grounded_in": [],
            "amends_change": False,
            "amendment_ref": None,
            "approver": {
                "role": getattr(user.role, "value", str(user.role)),
                "name": user.full_name or user.username,
                "email": user.email,
                "approved_at": _now,
            },
            "generation_mode": "human_authored",
            "references": (
                [{"type": "query", "id": correlation_id, "relation": "answers"}]
                if correlation_id else []
            ),
            # Channel discriminator so the partner-side handler routes
            # this back into the cert messaging inbox (vs general).
            "channel": kind,
        }
        if correlation_id:
            payload["correlation_id"] = correlation_id
        if kind == "blocker":
            # A blocker-tab reply is an INTERIM, non-terminal status note — it does NOT
            # close the blocker. Only the structured /resolve endpoint closes one (the UI
            # exposes a Resolve card for that). Attach the note to the blocker it targets
            # and push a BLOCKER_STATUS_UPDATE so the partner sees investigation progress
            # without being told the blocker is fixed.
            _b = None
            if body.blocker_id:
                _b = db.get(Blocker, body.blocker_id)
            if _b is None:
                # No explicit target (e.g. reply typed without picking a message):
                # fall back to the oldest still-open blocker for this partner.
                _b = db.scalars(
                    select(Blocker).where(
                        Blocker.change_request_id == change_id,
                        Blocker.partner_id == partner_id,
                        Blocker.status == BlockerStatus.OPEN,
                    ).order_by(Blocker.created_at.asc())
                ).first()

            if _b is not None and _b.status == BlockerStatus.OPEN:
                approved_msg.blocker_id = _b.id
                approved_msg.event_kind = "blocker_status_update"
                from app.a2a_common import protocol as _proto
                await notify_partner(
                    partner.id,
                    _proto.A2ATaskType.BLOCKER_STATUS_UPDATE.value,
                    {
                        "in_response_to_blocker": _b.blocker_id,
                        "blocker_id":             _b.blocker_id,
                        "status":                 "update",
                        "notes":                  body.response_text,
                    },
                    change_id=change_id,
                    label=partner.name,
                    correlation_id=correlation_id,
                    context="blocker status update",
                )
            # No open blocker to attach to → the note still lands in the blocker thread as a
            # plain bubble, but nothing is pushed: a status update against a non-existent or
            # already-closed blocker would only mislead the partner.
        else:
            await notify_partner(
                partner.id,
                A2ATaskType.CLARIFICATION_RESPONSE.value,
                payload,
                change_id=change_id,
                label=partner.name,
                correlation_id=correlation_id,
                context="clarification response",
            )

    # Mark pending queries as completed — channel-scoped (protocol-v1 fold:
    # match both 'query' and legacy 'cert_query', partition by payload phase).
    _pending_rows = db.scalars(
        select(A2AMessage).where(
            A2AMessage.change_request_id == change_id,
            A2AMessage.partner_id == partner_id,
            A2AMessage.task_type.in_(["query", "cert_query"]),
            A2AMessage.status.in_(["submitted", "working"]),
        )
    ).all()
    pending = [p for p in _pending_rows if _query_row_in_channel(p, kind)]
    for p in pending:
        p.status = "completed"

    # Unified negotiation flow: the accept/reject counter cards are gone, so a
    # free-text PM reply IS the resolution. Settle any open partner counter(s)
    # for this change/partner so the structured status updates underneath and
    # they stop gating rollout progression. (general channel only — cert Q&A
    # never creates counters.)
    # 'blocker' notes never settle counter-proposals — a blocker is a different
    # conversation, and only /resolve closes one.
    if kind == "general":
        _q = select(CounterProposal).where(
            CounterProposal.change_request_id == change_id,
            CounterProposal.partner_id == partner_id,
            CounterProposal.status == CounterProposalStatus.OPEN,
            CounterProposal.originator == "partner",
        )
        # Targeted reply: settle ONLY the counter the PM replied to. Replying to one of
        # three open counters used to accept all three, stamping the same text on each —
        # including ones the reply never mentioned, or explicitly refused.
        if body.counter_proposal_id:
            _q = _q.where(CounterProposal.id == body.counter_proposal_id)
        open_cps = db.scalars(_q).all()
        for cp in open_cps:
            # ACCEPTED is the only non-OPEN status that unblocks progression;
            # in the unified model it means "answered/resolved by the PM reply".
            cp.status = CounterProposalStatus.ACCEPTED
            cp.resolved_at = utcnow()
            cp.resolved_by = user.id
            cp.resolution_text = body.response_text

    db.commit()
    logger.info("Negotiation response sent: change=%s partner=%s kind=%s by=%s", change_id, partner_id, kind, user.username)
    # Return the freshly-built thread so the Authority UI can hydrate its
    # query cache via `setQueryData` instead of a second roundtrip —
    # eliminates the post-send refetch lag (formerly 0-5s).
    fresh_thread = get_negotiation_thread(change_id, partner_id, db, user, kind)
    return {
        "sent": True,
        "message_id": approved_msg.id,
        "kind": kind,
        "thread": fresh_thread,
    }


# ── Counter-Proposals (Tier 1) ───────────────────────────────────────────────

class CounterRejectRequest(BaseModel):
    rationale: str


class CounterBackRequest(BaseModel):
    """PM's counter-back terms. Free-text justification — same shape
    the partner uses when sending their counter."""
    justification: str
    valid_until_days: int = 7


@router.get("/changes/{change_id}/partners/{partner_id}/counter-proposals")
def list_counter_proposals(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """List all counter-proposals for a partner on a change request,
    most recent first. Used by the chat sidebar to render open counters."""
    rows = db.scalars(
        select(CounterProposal)
        .where(
            CounterProposal.change_request_id == change_id,
            CounterProposal.partner_id == partner_id,
        )
        .order_by(CounterProposal.created_at.desc())
    ).all()
    return [
        {
            "id": cp.id,
            "counter_proposal_id": cp.counter_proposal_id,
            "status": cp.status.value,
            "originator": cp.originator,
            "negotiation_round": cp.negotiation_round,
            "justification": cp.justification,
            "valid_until": cp.valid_until.isoformat() if cp.valid_until else None,
            "created_at": cp.created_at.isoformat(),
            "resolved_at": cp.resolved_at.isoformat() if cp.resolved_at else None,
            "resolved_by": cp.resolved_by,
            "resolution_text": cp.resolution_text,
        }
        for cp in rows
    ]


async def _send_counter_decision(partner: PartnerAgent, change_id: str, cp: CounterProposal, decision: str, text: str | None, db: DbDep):
    """Helper — send a CLARIFICATION_RESPONSE back to the partner so
    they see the counter decision in their chat thread. Async so
    callers (the accept/reject endpoints) can await directly inside
    the FastAPI request loop."""
    _decision_map = {"ACCEPT": "accepted", "REJECT": "rejected"}
    await notify_partner(
        partner.id,
        A2ATaskType.CLARIFICATION_RESPONSE.value,
        {
            "message_kind":          "COUNTER_DECISION",
            "in_response_to":        cp.counter_proposal_id,
            # v1.1 spec-shaped (A2A v1.0 §counter_decision): the flat decision is
            # wrapped in a per_counter[] array-of-one; clause_ref/modified_text are
            # null pending per-clause capture. Still delivered inside
            # clarification_response (de-overloading is deferred).
            "decided_at":            utcnow().isoformat(),
            "per_counter": [
                {
                    "clause_ref":    None,
                    "decision":      _decision_map.get(decision, decision.lower()),
                    "modified_text": None,
                    "reasoning":     text or "",
                }
            ],
            "references": [
                {"type": "counter_proposal", "id": cp.counter_proposal_id, "relation": "decides"}
            ],
            # Bilateral-extension / back-compat fields (existing partner consumers
            # read these):
            "decision":              decision,                # "ACCEPT" or "REJECT"
            "negotiation_round":     cp.negotiation_round,
            "resolution_text":       text or "",
            "response":              text or f"Counter-proposal {decision.lower()}ed",
            # Echo back the partner's original counter so the partner UI
            # can render the decision as a real reply ("Accepting your
            # counter: '<original text>'") instead of an orphaned
            # "Counter accepted" bubble floating at the bottom of the
            # chat hours after the original counter scrolled off.
            "original_justification": cp.justification or "",
        },
        change_id=change_id,
        label=partner.name,
        context="counter decision",
    )


@router.post("/changes/{change_id}/partners/{partner_id}/counter-proposals/{cp_id}/accept")
async def accept_counter_proposal(
    change_id: str, partner_id: str, cp_id: str,
    db: DbDep, user: CurrentUser,
):
    """PM accepts the partner's counter terms. Resolves the counter,
    sends decision back to partner, and unblocks state progression."""
    cp = db.scalars(
        select(CounterProposal).where(
            CounterProposal.id == cp_id,
            CounterProposal.change_request_id == change_id,
            CounterProposal.partner_id == partner_id,
        )
    ).first()
    if not cp:
        raise HTTPException(status_code=404, detail="Counter-proposal not found")
    if cp.status != CounterProposalStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Counter-proposal already {cp.status.value}")

    cp.status = CounterProposalStatus.ACCEPTED
    cp.resolved_at = utcnow()
    cp.resolved_by = user.id
    cp.resolution_text = "Accepted as-is"
    try:
        from app.services.negotiation_service import record_linked_message
        record_linked_message(
            db,
            change_request_id=change_id,
            partner_id=partner_id,
            role="po_approved",
            content="Accepted as-is",
            event_kind="resolution",
            counter_proposal_id=cp.id,
            approved_by=user.id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Dual-write NM failed for counter accept; CP still resolved")
    db.commit()

    partner = db.get(PartnerAgent, partner_id)
    if partner:
        await _send_counter_decision(partner, change_id, cp, "ACCEPT", "Counter accepted", db)

    logger.info("Counter accepted: change=%s partner=%s cp=%s by=%s", change_id, partner_id, cp.counter_proposal_id, user.username)
    return {"resolved": True, "status": "accepted"}


@router.post("/changes/{change_id}/partners/{partner_id}/counter-proposals/{cp_id}/reject")
async def reject_counter_proposal(
    change_id: str, partner_id: str, cp_id: str,
    body: CounterRejectRequest,
    db: DbDep, user: CurrentUser,
):
    """PM rejects the partner's counter. Resolves the counter (partner
    can still re-counter up to MAX_NEGOTIATION_ROUNDS or accept original)."""
    cp = db.scalars(
        select(CounterProposal).where(
            CounterProposal.id == cp_id,
            CounterProposal.change_request_id == change_id,
            CounterProposal.partner_id == partner_id,
        )
    ).first()
    if not cp:
        raise HTTPException(status_code=404, detail="Counter-proposal not found")
    if cp.status != CounterProposalStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Counter-proposal already {cp.status.value}")
    if not body.rationale.strip():
        raise HTTPException(status_code=400, detail="Rejection rationale required")

    cp.status = CounterProposalStatus.REJECTED
    cp.resolved_at = utcnow()
    cp.resolved_by = user.id
    cp.resolution_text = body.rationale
    try:
        from app.services.negotiation_service import record_linked_message
        record_linked_message(
            db,
            change_request_id=change_id,
            partner_id=partner_id,
            role="po_approved",
            content=body.rationale,
            event_kind="resolution",
            counter_proposal_id=cp.id,
            approved_by=user.id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Dual-write NM failed for counter reject; CP still resolved")
    db.commit()

    partner = db.get(PartnerAgent, partner_id)
    if partner:
        await _send_counter_decision(partner, change_id, cp, "REJECT", body.rationale, db)

    logger.info("Counter rejected: change=%s partner=%s cp=%s by=%s", change_id, partner_id, cp.counter_proposal_id, user.username)
    return {"resolved": True, "status": "rejected"}


@router.post("/changes/{change_id}/partners/{partner_id}/counter-proposals/{cp_id}/counter")
async def counter_back_proposal(
    change_id: str, partner_id: str, cp_id: str,
    body: CounterBackRequest,
    db: DbDep, user: CurrentUser,
):
    """PM responds to a partner counter with their own counter terms —
    the multi-round-negotiation flow from Journey B of the rollout doc.

    Marks the partner's counter as `countered_back` and creates a new
    row with originator='npci' that's now waiting for partner action.
    The partner sees PM's terms in their chat (CLARIFICATION_RESPONSE
    with a structured COUNTER_PROPOSAL payload) and can either Accept
    (full PROPOSAL_ACCEPTANCE) or counter again.
    """
    cp = db.scalars(
        select(CounterProposal).where(
            CounterProposal.id == cp_id,
            CounterProposal.change_request_id == change_id,
            CounterProposal.partner_id == partner_id,
        )
    ).first()
    if not cp:
        raise HTTPException(status_code=404, detail="Counter-proposal not found")
    if cp.status != CounterProposalStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Counter-proposal already {cp.status.value}")
    if not body.justification.strip():
        raise HTTPException(status_code=400, detail="Counter-back justification required")

    # Round-cap enforcement against the assignment's total counters.
    from app.a2a_common.authority_handlers import MAX_NEGOTIATION_ROUNDS
    total_rounds = db.scalar(
        select(func.count(CounterProposal.id)).where(
            CounterProposal.assignment_id == cp.assignment_id,
        )
    ) or 0
    if total_rounds >= MAX_NEGOTIATION_ROUNDS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_NEGOTIATION_ROUNDS} negotiation rounds reached",
        )

    # 1) Mark partner's counter as countered-back.
    cp.status = CounterProposalStatus.COUNTERED_BACK
    cp.resolved_at = utcnow()
    cp.resolved_by = user.id
    cp.resolution_text = body.justification

    # 2) Create the new authority-originated counter that the partner will see.
    from datetime import timedelta
    authority_cp_id = f"authority-counter-{generate_uuid()[:12]}"
    authority_valid_until = utcnow() + timedelta(days=body.valid_until_days)
    authority_cp = CounterProposal(
        id=generate_uuid(),
        change_request_id=change_id,
        partner_id=partner_id,
        assignment_id=cp.assignment_id,
        counter_proposal_id=authority_cp_id,
        status=CounterProposalStatus.OPEN,
        originator="npci",
        negotiation_round=cp.negotiation_round + 1,
        justification=body.justification,
        valid_until=authority_valid_until,
        payload={
            "message_kind":         "COUNTER_PROPOSAL",
            "in_response_to":       cp.counter_proposal_id,
            "counter_proposal_id":  authority_cp_id,
            "decision":             "COUNTER",
            "negotiation_round":    cp.negotiation_round + 1,
            "justification":        body.justification,
            "valid_until":          authority_valid_until.isoformat(),
        },
    )
    db.add(authority_cp)
    db.flush()
    # Dual-write: the partner CP closes (resolution event) and the new
    # The Authority CP opens (proposal event). Mirror both onto the unified
    # negotiation timeline. Fix #1 in get_negotiation_thread already
    # suppresses the resolution-event synthetic bubble when its text
    # equals the next round's justification — we keep that working
    # by writing both NMs here regardless.
    try:
        from app.services.negotiation_service import record_linked_message
        record_linked_message(
            db,
            change_request_id=change_id,
            partner_id=partner_id,
            role="po_approved",
            content=body.justification,
            event_kind="resolution",
            counter_proposal_id=cp.id,
            approved_by=user.id,
        )
        record_linked_message(
            db,
            change_request_id=change_id,
            partner_id=partner_id,
            role="po_approved",
            content=body.justification,
            event_kind="proposal",
            counter_proposal_id=authority_cp.id,
            approved_by=user.id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Dual-write NM failed for counter-back; CP rows still persisted")

    # Open a fresh round for the partner to respond to the Authority's counter.
    # Round number tracks the new the Authority counter's negotiation_round so the
    # Hub's per-partner round panel advances past round 1.
    #
    # NOTE: this is intra-round message plumbing (a PM counter is a reply
    # inside the existing 24h window), NOT a real round transition. We do
    # NOT send round_opened here — that notice is reserved for genuine
    # window transitions (kit delivered, PM force-close, silent advance,
    # new version ship). Partner learns of PM's counter via the counter
    # message itself.
    try:
        from app.services.negotiation_extended import create_round_state
        create_round_state(change_id, partner_id, round_number=cp.negotiation_round + 1, db=db)
    except Exception:
        logger.exception("Failed to create round state on counter-back change=%s partner=%s", change_id, partner_id)

    db.commit()

    # 3) Send the Authority's counter to the partner so it appears in their thread.
    partner = db.get(PartnerAgent, partner_id)
    if partner:
        await notify_partner(
            partner.id,
            A2ATaskType.CLARIFICATION_RESPONSE.value,
            {
                "message_kind":         "COUNTER_PROPOSAL",
                "in_response_to":       cp.counter_proposal_id,
                "counter_proposal_id":  authority_cp_id,
                "decision":             "COUNTER",
                "negotiation_round":    cp.negotiation_round + 1,
                "justification":        body.justification,
                "valid_until":          authority_valid_until.isoformat(),
                "response":             body.justification,  # for partner's chat thread render
                "from":                 "npci",
            },
            change_id=change_id,
            label=partner.name,
            context="counter proposal",
        )

    logger.info(
        "PM counter-back: change=%s partner=%s closed_cp=%s new_cp=%s round=%d by=%s",
        change_id, partner_id, cp.counter_proposal_id, authority_cp_id,
        cp.negotiation_round + 1, user.username,
    )
    return {
        "resolved":            True,
        "status":              "countered_back",
        "new_counter_proposal_id": authority_cp_id,
        "negotiation_round":   cp.negotiation_round + 1,
    }


# ── Blockers (Tier 2) ────────────────────────────────────────────────────────

class BlockerResolveRequest(BaseModel):
    action_taken: str             # which option was picked, free text
    resolution_text: str          # PM's narrative back to partner
    artifact_ref: str | None = None   # optional URL/ref to a patched artifact


class BlockerStatusUpdateRequest(BaseModel):
    # Protocol v1 blocker_status_update (§6.13) — interim, non-terminal.
    status: str                       # received|triaged|assigned|in_investigation|fix_in_progress|fix_validating|awaiting_bank_input
    assigned_team: str | None = None
    notes: str | None = None
    estimated_resolution_by: str | None = None
    crm: dict | None = None           # {system, ticket_id, url, priority}


@router.get("/changes/{change_id}/partners/{partner_id}/blockers")
def list_blockers(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """List all blockers (open + resolved) for a partner."""
    rows = db.scalars(
        select(Blocker)
        .where(
            Blocker.change_request_id == change_id,
            Blocker.partner_id == partner_id,
        )
        .order_by(Blocker.created_at.desc())
    ).all()
    return [
        {
            "id": b.id,
            "blocker_id": b.blocker_id,
            "severity": b.severity.value,
            "status": b.status.value,
            "description": b.description,
            "impact": b.impact,
            "investigation_done": b.investigation_done or [],
            "options_considered": b.options_considered or [],
            "requested_action_from_npci": b.requested_action_from_npci,
            "created_at": b.created_at.isoformat(),
            "resolved_at": b.resolved_at.isoformat() if b.resolved_at else None,
            "resolved_by": b.resolved_by,
            "resolution_action": b.resolution_action,
            "resolution_text": b.resolution_text,
            "resolution_artifact_ref": b.resolution_artifact_ref,
        }
        for b in rows
    ]


@router.post("/changes/{change_id}/partners/{partner_id}/blockers/{blocker_id}/resolve")
async def resolve_blocker(
    change_id: str, partner_id: str, blocker_id: str,
    body: BlockerResolveRequest,
    db: DbDep, user: CurrentUser,
):
    """PM resolves a partner-reported blocker. Sends BLOCKER_RESOLUTION
    back to the partner with the chosen action + optional artifact ref;
    clears the assignment's blocked flag if no other open blockers."""
    b = db.scalars(
        select(Blocker).where(
            Blocker.id == blocker_id,
            Blocker.change_request_id == change_id,
            Blocker.partner_id == partner_id,
        )
    ).first()
    if not b:
        raise HTTPException(status_code=404, detail="Blocker not found")
    if b.status != BlockerStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Blocker already {b.status.value}")

    b.status = BlockerStatus.RESOLVED
    b.resolved_at = utcnow()
    b.resolved_by = user.id
    b.resolution_action = body.action_taken
    b.resolution_text = body.resolution_text
    b.resolution_artifact_ref = body.artifact_ref

    # Clear the assignment's blocked flag if no remaining open blockers.
    remaining_open = db.scalar(
        select(func.count(Blocker.id)).where(
            Blocker.assignment_id == b.assignment_id,
            Blocker.status == BlockerStatus.OPEN,
            Blocker.id != b.id,
        )
    ) or 0
    if remaining_open == 0:
        assignment = db.get(ChangePartnerAssignment, b.assignment_id)
        if assignment:
            assignment.blocked_at = None
            assignment.blocked_reason = None

    db.commit()

    # Record the Authority's side in the BLOCKER thread. Without this the thread carried only the
    # partner's report: the resolution went out over A2A but was never written to the
    # timeline, so the blocker conversation read as one-sided.
    try:
        from app.services.negotiation_service import record_linked_message
        record_linked_message(
            db,
            change_request_id=change_id,
            partner_id=partner_id,
            role="po_approved",
            content=(body.resolution_text or body.action_taken or "Blocker resolved"),
            event_kind="blocker_resolution",
            blocker_id=b.id,
            approved_by=getattr(user, "id", None),
            thread_kind="blocker",
        )
        db.commit()
    except Exception:  # noqa: BLE001 — a timeline write must never undo the resolution
        logger.exception("Dual-write NM failed for blocker resolution; Blocker still resolved")

    # Send resolution back to partner
    partner = db.get(PartnerAgent, partner_id)
    if partner:
        await notify_partner(
            partner.id,
            A2ATaskType.BLOCKER_RESOLUTION.value,
            {
                "message_kind":           "BLOCKER_RESOLUTION",
                # v1.1 spec-shaped (A2A v1.0 §blocker_resolution): `resolution` is
                # the terminal-disposition STRING enum (this endpoint only resolves);
                # details are top-level. `crm`/`assigned_team` null pending capture.
                "blocker_id":             b.blocker_id,
                "resolved_at":            b.resolved_at.isoformat(),
                "resolution":             "resolved",
                "resolution_text":        body.resolution_text,
                "action_taken":           body.action_taken,
                "patched_artefacts":      [body.artifact_ref] if body.artifact_ref else [],
                "crm":                    None,
                "assigned_team":          None,
                "references": [
                    {"type": "blocker", "id": b.blocker_id, "relation": "resolves"}
                ],
                # back-compat: the partner handler still tolerates in_response_to_blocker.
                "in_response_to_blocker": b.blocker_id,
            },
            change_id=change_id,
            label=partner.name,
            context="blocker resolution",
        )

    logger.info("Blocker resolved: change=%s partner=%s blocker=%s by=%s", change_id, partner_id, b.blocker_id, user.username)
    return {"resolved": True, "remaining_open_blockers": remaining_open}


@router.post("/changes/{change_id}/partners/{partner_id}/blockers/{blocker_id}/status")
async def update_blocker_status(
    change_id: str, partner_id: str, blocker_id: str,
    body: BlockerStatusUpdateRequest,
    db: DbDep, user: CurrentUser,
):
    """PM pushes an interim, non-terminal status on a partner blocker
    (protocol v1 `blocker_status_update`, §6.13). Unlike `/resolve` this does
    NOT close the blocker — it's an investigation progress signal (triaged,
    in_investigation, fix_in_progress, …) with optional CRM ref."""
    from app.a2a_common import protocol as _proto

    b = db.scalars(
        select(Blocker).where(
            Blocker.id == blocker_id,
            Blocker.change_request_id == change_id,
            Blocker.partner_id == partner_id,
        )
    ).first()
    if not b:
        raise HTTPException(status_code=404, detail="Blocker not found")
    if b.status != BlockerStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Blocker already {b.status.value}")

    # Interim progress belongs in the blocker thread too — otherwise the partner sees
    # investigation updates over A2A that never appear in the change's own timeline.
    try:
        from app.services.negotiation_service import record_linked_message
        _status = getattr(body, "status", None) or "update"
        record_linked_message(
            db,
            change_request_id=change_id,
            partner_id=partner_id,
            role="po_approved",
            content=f"[{_status}] " + (body.notes or "Investigation update"),
            event_kind="blocker_status_update",
            blocker_id=b.id,
            approved_by=getattr(user, "id", None),
            thread_kind="blocker",
        )
        db.commit()
    except Exception:  # noqa: BLE001 — timeline write must never block the status push
        logger.exception("Dual-write NM failed for blocker status update")

    partner = db.get(PartnerAgent, partner_id)
    if partner:
        await notify_partner(
            partner.id,
            _proto.A2ATaskType.BLOCKER_STATUS_UPDATE.value,
            {
                # v1.1 spec-shaped (A2A v1.0 §blocker_status_update) — the code was
                # already near-complete; adds `updated_at` + self-referential `references`.
                "blocker_id":              b.blocker_id,
                "updated_at":              utcnow().isoformat(),
                "status":                  body.status,
                "assigned_team":           body.assigned_team,
                "estimated_resolution_by": body.estimated_resolution_by,
                "crm":                     body.crm,
                "notes":                   body.notes,
                "references": [
                    {"type": "blocker", "id": b.blocker_id, "relation": "updates"}
                ],
                # back-compat: partner handler tolerates in_response_to_blocker.
                "in_response_to_blocker":  b.blocker_id,
            },
            change_id=change_id,
            label=partner.name,
            context="blocker status update",
        )

    logger.info(
        "Blocker status update sent: change=%s partner=%s blocker=%s status=%s by=%s",
        change_id, partner_id, b.blocker_id, body.status, user.username,
    )
    return {"sent": True, "blocker_id": b.blocker_id, "status": body.status}


# ── Cert waivers (protocol v1 §7.8–7.9) ───────────────────────────────────────

class CertWaiverDecisionRequest(BaseModel):
    decision: str                       # granted | rejected
    conditions: str | None = None
    valid_until: str | None = None


@router.get("/changes/{change_id}/partners/{partner_id}/cert-waivers")
def list_cert_waivers(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """List cert waivers (requested + decided) for a partner."""
    from app.models.phase_c import CertWaiver
    rows = db.scalars(
        select(CertWaiver)
        .where(CertWaiver.change_request_id == change_id, CertWaiver.partner_id == partner_id)
        .order_by(CertWaiver.requested_at.desc())
    ).all()
    return [
        {
            "id": w.id, "case_id": w.case_id, "category": w.category,
            "reason": w.reason, "status": w.status, "conditions": w.conditions,
            "valid_until": w.valid_until, "decided_by": w.decided_by,
            "requested_at": w.requested_at.isoformat(),
            "decided_at": w.decided_at.isoformat() if w.decided_at else None,
        }
        for w in rows
    ]


@router.post("/changes/{change_id}/partners/{partner_id}/cert-waivers/{waiver_id}/decide")
async def decide_cert_waiver(
    change_id: str, partner_id: str, waiver_id: str,
    body: CertWaiverDecisionRequest,
    db: DbDep, user: CurrentUser,
):
    """Risk+Product decision on a cert waiver (§7.9). Updates the CertWaiver and
    sends cert_waiver_decision to the partner."""
    from app.a2a_common import protocol as _proto
    from app.models.phase_c import CertWaiver

    w = db.scalars(
        select(CertWaiver).where(
            CertWaiver.id == waiver_id,
            CertWaiver.change_request_id == change_id,
            CertWaiver.partner_id == partner_id,
        )
    ).first()
    if not w:
        raise HTTPException(status_code=404, detail="Waiver not found")
    if w.status != "requested":
        raise HTTPException(status_code=400, detail=f"Waiver already {w.status}")
    if body.decision not in ("granted", "rejected"):
        raise HTTPException(status_code=400, detail="decision must be granted|rejected")

    w.status = body.decision
    w.conditions = body.conditions
    w.valid_until = body.valid_until
    w.decided_by = user.username
    w.decided_at = utcnow()
    db.commit()

    partner = db.get(PartnerAgent, partner_id)
    if partner:
        # NOT migrated to PartnerChannel, deliberately. This is a
        # certification-protocol message, not change communication.
        # Routing it through the channel would drag certification
        # vocabulary into the generic contract; folding it into
        # CertificationHarness would widen that interface to suit one
        # domain. It stays on the transport it belongs to.
        await send_task_to_partner(
            partner=partner,
            task_type=_proto.A2ATaskType.CERT_WAIVER_DECISION,
            payload={
                "case_id": w.case_id, "decision": body.decision,
                "conditions": body.conditions, "valid_until": body.valid_until,
            },
            db=db,
            change_request_id=change_id,
            cflow_id=w.cflow_id,
        )
    logger.info("Cert waiver %s: case=%s by=%s", body.decision, w.case_id, user.username)
    return {"decided": body.decision, "case_id": w.case_id}


# ── Status Tracking & Readiness ──────────────────────────────────────────────

@router.get("/changes/{change_id}/partners/{partner_id}/progress")
def get_partner_progress(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """Get a partner's implementation progress steps for a change request."""
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()

    if not assignment:
        return {"steps": [], "status": None}

    progress = db.scalars(
        select(PartnerProgress)
        .where(PartnerProgress.assignment_id == assignment.id)
        .order_by(PartnerProgress.reported_at)
    ).all()

    return {
        "status": assignment.status.value if hasattr(assignment.status, 'value') else assignment.status,
        "steps": [
            {
                "step": p.step.value if hasattr(p.step, 'value') else p.step,
                "notes": p.notes,
                "reported_at": p.reported_at.isoformat(),
            }
            for p in progress
        ],
    }


@router.get("/changes/{change_id}/phase-c/progress-grid")
def get_progress_grid(change_id: str, db: DbDep, _: CurrentUser):
    """Get the consolidated progress grid — all partners and their steps."""
    assignments = db.scalars(
        select(ChangePartnerAssignment)
        .where(ChangePartnerAssignment.change_request_id == change_id)
    ).all()

    grid = []
    for a in assignments:
        partner = db.get(PartnerAgent, a.partner_id)
        if not partner:
            continue

        progress = db.scalars(
            select(PartnerProgress)
            .where(PartnerProgress.assignment_id == a.id)
        ).all()
        completed_steps = {p.step.value if hasattr(p.step, 'value') else p.step for p in progress}

        # "Accepted" means the partner has formally agreed to terms —
        # at or beyond AssignmentStatus.ACCEPTED. This is the contract
        # checkpoint that gates implementation; surfaced as a top-level
        # stage in the unified 7-stage stepper (Communicated · Accepted ·
        # Design · Coding · Testing · Ready for Certification · Certified).
        accepted_or_beyond = a.status in (
            AssignmentStatus.ACCEPTED, "accepted",
            AssignmentStatus.APPLIED, "applied",
            AssignmentStatus.TESTED, "tested",
            AssignmentStatus.READY_FOR_CERTIFICATION, "ready_for_certification",
            AssignmentStatus.CERTIFYING, "certifying",
            AssignmentStatus.CERTIFIED, "certified",
            AssignmentStatus.READY_FOR_PRODUCTION, "ready_for_production",
            AssignmentStatus.IN_PRODUCTION, "in_production",
            # Legacy values that conceptually map to post-accepted
            AssignmentStatus.IN_PROGRESS, "in_progress",
            AssignmentStatus.READY, "ready",
        )
        grid.append({
            "partner_id": partner.id,
            "name": partner.name,
            "partner_type": partner.partner_type.value if hasattr(partner.partner_type, 'value') else partner.partner_type,
            "status": a.status.value if hasattr(a.status, 'value') else a.status,
            "accepted": accepted_or_beyond,
            "design_completed": "design_completed" in completed_steps,
            "coding_completed": "coding_completed" in completed_steps,
            "testing_completed": "testing_completed" in completed_steps,
            "ready_for_cert": a.status in (
                AssignmentStatus.READY_FOR_CERTIFICATION, "ready_for_certification",
                AssignmentStatus.READY, "ready",  # legacy
            ),
            "certified": a.status in (
                AssignmentStatus.CERTIFIED, "certified",
                AssignmentStatus.READY_FOR_PRODUCTION, "ready_for_production",
                AssignmentStatus.IN_PRODUCTION, "in_production",
            ),
        })

    return {"change_id": change_id, "partners": grid}


# ── Certification Testing ────────────────────────────────────────────────────

# ── Demo controls ─────────────────────────────────────────────────────────
# Two operator helpers behind the cert conversation view: (1) fire the
# in-process precert engine directly, without waiting for the partner to send a
# readiness A2A message; (2) clear a single (change, partner)'s conversation +
# run history so a fresh run can be shown from scratch. Both are scoped to one
# partner — they never touch other partners, the change, or bank provisioning.

@router.post("/changes/{change_id}/partners/{partner_id}/cert/demo-run")
async def demo_run_certification(
    change_id: str, partner_id: str,
    db: DbDep, _: CurrentUser, background: BackgroundTasks,
):
    """Trigger the in-process precert engine for this (change, partner). Drives the
    full signed-A2A lifecycle conversation in the background; messages land in
    a2a_messages and surface live in the cert conversation view."""
    if not db.get(ChangeRequest, change_id):
        raise HTTPException(status_code=404, detail="Change request not found")
    partner = db.get(PartnerAgent, partner_id)
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    from app.core.config import settings
    from app.services.cert_orchestrator import orchestrate_cert_run
    role = getattr(settings, "precert_engine_certgroup", None) or "REMITTER"
    background.add_task(orchestrate_cert_run, change_id, partner_id, role, {}, {})
    logger.info("Demo cert run dispatched: change=%s partner=%s role=%s", change_id, partner_id, role)
    return {"status": "running", "change_id": change_id, "partner_id": partner_id, "role": role}


@router.post("/changes/{change_id}/partners/{partner_id}/cert/demo-reset")
def demo_reset_certification(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """Clear this (change, partner)'s certification conversation + run history so
    a fresh demo run can start clean. Scoped to the single partner; leaves other
    partners, the change, and bank provisioning untouched."""
    from sqlalchemy import text
    params = {"cid": change_id, "pid": partner_id}
    stmts = [
        ("cert_triage",
         "DELETE FROM cert_triage WHERE cert_test_result_id IN "
         "(SELECT ctr.id FROM cert_test_results ctr "
         "  JOIN cert_runs cr2 ON ctr.cert_run_id = cr2.id "
         " WHERE cr2.change_request_id = :cid AND cr2.partner_id = :pid)"),
        ("cert_test_results",
         "DELETE FROM cert_test_results WHERE cert_run_id IN "
         "(SELECT id FROM cert_runs WHERE change_request_id = :cid AND partner_id = :pid)"),
        ("cert_runs",
         "DELETE FROM cert_runs WHERE change_request_id = :cid AND partner_id = :pid"),
        ("a2a_messages",
         "DELETE FROM a2a_messages WHERE change_request_id = :cid AND partner_id = :pid"),
    ]
    deleted = {}
    for name, sql in stmts:
        deleted[name] = db.execute(text(sql), params).rowcount
    assignment = db.scalars(select(ChangePartnerAssignment).where(
        ChangePartnerAssignment.change_request_id == change_id,
        ChangePartnerAssignment.partner_id == partner_id,
    )).first()
    if assignment:
        assignment.status = AssignmentStatus.READY_FOR_CERTIFICATION
        assignment.blocked_at = None
        assignment.blocked_reason = None
    db.commit()
    logger.info("Demo cert reset: change=%s partner=%s deleted=%s", change_id, partner_id, deleted)
    return {"status": "reset", "deleted": deleted}


@router.get("/changes/{change_id}/partners/{partner_id}/cert/txns")
def cert_txns(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """Round 2 (online certification) — the real network switch<->switch transactions
    for this cert run, read from precertdb.upihosttxnlog: per case, the request the
    certification-agent switch (precert) sent and the response the bank switch
    (bank-sim) returned. Round 1 is the A2A agent<->agent conversation; this is the
    on-the-wire the network exchange underneath it."""
    from sqlalchemy import select as _sel, and_ as _and
    # Finding #8: this is the ONE tiered-column read in the codebase that goes
    # through a column-level select() rather than the ORM, so the coldstore
    # read-through 'load' hook (services/artifact_coldstore_read.py) cannot cover
    # it — no ORM instance is ever built. The message id is selected alongside
    # the payload so a nulled (cold-stored) payload can be rehydrated explicitly.
    rows = db.execute(_sel(A2AMessage.id, A2AMessage.payload).where(_and(
        A2AMessage.change_request_id == change_id,
        A2AMessage.partner_id == partner_id,
        A2AMessage.task_type == "cert_case_result",
    ))).all()
    from app.services.artifact_coldstore_read import rehydrate_payload_dict
    tcs: list[str] = []
    for (msg_id, pl) in rows:
        pl = rehydrate_payload_dict(db, msg_id, pl)   # no-op when pl is not None
        t = ((pl or {}).get("payload") or {}).get("test_case_id")
        if t and t not in tcs:
            tcs.append(t)
    if not tcs:
        return {"txns": [], "count": 0}
    import psycopg2
    from app.core.config import settings
    con = psycopg2.connect(host=settings.precert_engine_db_host, port=settings.precert_engine_db_port,
                           user=settings.precert_engine_db_user, password=settings.precert_engine_db_password,
                           dbname=settings.precert_engine_db_name)
    try:
        with con.cursor() as cur:
            ph = ",".join(["%s"] * len(tcs))
            cur.execute(
                f"SELECT testid, apiname, rc, expectedrc, review, req_time, res_time, request, response "
                f"FROM upihosttxnlog WHERE testid IN ({ph}) ORDER BY id DESC", tcs)
            seen, out = set(), []
            for testid, api, rc, exp, review, reqt, rest, req, resp in cur.fetchall():
                if testid in seen:
                    continue
                seen.add(testid)
                out.append({
                    "test_case_id": testid, "api": api, "rc": rc, "expected_rc": exp,
                    "review": review,
                    "req_time": str(reqt) if reqt else None,
                    "res_time": str(rest) if rest else None,
                    "request_xml": req, "response_xml": resp,
                })
    finally:
        con.close()
    order = {t: i for i, t in enumerate(tcs)}
    out.sort(key=lambda x: order.get(x["test_case_id"], 999))
    return {"txns": out, "count": len(out)}



@router.post("/changes/{change_id}/partners/{partner_id}/cert/dispatch")
async def dispatch_certification(change_id: str, partner_id: str, db: DbDep,
                                 user: CurrentUser,
                                 body: dict | None = None):
    """Dispatch ONE certification round through the harness-agnostic seam.

    This is the OPERATOR's button onto `certification_dispatch.run_certification`
    — the same call the partner's readiness declaration triggers, so the real
    harness the active domain pack declares runs (sim_pack builds and publishes
    the round pack, announces the partner's class over A2A, executes the
    authority's own class, and leaves the join to finalize). Nothing here is a
    demo path: the run rows, pack refs and A2A messages are the production ones.

    Body (all optional):
        role     — the role the partner certifies for; scopes the case set to
                   that actor's sheet of the change's cert workbook
                   (e.g. LENDING_LIBRARY). Empty = unscoped.
        advance  — walk the assignment to CERTIFYING first (for a change whose
                   partner has not driven received→…→ready itself). Uses the
                   real status setter, so history rows are written.

    Returns the round summary. `status=awaiting_partner` means the round is
    DISPATCHED, not passed: the partner's cases are outstanding and the join
    flips the verdict when their reports arrive (or the suite deadline does).

    Distinct from `cert/start` below, which drives the legacy cert-agent
    delegation to a registered cert_engine partner and bypasses the domain
    pack's harness selection entirely.
    """
    body = body or {}
    cr = db.get(ChangeRequest, change_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Partner not assigned to this change")

    current = assignment.status.value if hasattr(assignment.status, "value") else assignment.status
    if current == "withdrawn":
        raise HTTPException(status_code=409, detail="Partner assignment is withdrawn")

    if body.get("advance"):
        for target in (AssignmentStatus.ACCEPTED, AssignmentStatus.APPLIED,
                       AssignmentStatus.TESTED,
                       AssignmentStatus.READY_FOR_CERTIFICATION,
                       AssignmentStatus.CERTIFYING):
            if assignment.status != target:
                set_status(assignment, target, db, actor_partner_id=partner_id,
                           reason=f"Operator dispatch by {getattr(user, 'username', 'operator')}")
        db.commit()
    elif current not in ("ready_for_certification", "certifying", "certified",
                         "ready", "ready_for_production", "in_production"):
        raise HTTPException(
            status_code=409,
            detail=f"Partner is '{current}' — not ready for certification. "
                   "Pass advance=true to walk the assignment forward, or let "
                   "the partner declare readiness.")

    from app.services.certification_dispatch import run_certification

    result = await run_certification(
        change_id, partner_id, role=str(body.get("role") or ""),
        test_data={}, dispatch_meta={"dispatched_by": "operator"})
    if result is None:
        raise HTTPException(
            status_code=409,
            detail="The active domain pack declares no certification harness — "
                   "nothing to dispatch (add `certification_harness:` to the "
                   "pack, or check DOMAIN_PACK).")
    details = result.details or {}
    if details.get("skipped"):
        raise HTTPException(status_code=409,
                            detail=f"{details.get('error')}: {details.get('detail')}")
    return details


@router.post("/changes/{change_id}/partners/{partner_id}/cert/start")
async def start_certification(change_id: str, partner_id: str, db: DbDep, user: CurrentUser):
    """Start a certification test run for a partner.

    Delegates execution to the registered cert_engine partner (cert-agent) over
    A2A. Returns immediately with status=running; per-TC results land
    asynchronously when cert-agent POSTs CERT_TEST_RESPONSE back to the Authority's
    /api/a2a/tasks/send. The assignment.status flips to CERTIFYING immediately
    on dispatch.
    """
    cr = db.get(ChangeRequest, change_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")

    assignment = db.scalars(
        select(ChangePartnerAssignment).where(
            ChangePartnerAssignment.change_request_id == change_id,
            ChangePartnerAssignment.partner_id == partner_id,
        )
    ).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Partner not assigned to this change")

    # Gate: partner must be ready_for_certification (or already certifying for
    # re-runs after a failure). Reject withdrawn / blocked / earlier states.
    current_status = assignment.status.value if hasattr(assignment.status, 'value') else assignment.status
    allowed = {"ready_for_certification", "certifying", "certified", "ready", "in_progress"}  # legacy values too
    if current_status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Partner status is '{current_status}'; cert can only start from ready_for_certification or certifying",
        )
    if assignment.blocked_at is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Partner is blocked: {assignment.blocked_reason or 'no reason given'}",
        )

    cert_doc = latest_kit_doc(db, change_id, ProductKitDocType.CERT_TEST_CASES)
    if not cert_doc or not cert_doc.content:
        raise HTTPException(
            status_code=400,
            detail="No certification test cases found. Generate them in Phase A Product Kit first.",
        )

    # Find the cert_engine partner (the cert-agent submodule).
    cert_engine = None
    all_partners = db.scalars(
        select(PartnerAgent).where(PartnerAgent.status == PartnerStatus.ACTIVE)
    ).all()
    for p in all_partners:
        types = p.partner_type or []
        if isinstance(types, str):
            types = [types]
        if "cert_engine" in types:
            cert_engine = p
            break
    if not cert_engine or not cert_engine.endpoint_url:
        raise HTTPException(
            status_code=503,
            detail=(
                "No active cert_engine partner is registered with an endpoint URL. "
                "Register the certification engine in Admin → Partners first."
            ),
        )

    # Allocate run number + persist a RUNNING CertRun row.
    existing_runs = db.scalars(
        select(CertRun).where(
            CertRun.change_request_id == change_id,
            CertRun.partner_id == partner_id,
        )
    ).all()
    run_number = len(existing_runs) + 1

    cert_run = CertRun(
        id=generate_uuid(),
        change_request_id=change_id,
        partner_id=partner_id,
        run_number=run_number,
        status=CertRunStatus.RUNNING,
    )
    db.add(cert_run)

    # Flip assignment.status to CERTIFYING immediately on dispatch.
    set_status(
        assignment, AssignmentStatus.CERTIFYING, db,
        actor_user_id=user.id,
        reason=f"Cert run #{run_number} dispatched",
    )
    db.commit()
    db.refresh(cert_run)

    logger.info(
        "Certification dispatch: change=%s partner=%s run=%d cert_run_id=%s engine=%s",
        change_id, partner_id, run_number, cert_run.id, cert_engine.name,
    )

    # Cap inline payload size to protect cert-agent's request handler.
    test_cases_content = cert_doc.content
    if len(test_cases_content) > 1_000_000:
        test_cases_content = test_cases_content[:1_000_000]
        logger.warning(
            "Cert test-cases doc truncated to 1MB: change=%s run_id=%s",
            change_id, cert_run.id,
        )

    target_partner = db.get(PartnerAgent, partner_id)
    payload = {
        "cert_run_id":       cert_run.id,
        "change_id":         change_id,
        "change_title":      cr.title or "",
        "partner_id":        partner_id,
        "partner_name":      target_partner.name if target_partner else None,
        "partner_endpoints": {"endpoint_url": target_partner.endpoint_url} if target_partner else {},
        "test_cases":        test_cases_content,
    }

    # If the operator has pushed Phase A's TCs into cert-agent's tc_store
    # (cert_simulator_sync_log has an apply row), instruct cert-agent to run
    # only those TCs via the subset filter. Otherwise cert-agent runs its
    # full library (backward compatible).
    from app.models.cert_sync import CertSimulatorSyncLog
    synced = db.scalars(
        select(CertSimulatorSyncLog)
        .where(
            CertSimulatorSyncLog.change_request_id == change_id,
            CertSimulatorSyncLog.operation == "apply",
        )
        .order_by(CertSimulatorSyncLog.created_at.desc())
        .limit(1)
    ).first()
    if synced:
        payload["subset"] = f"cr-{change_id[:8]}"
        logger.info(
            "Cert run will use synced subset: change=%s subset=%s",
            change_id, payload["subset"],
        )
    else:
        logger.warning(
            "start_certification called without a synced test suite for change=%s; "
            "cert-agent will run its full library", change_id,
        )

    # NOT migrated to PartnerChannel, deliberately. This is a
    # certification-protocol message, not change communication.
    # Routing it through the channel would drag certification
    # vocabulary into the generic contract; folding it into
    # CertificationHarness would widen that interface to suit one
    # domain. It stays on the transport it belongs to.
    msg = await send_task_to_partner(
        partner=cert_engine,
        task_type=A2ATaskType.CERT_TEST_REQUEST,
        payload=payload,
        db=db,
        change_request_id=change_id,
    )

    if msg.status not in ("delivered", "sent"):
        # Delivery failed — fail the cert_run early so the UI doesn't poll forever.
        cert_run.status = CertRunStatus.COMPLETED
        cert_run.total = 0
        cert_run.passed = 0
        cert_run.failed = 0
        cert_run.skipped = 0
        cert_run.completed_at = utcnow()
        db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"Failed to deliver cert request to engine: status={msg.status}",
        )

    return {
        "run_id":     cert_run.id,
        "run_number": run_number,
        "total":      None,
        "passed":     None,
        "failed":     None,
        "skipped":    None,
        "status":     "running",
        "certified":  False,
        "task_id":    msg.id,
        "engine":     cert_engine.name,
    }


@router.get("/changes/{change_id}/partners/{partner_id}/cert/runs")
def list_cert_runs(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """List all certification runs for a partner."""
    runs = db.scalars(
        select(CertRun).where(
            CertRun.change_request_id == change_id,
            CertRun.partner_id == partner_id,
        ).order_by(CertRun.run_number.desc())
    ).all()

    return [
        {
            "id": r.id,
            "run_number": r.run_number,
            "total": r.total,
            "passed": r.passed,
            "failed": r.failed,
            "skipped": r.skipped,
            "status": r.status.value if hasattr(r.status, 'value') else r.status,
            "started_at": r.started_at.isoformat(),
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in runs
    ]


@router.get("/changes/{change_id}/partners/{partner_id}/cert/runs/{run_id}")
def get_cert_run_detail(change_id: str, partner_id: str, run_id: str, db: DbDep, _: CurrentUser):
    """Get detailed results for a certification run."""
    cert_run = db.get(CertRun, run_id)
    if not cert_run:
        raise HTTPException(status_code=404, detail="Certification run not found")

    results = db.scalars(
        select(CertTestResult).where(CertTestResult.cert_run_id == run_id).order_by(CertTestResult.created_at)
    ).all()

    return {
        "id": cert_run.id,
        "run_number": cert_run.run_number,
        "total": cert_run.total,
        "passed": cert_run.passed,
        "failed": cert_run.failed,
        "skipped": cert_run.skipped,
        "status": cert_run.status.value if hasattr(cert_run.status, 'value') else cert_run.status,
        "started_at": cert_run.started_at.isoformat(),
        "completed_at": cert_run.completed_at.isoformat() if cert_run.completed_at else None,
        "results": [
            {
                "id": r.id,
                "test_case_id": r.test_case_id,
                "direction": r.direction.value if hasattr(r.direction, 'value') else r.direction,
                "status": r.status.value if hasattr(r.status, 'value') else r.status,
                "expected_response": r.expected_response,
                "actual_response": r.actual_response,
                "latency_ms": r.latency_ms,
                "triage": {
                    "id": r.triage.id,
                    "ai_verdict": r.triage.ai_verdict.value if hasattr(r.triage.ai_verdict, 'value') else r.triage.ai_verdict,
                    "ai_reasoning": r.triage.ai_reasoning,
                    "user_override": r.triage.user_override,
                    "final_verdict": r.triage.final_verdict,
                } if r.triage else None,
            }
            for r in results
        ],
    }


# ── Cert Triage ──────────────────────────────────────────────────────────────

class TriageApproveRequest(BaseModel):
    verdict: str  # partner_code_bug, test_case_issue, env_issue


@router.post("/changes/{change_id}/partners/{partner_id}/cert/triage")
async def trigger_triage(change_id: str, partner_id: str, db: DbDep, _: CurrentUser):
    """Run AI triage on all failed tests in the latest certification run."""
    # Get latest cert run
    cert_run = db.scalars(
        select(CertRun).where(
            CertRun.change_request_id == change_id,
            CertRun.partner_id == partner_id,
        ).order_by(CertRun.run_number.desc())
    ).first()

    if not cert_run:
        raise HTTPException(status_code=404, detail="No certification run found")

    # Get failed results
    failed = db.scalars(
        select(CertTestResult).where(
            CertTestResult.cert_run_id == cert_run.id,
            CertTestResult.status == CertTestStatus.FAIL,
        )
    ).all()

    if not failed:
        return {"message": "No failed tests to triage", "triaged": 0}

    # Build input for AI
    failed_data = [
        {
            "id": r.id,
            "test_case_id": r.test_case_id,
            "direction": r.direction.value if hasattr(r.direction, 'value') else r.direction,
            "expected_response": r.expected_response,
            "actual_response": r.actual_response,
        }
        for r in failed
    ]

    logger.info("Cert triage started: change=%s partner=%s failed=%d", change_id, partner_id, len(failed))

    verdicts = await triage_failed_tests(failed_data)

    # Store triage results
    triaged = 0
    for v in verdicts:
        result_id = v.get("test_result_id")
        verdict_str = v.get("verdict", "env_issue")
        reasoning = v.get("reasoning", "")

        # Find matching test result
        test_result = None
        for r in failed:
            if r.id == result_id or r.test_case_id == result_id:
                test_result = r
                break

        if not test_result:
            continue

        # Check if triage already exists
        existing = db.scalars(
            select(CertTriage).where(CertTriage.cert_test_result_id == test_result.id)
        ).first()

        if existing:
            existing.ai_verdict = TriageVerdict(verdict_str)
            existing.ai_reasoning = reasoning
        else:
            try:
                verdict_enum = TriageVerdict(verdict_str)
            except ValueError:
                verdict_enum = TriageVerdict.ENV_ISSUE

            db.add(CertTriage(
                id=generate_uuid(),
                cert_test_result_id=test_result.id,
                ai_verdict=verdict_enum,
                ai_reasoning=reasoning,
            ))
        triaged += 1

    db.commit()
    logger.info("Cert triage completed: change=%s partner=%s triaged=%d", change_id, partner_id, triaged)

    return {"triaged": triaged, "run_id": cert_run.id}


@router.post("/changes/{change_id}/partners/{partner_id}/cert/triage/{triage_id}/approve")
def approve_triage(change_id: str, partner_id: str, triage_id: str, body: TriageApproveRequest, db: DbDep, user: CurrentUser):
    """PO approves or overrides a triage verdict."""
    triage = db.get(CertTriage, triage_id)
    if not triage:
        raise HTTPException(status_code=404, detail="Triage not found")

    if body.verdict != triage.ai_verdict.value:
        triage.user_override = body.verdict
    triage.final_verdict = body.verdict
    db.commit()

    logger.info("Triage approved: id=%s verdict=%s by=%s", triage_id, body.verdict, user.username)
    return {"approved": True, "final_verdict": body.verdict}


# ── Aggregation endpoints for the standalone Certification UI ────────────────

_BUCKET_FOR_STATUS = {
    "in_production":           "live",
    "ready_for_production":    "awaiting_go_live",
    "certified":               "cert_done",
    "certifying":              "cert_in_flight",
    "ready_for_certification": "cert_pending",
    "ready":                   "cert_pending",   # legacy
    "tested":                  "building",
    "applied":                 "building",
    "in_progress":             "building",       # legacy
    "accepted":                "kickoff",
    "acknowledged":            "kickoff",        # legacy
    "received":                "kickoff",
    "communicated":            "kickoff",        # legacy
    "assigned":                "kickoff",
    "withdrawn":               "withdrawn",
}

# Priority order for picking the row-level pill. Higher index → higher priority.
_ROW_STATUS_PRIORITY = [
    "not_started",
    "kickoff",
    "building",
    "cert_pending",
    "cert_in_flight",
    "failed",
    "cert_done",
    "awaiting_go_live",
    "live",
    "withdrawn",
    "blocked",
]


@router.get("/certification/dashboard")
def certification_dashboard(db: DbDep, _: CurrentUser):
    """Aggregate cert-run state across every change request × partner.

    Returns one row per change with rolled-up partner counts in the new
    lifecycle buckets. Used by frontend/src/pages/Certification/CertDashboard.jsx.
    """
    changes = db.scalars(select(ChangeRequest).order_by(ChangeRequest.created_at.desc())).all()
    rows = []
    for cr in changes:
        assignments = db.scalars(
            select(ChangePartnerAssignment).where(ChangePartnerAssignment.change_request_id == cr.id)
        ).all()
        if not assignments:
            continue

        # Bucket counts (new vocabulary)
        buckets = {b: 0 for b in {
            "live", "awaiting_go_live", "cert_done", "cert_in_flight", "cert_pending",
            "building", "kickoff", "withdrawn",
        }}
        blocked = 0
        failed = 0  # latest cert run had failures
        latest_run_at = None
        partner_pills = []

        for a in assignments:
            status_val = a.status.value if hasattr(a.status, "value") else a.status
            if a.blocked_at is not None:
                blocked += 1
            bucket = _BUCKET_FOR_STATUS.get(status_val, "kickoff")
            buckets[bucket] = buckets.get(bucket, 0) + 1

            latest_run = db.scalars(
                select(CertRun)
                .where(CertRun.change_request_id == cr.id, CertRun.partner_id == a.partner_id)
                .order_by(CertRun.run_number.desc())
            ).first()
            if latest_run:
                if latest_run.failed and latest_run.failed > 0:
                    failed += 1
                if latest_run.completed_at:
                    if latest_run_at is None or latest_run.completed_at > latest_run_at:
                        latest_run_at = latest_run.completed_at

            partner_pills.append({"status": status_val, "blocked": a.blocked_at is not None})

        total = len(assignments)

        # Pick the row-level status pill: highest-priority bucket present,
        # with overrides for blocked / withdrawn.
        if blocked > 0 and buckets.get("withdrawn", 0) < total:
            row_status = "blocked"
        elif buckets.get("withdrawn", 0) == total:
            row_status = "withdrawn"
        elif buckets.get("live", 0) == total:
            row_status = "live"
        elif buckets.get("live", 0) > 0:
            row_status = "live"  # at least some live
        elif buckets.get("awaiting_go_live", 0) > 0:
            row_status = "awaiting_go_live"
        elif buckets.get("cert_done", 0) > 0 and buckets.get("cert_done", 0) == total:
            row_status = "cert_done"
        elif failed > 0:
            row_status = "failed"
        elif buckets.get("cert_in_flight", 0) > 0:
            row_status = "cert_in_flight"
        elif buckets.get("cert_pending", 0) > 0:
            row_status = "cert_pending"
        elif buckets.get("building", 0) > 0:
            row_status = "building"
        elif buckets.get("kickoff", 0) == total:
            row_status = "kickoff"
        else:
            row_status = "kickoff"

        rows.append({
            "id":             cr.id,
            "title":          cr.title or "(untitled)",
            "description":    (cr.initial_prompt or "")[:200],
            "released_at":    cr.created_at.isoformat() if cr.created_at else None,
            "phase":          cr.status.value if hasattr(cr.status, "value") else str(cr.status),
            "partners":       total,
            # Legacy fields kept for backward-compat with existing UI bindings
            "certified":      buckets.get("cert_done", 0) + buckets.get("awaiting_go_live", 0) + buckets.get("live", 0),
            "pending":        buckets.get("kickoff", 0) + buckets.get("building", 0) + buckets.get("cert_pending", 0) + buckets.get("cert_in_flight", 0),
            "failed":         failed,
            # New richer breakdown
            "buckets":        buckets,
            "blocked":        blocked,
            "withdrawn":      buckets.get("withdrawn", 0),
            "live":           buckets.get("live", 0),
            "status":         row_status,
            "latest_run_at":  latest_run_at.isoformat() if latest_run_at else None,
        })

    return {
        "changes": rows,
        "totals": {
            "total_crs":        len(rows),
            "live_crs":         sum(1 for r in rows if r["status"] == "live"),
            "awaiting_go_live": sum(1 for r in rows if r["status"] == "awaiting_go_live"),
            "active_cert_crs":  sum(1 for r in rows if r["status"] in ("cert_in_flight", "cert_pending", "failed")),
            "blocked_crs":      sum(1 for r in rows if r["status"] == "blocked"),
            "withdrawn_crs":    sum(1 for r in rows if r["status"] == "withdrawn"),
            # Backward-compat keys preserved so the old dashboard frontend keeps working
            "completed_crs":    sum(1 for r in rows if r["status"] in ("cert_done", "live", "awaiting_go_live")),
            "in_progress_crs":  sum(1 for r in rows if r["status"] in ("cert_in_flight", "cert_pending", "building", "kickoff")),
            "total_partners":   sum(r["partners"] for r in rows),
            "certified_total":  sum(r["certified"] for r in rows),
            "live_partners":    sum(r["live"] for r in rows),
            "blocked_partners": sum(r["blocked"] for r in rows),
        },
    }


@router.get("/changes/{change_id}/cert-summary")
def change_cert_summary(change_id: str, db: DbDep, _: CurrentUser):
    """Per-partner cert summary for one change. Drives CertChangeDetail.jsx."""
    cr = db.get(ChangeRequest, change_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")

    assignments = db.scalars(
        select(ChangePartnerAssignment).where(ChangePartnerAssignment.change_request_id == change_id)
    ).all()

    partners_out = []
    for a in assignments:
        partner = db.get(PartnerAgent, a.partner_id)
        if not partner:
            continue

        latest_run = db.scalars(
            select(CertRun)
            .where(CertRun.change_request_id == change_id, CertRun.partner_id == a.partner_id)
            .order_by(CertRun.run_number.desc())
        ).first()
        run_count = db.scalar(
            select(func.count(CertRun.id))
            .where(CertRun.change_request_id == change_id, CertRun.partner_id == a.partner_id)
        ) or 0

        status_val = a.status.value if hasattr(a.status, "value") else a.status
        partner_types = partner.partner_type if isinstance(partner.partner_type, list) else [partner.partner_type or "bank"]

        # Open negotiation threads → derive `negotiating` flag
        from app.models.phase_c import NegotiationThread, ThreadStatus
        open_threads = db.scalar(
            select(func.count(NegotiationThread.id))
            .where(
                NegotiationThread.change_request_id == change_id,
                NegotiationThread.partner_id == a.partner_id,
                NegotiationThread.status == ThreadStatus.OPEN,
            )
        ) or 0

        # Latest history row → "current state since"
        from app.models.phase_c import AssignmentStatusHistory
        latest_history = db.scalars(
            select(AssignmentStatusHistory)
            .where(AssignmentStatusHistory.assignment_id == a.id)
            .order_by(AssignmentStatusHistory.created_at.desc())
        ).first()

        partners_out.append({
            "partner_id":        partner.id,
            "partner_name":      partner.name,
            "partner_types":     partner_types,
            "assignment_status": status_val,
            "blocked":           a.blocked_at is not None,
            "blocked_at":        a.blocked_at.isoformat() if a.blocked_at else None,
            "blocked_reason":    a.blocked_reason,
            "open_threads":      open_threads,
            "current_state_since": latest_history.created_at.isoformat() if latest_history else (a.assigned_at.isoformat() if a.assigned_at else None),
            "run_count":         run_count,
            "latest_run":        {
                "id":           latest_run.id,
                "run_number":   latest_run.run_number,
                "status":       latest_run.status.value if latest_run and hasattr(latest_run.status, "value") else None,
                "total":        latest_run.total,
                "passed":       latest_run.passed,
                "failed":       latest_run.failed,
                "skipped":      latest_run.skipped,
                "started_at":   latest_run.started_at.isoformat() if latest_run.started_at else None,
                "completed_at": latest_run.completed_at.isoformat() if latest_run.completed_at else None,
            } if latest_run else None,
        })

    def _count(stat: str) -> int:
        return sum(1 for p in partners_out if p["assignment_status"] == stat)

    return {
        "change_id":    change_id,
        "change_title": cr.title or "",
        "partners":     partners_out,
        "summary": {
            "total":            len(partners_out),
            "live":             _count("in_production"),
            "awaiting_go_live": _count("ready_for_production"),
            "certified":        _count("certified"),
            "certifying":       _count("certifying"),
            "ready":            _count("ready_for_certification") + _count("ready"),  # legacy
            "tested":           _count("tested"),
            "applied":          _count("applied"),
            "accepted":         _count("accepted"),
            "received":         _count("received"),
            "withdrawn":        _count("withdrawn"),
            "blocked":          sum(1 for p in partners_out if p["blocked"]),
            "in_progress":      _count("in_progress"),  # legacy backward-compat
        },
    }


@router.get("/certification/agent-messages")
def certification_agent_messages(db: DbDep, _: CurrentUser, limit: int = 100):
    """Recent A2A messages exchanged with the cert_engine partner. Drives
    AgentMessaging.jsx. Useful for debugging the Authority ↔ cert-agent traffic.
    """
    cert_engines = []
    for p in db.scalars(select(PartnerAgent)).all():
        types = p.partner_type or []
        if isinstance(types, str):
            types = [types]
        if "cert_engine" in types:
            cert_engines.append(p.id)

    if not cert_engines:
        return {"messages": [], "engine_count": 0}

    messages = db.scalars(
        select(A2AMessage)
        .where(A2AMessage.partner_id.in_(cert_engines))
        .order_by(A2AMessage.created_at.desc())
        .limit(limit)
    ).all()

    return {
        "engine_count": len(cert_engines),
        "messages": [
            {
                "id":          m.id,
                "change_id":   m.change_request_id,
                "partner_id":  m.partner_id,
                "direction":   m.direction.value if hasattr(m.direction, "value") else m.direction,
                "task_type":   m.task_type.value if hasattr(m.task_type, "value") else m.task_type,
                "status":      m.status,
                "created_at":  m.created_at.isoformat() if m.created_at else None,
                "summary":     (m.payload or {}).get("summary"),
                "cert_run_id": (m.payload or {}).get("cert_run_id"),
            }
            for m in messages
        ],
    }
