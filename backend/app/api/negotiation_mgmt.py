# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Negotiation Management API — BRD requirements, round states, clusters, governance.

Endpoints:
  BRD requirements CRUD (per change)
  Round state management (per change × partner)
  Cross-partner cluster view and PM decision actions
  Governance: finalize negotiation, create new version
  Silent acceptance sweep
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app.core.deps import DbDep, CurrentUser
from app.models.base import generate_uuid, utcnow
from app.models.change_request import ChangeRequest
from app.models.user import UserRole

# Negotiation outcomes are PM workflow data — the product-kit-scoped review
# teams (tech_lead / infosec_reviewer / risk_reviewer) must not read them.
_NEGOTIATION_ROLES = {UserRole.PRODUCT_MANAGER, UserRole.PRODUCT_OWNER, UserRole.ADMIN}
from app.models.phase_c import (
    BRDRequirement,
    ClusterPMDecision,
    CounterProposal,
    NegotiationCluster,
    NegotiationClusterMember,
    NegotiationRoundState,
    PartnerAgent,
    RoundStatus,
)
from app.services.negotiation_extended import (
    apply_silent_acceptances,
    create_new_version,
    finalize_negotiation,
    get_active_round,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["negotiation-mgmt"])


async def _notify_partner_cp_decision(
    partner_id: str,
    change_id: str,
    counter_proposal_id: str,
    negotiation_round: int,
    decision: str,
    resolution_text: str,
    justification: str,
) -> None:
    """Background: deliver one counter-decision to a partner.

    Goes through the active domain pack's channel rather than calling A2A
    directly. Notifying a partner of a decision is a generic act — an internal
    API deprecation would post it to a GitHub issue — so the transport is the
    pack's choice. For the network the pack returns the A2A channel and the wire
    behaviour is unchanged.

    A pack that declares no channel (`channel_of(...) is None`) means the domain
    has no way to reach partners at all. That is a real state, not an error: the
    OCPP shape publishes and nobody is notified. Skipping is correct there.
    """
    from app.core.domain.contract import OutboundMessage, Partner, channel_of
    from app.core.domain.registry import get_active_pack

    channel = channel_of(get_active_pack())
    if channel is None:
        logger.info("CP decision notify skipped: domain has no partner channel")
        return

    try:
        # No local session or partner lookup: the channel owns partner
        # resolution and the audit row. Doing it here too would open a second
        # session and query the same row twice.
        result = await channel.deliver(
            Partner(key=partner_id, label=partner_id),
            OutboundMessage(
                # Protocol v1: first-class counter_decision task type (§6.7).
                kind="counter_decision",
                change_id=change_id,
                payload={
                    "change_id":              change_id,
                    "decision":               decision,
                    "in_response_to":         counter_proposal_id,
                    "negotiation_round":      negotiation_round,
                    "resolution_text":        resolution_text,
                    "original_justification": justification,
                },
            ),
        )
        if result.delivered:
            logger.info(
                "CP decision notified: partner=%s change=%s cp=%s decision=%s",
                partner_id, change_id, counter_proposal_id, decision,
            )
        else:
            # Previously a transport failure raised and was caught below. The
            # channel reports it instead, so log it rather than losing it.
            logger.warning(
                "CP decision notify not delivered: partner=%s change=%s cp=%s error=%s",
                partner_id, change_id, counter_proposal_id, result.error,
            )
    except Exception:
        logger.exception(
            "CP decision notify failed: partner=%s change=%s cp=%s",
            partner_id, change_id, counter_proposal_id,
        )


async def _notify_partner_round_closed(
    partner_id: str,
    change_id: str,
    negotiation_round: int,
    closed_at: str,
) -> None:
    """Background: deliver one ROUND_CLOSED A2A message to a partner.

    Partners have no round UI of their own, so a PM force-close is
    otherwise invisible to them. Delegates to the first-class
    send_round_closed helper (v1.0+ext ROUND_CLOSED task type) — was
    previously piggybacked on CLARIFICATION_RESPONSE with a message_kind
    discriminator, which meant the partner's dispatcher had no direct hook.
    `closed_at` is accepted for signature compatibility but is re-derived
    from the round state inside send_round_closed.
    """
    from app.core.database import SessionLocal
    from app.services.negotiation_extended import send_round_closed

    db = SessionLocal()
    try:
        await send_round_closed(
            change_request_id=change_id,
            partner_id=partner_id,
            round_number=negotiation_round,
            close_reason="pm_forced",
            db=db,
        )
        logger.info(
            "Round-closed notified: partner=%s change=%s round=%s",
            partner_id, change_id, negotiation_round,
        )
    finally:
        db.close()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class BRDRequirementCreate(BaseModel):
    label: str
    description: str | None = None
    category: str = "general"
    is_mandatory: bool = False
    tolerance_config: dict | None = None


class BRDRequirementUpdate(BaseModel):
    label: str | None = None
    description: str | None = None
    category: str | None = None
    is_mandatory: bool | None = None
    tolerance_config: dict | None = None


class ClusterDecisionRequest(BaseModel):
    decision: str  # accept | modify | reject
    decision_text: str | None = None
    modified_value: dict | None = None  # only for 'modify'


# ── BRD Requirements ──────────────────────────────────────────────────────────

@router.get("/changes/{change_id}/brd-requirements")
def list_brd_requirements(change_id: str, db: DbDep, current_user: CurrentUser):
    reqs = (
        db.query(BRDRequirement)
        .filter(BRDRequirement.change_request_id == change_id)
        .order_by(BRDRequirement.created_at)
        .all()
    )
    return [
        {
            "id": r.id,
            "label": r.label,
            "description": r.description,
            "category": r.category,
            "is_mandatory": r.is_mandatory,
            "tolerance_config": r.tolerance_config,
            "source": getattr(r, "source", "manual"),
            "ai_rationale": getattr(r, "ai_rationale", None),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in reqs
    ]


@router.post("/changes/{change_id}/brd-requirements", status_code=201)
def create_brd_requirement(
    change_id: str,
    body: BRDRequirementCreate,
    db: DbDep,
    current_user: CurrentUser,
):
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(404, "Change not found")

    req = BRDRequirement(
        id=generate_uuid(),
        change_request_id=change_id,
        label=body.label,
        description=body.description,
        category=body.category,
        is_mandatory=body.is_mandatory,
        tolerance_config=body.tolerance_config,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return {"id": req.id, "label": req.label, "is_mandatory": req.is_mandatory}


@router.put("/changes/{change_id}/brd-requirements/{req_id}")
def update_brd_requirement(
    change_id: str,
    req_id: str,
    body: BRDRequirementUpdate,
    background_tasks: BackgroundTasks,
    db: DbDep,
    current_user: CurrentUser,
):
    req = (
        db.query(BRDRequirement)
        .filter(BRDRequirement.id == req_id, BRDRequirement.change_request_id == change_id)
        .first()
    )
    if not req:
        raise HTTPException(404, "Requirement not found")

    # Track whether a *classification-relevant* field changed. The mandatory
    # flag, category, and tolerance all feed the auto-accept/reject classifier,
    # so editing any of them must re-evaluate the change's open counters. Label
    # and description are cosmetic and don't re-trigger classification.
    classification_changed = False
    if body.label is not None:
        req.label = body.label
    if body.description is not None:
        req.description = body.description
    if body.category is not None:
        classification_changed = classification_changed or (req.category != body.category)
        req.category = body.category
    if body.is_mandatory is not None:
        classification_changed = classification_changed or (req.is_mandatory != body.is_mandatory)
        req.is_mandatory = body.is_mandatory
    if body.tolerance_config is not None:
        classification_changed = classification_changed or (req.tolerance_config != body.tolerance_config)
        req.tolerance_config = body.tolerance_config

    req.updated_at = datetime.now(timezone.utc)
    db.commit()

    # Re-run auto-disposition on the change's open counters so the PM's edit
    # takes effect on counters that already arrived — not just future ones.
    if classification_changed:
        from app.services.negotiation_extended import reclassify_open_counter_proposals
        background_tasks.add_task(reclassify_open_counter_proposals, change_id)

    return {
        "id": req.id,
        "label": req.label,
        "is_mandatory": req.is_mandatory,
        "reclassification_triggered": classification_changed,
    }


@router.delete("/changes/{change_id}/brd-requirements/{req_id}", status_code=204)
def delete_brd_requirement(
    change_id: str,
    req_id: str,
    db: DbDep,
    current_user: CurrentUser,
):
    req = (
        db.query(BRDRequirement)
        .filter(BRDRequirement.id == req_id, BRDRequirement.change_request_id == change_id)
        .first()
    )
    if not req:
        raise HTTPException(404, "Requirement not found")
    db.delete(req)
    db.commit()


@router.post("/changes/{change_id}/brd-requirements/generate")
async def generate_brd_requirements(
    change_id: str,
    db: DbDep,
    current_user: CurrentUser,
):
    """Read the change's BRD and let an LLM extract + classify requirements.

    Each extracted requirement is persisted as a BRDRequirement with
    source='ai' and an ai_rationale. Non-destructive: requirements whose
    label already exists (case-insensitive) are skipped, so the PM's manual
    rows and prior edits are preserved across re-runs. The PM can then toggle
    mandatory/optional, edit, or delete any of them — the negotiation
    classifier reads the live is_mandatory flag regardless of source.
    """
    from app.models.brd import BRD
    from app.agents.brd_extractor import extract_brd_requirements

    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(404, "Change not found")

    brd = (
        db.query(BRD)
        .filter(BRD.change_request_id == change_id)
        .order_by(BRD.version.desc())
        .first()
    )
    if not brd or not (brd.content or "").strip():
        raise HTTPException(400, "No BRD document with content found for this change")

    extracted = await extract_brd_requirements(brd.content)
    if not extracted:
        return {"created": [], "skipped": 0, "total": 0,
                "message": "No requirements could be extracted from the BRD"}

    existing_labels = {
        (r.label or "").strip().lower()
        for r in db.query(BRDRequirement).filter(
            BRDRequirement.change_request_id == change_id
        ).all()
    }

    created = []
    skipped = 0
    for item in extracted:
        if item["label"].strip().lower() in existing_labels:
            skipped += 1
            continue
        req = BRDRequirement(
            id=generate_uuid(),
            change_request_id=change_id,
            label=item["label"],
            description=item["description"],
            category=item["category"],
            is_mandatory=item["is_mandatory"],
            tolerance_config=item["tolerance_config"],
            source="ai",
            ai_rationale=item["rationale"],
        )
        db.add(req)
        existing_labels.add(item["label"].strip().lower())
        created.append(req)

    db.commit()
    return {
        "created": [
            {
                "id": r.id,
                "label": r.label,
                "category": r.category,
                "is_mandatory": r.is_mandatory,
                "tolerance_config": r.tolerance_config,
                "source": r.source,
                "ai_rationale": r.ai_rationale,
            }
            for r in created
        ],
        "skipped": skipped,
        "total": len(extracted),
    }


# ── Round States ──────────────────────────────────────────────────────────────

def _cp_brief(cp: CounterProposal) -> dict:
    """Compact counter-proposal shape for the rounds grid."""
    return {
        "id": cp.id,
        "partner_id": cp.partner_id,
        "justification": cp.justification,
        "request_category": cp.request_category,
        "brd_classification": cp.brd_classification,
        "auto_disposition": cp.auto_disposition,
        "status": cp.status.value if hasattr(cp.status, "value") else cp.status,
        "negotiation_round": cp.negotiation_round,
        "created_at": cp.created_at.isoformat() if cp.created_at else None,
    }


@router.get("/changes/{change_id}/negotiation/rounds")
def list_round_states(change_id: str, db: DbDep, current_user: CurrentUser):
    """Return per-partner round summary for a change, with the counter-proposals
    raised in each round.

    Rows come from two sources, unioned by (partner, round_number):
      1. NegotiationRoundState — the tracked negotiation window (deadline,
         silent-acceptance status).
      2. Counter-proposals that carry a `negotiation_round` but have no state
         row yet (e.g. created before round-state tracking existed). These are
         surfaced as synthetic `responded` rows so the counters stay visible.
    """
    states: list[NegotiationRoundState] = (
        db.query(NegotiationRoundState)
        .filter(NegotiationRoundState.change_request_id == change_id)
        .order_by(NegotiationRoundState.partner_id, NegotiationRoundState.round_number)
        .all()
    )
    cps: list[CounterProposal] = (
        db.query(CounterProposal)
        .filter(CounterProposal.change_request_id == change_id)
        .all()
    )

    partner_ids = {s.partner_id for s in states} | {cp.partner_id for cp in cps}
    partners = db.query(PartnerAgent).filter(PartnerAgent.id.in_(partner_ids)).all()
    partner_name = {p.id: p.name for p in partners}

    # Group counters by (partner, round). CPs without an explicit round default
    # to round 1 so they still attach somewhere visible.
    cps_by_round: dict[tuple[str, int], list[CounterProposal]] = {}
    for cp in cps:
        key = (cp.partner_id, cp.negotiation_round or 1)
        cps_by_round.setdefault(key, []).append(cp)

    now = datetime.now(timezone.utc)
    result = []
    seen: set[tuple[str, int]] = set()

    for s in states:
        key = (s.partner_id, s.round_number)
        seen.add(key)
        deadline = s.deadline_at
        if deadline and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        seconds_remaining = max(0, int((deadline - now).total_seconds())) if deadline else 0
        result.append({
            "id": s.id,
            "partner_id": s.partner_id,
            "partner_name": partner_name.get(s.partner_id, "Unknown"),
            "round_number": s.round_number,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "deadline_at": s.deadline_at.isoformat() if s.deadline_at else None,
            "seconds_remaining": seconds_remaining,
            "status": s.status,
            "closed_at": s.closed_at.isoformat() if s.closed_at else None,
            "counter_proposals": [_cp_brief(cp) for cp in cps_by_round.get(key, [])],
        })

    # Synthetic rows for counter-rounds that never got a state record.
    for (pid, rnd), cplist in cps_by_round.items():
        if (pid, rnd) in seen:
            continue
        result.append({
            "id": f"cp-round-{pid}-{rnd}",
            "partner_id": pid,
            "partner_name": partner_name.get(pid, "Unknown"),
            "round_number": rnd,
            "started_at": None,
            "deadline_at": None,
            "seconds_remaining": 0,
            "status": "responded",
            "closed_at": None,
            "counter_proposals": [_cp_brief(cp) for cp in cplist],
        })

    result.sort(key=lambda r: (r["partner_name"], r["round_number"]))
    return result


@router.post("/changes/{change_id}/negotiation/rounds/{partner_id}/close")
def close_round(
    change_id: str,
    partner_id: str,
    background_tasks: BackgroundTasks,
    db: DbDep,
    current_user: CurrentUser,
):
    """PM force-closes the active round for a specific partner (no silent acceptance).

    Notifies the partner over A2A so the close is visible on their side —
    they have no round UI, so this is their only signal the window shut.
    """
    state = get_active_round(change_id, partner_id, db)
    if not state:
        raise HTTPException(404, "No open round found for this partner")

    now = datetime.now(timezone.utc)
    state.status = "closed_by_pm"
    state.closed_at = now
    round_number = state.round_number
    db.commit()

    background_tasks.add_task(
        _notify_partner_round_closed,
        partner_id=partner_id,
        change_id=change_id,
        negotiation_round=round_number,
        closed_at=now.isoformat(),
    )
    return {"status": "closed", "round": round_number}


@router.post("/changes/{change_id}/negotiation/rounds/sweep")
async def sweep_silent_acceptances(
    change_id: str,
    db: DbDep,
    current_user: CurrentUser,
):
    """On-demand sweep: apply silent acceptance to all overdue round states for this change."""
    from app.services.negotiation_extended import send_round_events

    affected, events = apply_silent_acceptances(db)
    db.commit()
    await send_round_events(events, db)
    return {"silently_accepted": affected}


# ── Cross-Partner Clusters ────────────────────────────────────────────────────

@router.get("/changes/{change_id}/negotiation/clusters")
def list_clusters(change_id: str, db: DbDep, current_user: CurrentUser):
    clusters: list[NegotiationCluster] = (
        db.query(NegotiationCluster)
        .filter(NegotiationCluster.change_request_id == change_id)
        .order_by(NegotiationCluster.partner_count.desc(), NegotiationCluster.created_at)
        .all()
    )

    result = []
    for c in clusters:
        members: list[NegotiationClusterMember] = (
            db.query(NegotiationClusterMember)
            .filter(NegotiationClusterMember.cluster_id == c.id)
            .all()
        )
        cp_ids = [m.counter_proposal_id for m in members]
        cps: list[CounterProposal] = (
            db.query(CounterProposal).filter(CounterProposal.id.in_(cp_ids)).all()
        )
        partner_ids_in_cluster = list({cp.partner_id for cp in cps})
        partners = db.query(PartnerAgent).filter(PartnerAgent.id.in_(partner_ids_in_cluster)).all()
        partner_names = [p.name for p in partners]

        result.append({
            "id": c.id,
            "cluster_key": c.cluster_key,
            "category": c.category,
            "topic_summary": c.topic_summary,
            "partner_count": c.partner_count,
            "partner_names": partner_names,
            "ai_summary": c.ai_summary,
            "ai_recommendation": c.ai_recommendation,
            "confidence_score": c.confidence_score,
            "pm_decision": c.pm_decision,
            "pm_decision_text": c.pm_decision_text,
            "pm_modified_value": c.pm_modified_value,
            "pm_decided_at": c.pm_decided_at.isoformat() if c.pm_decided_at else None,
            "conflict_with_cluster_id": c.conflict_with_cluster_id,
            "counter_proposals": [
                {
                    "id": cp.id,
                    "partner_id": cp.partner_id,
                    "justification": cp.justification,
                    "brd_classification": cp.brd_classification,
                    "auto_disposition": cp.auto_disposition,
                    "status": cp.status.value if hasattr(cp.status, "value") else cp.status,
                    "request_category": cp.request_category,
                    "payload": cp.payload,
                    "created_at": cp.created_at.isoformat() if cp.created_at else None,
                }
                for cp in cps
            ],
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        })
    return result


@router.post("/changes/{change_id}/negotiation/clusters/{cluster_id}/decide")
async def decide_cluster(
    change_id: str,
    cluster_id: str,
    body: ClusterDecisionRequest,
    background_tasks: BackgroundTasks,
    db: DbDep,
    current_user: CurrentUser,
):
    """PM accept / modify / reject a cluster — decision applies to all member CPs."""
    cluster = (
        db.query(NegotiationCluster)
        .filter(NegotiationCluster.id == cluster_id, NegotiationCluster.change_request_id == change_id)
        .first()
    )
    if not cluster:
        raise HTTPException(404, "Cluster not found")

    if body.decision not in {"accept", "modify", "reject"}:
        raise HTTPException(422, "decision must be accept, modify, or reject")

    now = datetime.now(timezone.utc)
    cluster.pm_decision = body.decision
    cluster.pm_decision_text = body.decision_text
    cluster.pm_modified_value = body.modified_value
    cluster.pm_decided_at = now
    cluster.pm_decided_by = current_user.id
    cluster.updated_at = now

    # Reflect the cluster decision onto member counter-proposals
    members: list[NegotiationClusterMember] = (
        db.query(NegotiationClusterMember)
        .filter(NegotiationClusterMember.cluster_id == cluster_id)
        .all()
    )
    cp_ids = [m.counter_proposal_id for m in members]
    cps: list[CounterProposal] = (
        db.query(CounterProposal).filter(CounterProposal.id.in_(cp_ids)).all()
    )

    from app.models.phase_c import CounterProposalStatus
    new_cp_status = (
        CounterProposalStatus.ACCEPTED if body.decision in {"accept", "modify"}
        else CounterProposalStatus.REJECTED
    )
    resolution_text = body.decision_text or f"Cluster decision: {body.decision}"

    # Track which CPs were actually open (and thus actually changed).
    affected_cps = []
    for cp in cps:
        if cp.status == CounterProposalStatus.OPEN:
            cp.status = new_cp_status
            cp.resolution_text = resolution_text
            cp.resolved_at = now
            cp.resolved_by = current_user.id
            affected_cps.append(cp)

    db.commit()

    # Notify each affected partner of the decision via A2A so their
    # counter_decisions log and outgoing_queries status update in real time.
    a2a_decision = "ACCEPT" if body.decision in {"accept", "modify"} else "REJECT"
    for cp in affected_cps:
        background_tasks.add_task(
            _notify_partner_cp_decision,
            partner_id=cp.partner_id,
            change_id=change_id,
            counter_proposal_id=cp.counter_proposal_id or cp.id,
            negotiation_round=cp.negotiation_round or 1,
            decision=a2a_decision,
            resolution_text=resolution_text,
            justification=cp.justification or "",
        )

    return {
        "cluster_id": cluster_id,
        "pm_decision": body.decision,
        "affected_cps": len(cps),
    }


@router.post("/changes/{change_id}/negotiation/clusters/{cluster_id}/refresh-ai")
async def refresh_cluster_ai(
    change_id: str,
    cluster_id: str,
    db: DbDep,
    current_user: CurrentUser,
):
    """Trigger a fresh AI analysis for a cluster (PM-initiated)."""
    from app.services.negotiation_extended import _refresh_cluster_ai_summary
    cluster = (
        db.query(NegotiationCluster)
        .filter(NegotiationCluster.id == cluster_id, NegotiationCluster.change_request_id == change_id)
        .first()
    )
    if not cluster:
        raise HTTPException(404, "Cluster not found")

    await _refresh_cluster_ai_summary(cluster, db)
    db.commit()
    return {
        "cluster_id": cluster_id,
        "ai_summary": cluster.ai_summary,
        "ai_recommendation": cluster.ai_recommendation,
        "confidence_score": cluster.confidence_score,
    }


# ── Governance ────────────────────────────────────────────────────────────────

@router.post("/changes/{change_id}/negotiate/finalize")
async def finalize(change_id: str, db: DbDep, current_user: CurrentUser):
    """Lock the negotiation — closes all open CPs, sets negotiation_finalized_at."""
    from app.services.negotiation_extended import send_round_events
    try:
        change, round_events = finalize_negotiation(change_id, current_user.id, db)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    db.commit()
    # Fan round_closed(pm_forced) per round the finalize retired, so the
    # partner sees the round shut even though this endpoint doesn't touch the
    # version or auto-open a next round.
    await send_round_events(round_events, db)
    return {
        "change_id": change_id,
        "negotiation_finalized_at": change.negotiation_finalized_at.isoformat() if change.negotiation_finalized_at else None,
        "negotiation_version": change.negotiation_version,
    }


@router.post("/changes/{change_id}/negotiate/new-version")
def new_version(change_id: str, db: DbDep, current_user: CurrentUser):
    """Increment the negotiation version — partners must review and accept the new version."""
    try:
        new_ver = create_new_version(change_id, db)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    db.commit()
    return {
        "change_id": change_id,
        "negotiation_version": new_ver,
    }


@router.post("/changes/{change_id}/negotiate/advance-round")
async def advance_round(change_id: str, db: DbDep, current_user: CurrentUser):
    """PM: "Reviewed — no kit change needed this round."

    Closes the current round without shipping a new version and starts the
    next round on the same version (fresh window), or freezes the negotiation
    once the round cap is reached. The no-document-change path to the freeze,
    so a quiet round still advances the cap instead of stalling. When it
    freezes, the partner is notified so its UI locks too.
    """
    from app.services.negotiation_extended import (
        advance_round_no_change,
        notify_partners_frozen,
        send_round_events,
    )
    pre = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    was_frozen = bool(pre and getattr(pre, "negotiation_frozen_at", None))
    try:
        cr, round_events = advance_round_no_change(
            change_id, db, actor_user_id=getattr(current_user, "id", None),
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    db.commit()
    # Fan round_closed + round_opened notices to each partner BEFORE the
    # freeze/hold-clear notices, so the partner's timeline lands "round closed
    # → round opened" in that order and the follow-up (freeze or hold-clear)
    # arrives against a partner that already knows the round has moved.
    await send_round_events(round_events, db)
    if getattr(cr, "negotiation_frozen_at", None) and not was_frozen:
        await notify_partners_frozen(change_id, db)
    else:
        # Advanced (not frozen). A prior round may have set the query hold on
        # the partner (e.g. an auto-drafted plan the PM discarded). Nothing else
        # clears it on an advance — only a shipped kit or a freeze does — so lift
        # it explicitly, otherwise the partner composer stays locked through the
        # new round. Idempotent on the partner side.
        from app.services.kit_revision_runner import notify_partners_revision_hold
        target = (cr.negotiation_version or 1) + 1
        try:
            await notify_partners_revision_hold(change_id, target, in_progress=False)
        except Exception:
            logger.exception("revision-hold clear failed on advance-round: change=%s", change_id)
    return {
        "change_id": change_id,
        "negotiation_version": cr.negotiation_version,
        "negotiation_frozen_at": cr.negotiation_frozen_at.isoformat() if cr.negotiation_frozen_at else None,
    }


@router.post("/changes/{change_id}/negotiate/new-version-and-ship")
async def new_version_and_ship(change_id: str, db: DbDep, current_user: CurrentUser):
    """Publish a new kit version and ship it to partners.

    Bumps negotiation_version, clones the latest kit docs as new versions tagged
    with the bumped version (placeholder content for now), snapshots the
    publication, and re-dispatches to active partners (who must re-accept).
    """
    from app.models.phase_c import ChangePartnerAssignment
    from app.models.product_kit import ProductKitDocument, ProductKitDocType
    from app.services.change_dispatch import (
        build_kit_envelope, dispatch_kit_to_partners, snapshot_publication,
    )
    from app.services.product_kit_query import latest_kit_docs

    cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not cr:
        raise HTTPException(404, "Change not found")

    # 0. Consolidate the closing round's outcomes BEFORE bumping the version,
    #    then generate the partner-facing change summary (Slice 4).
    from app.agents.version_change_summary import summarize_version_changes
    from app.services.negotiation_extended import (
        close_open_rounds_for_version_ship,
        collect_round_outcomes,
        send_round_events,
    )
    prev_ver = getattr(cr, "negotiation_version", 1) or 1
    outcomes = collect_round_outcomes(change_id, db)
    change_summary = await summarize_version_changes(
        previous_version=prev_ver,
        new_version=prev_ver + 1,
        outcomes=outcomes,
        change_title=cr.title or "",
    )

    # 0b. Any round still OPEN/RESPONDED on the previous version is retired with
    # reason=superseded_by_version — otherwise the partner sees the new kit
    # arrive with no notice that the prior round was abandoned. Notices fire
    # after we commit the new version so the sequence lands cleanly.
    superseded_events = close_open_rounds_for_version_ship(change_id, db)

    # 1. Bump the published version (also reopens negotiation).
    new_ver = create_new_version(change_id, db)

    # 2. Clone the latest kit docs as new version rows tagged with the bumped
    #    negotiation_version. Content is carried forward unchanged until the real
    #    regeneration pipeline lands. Binary paths intentionally null — the
    #    partner re-renders from the markdown.
    cloned = 0
    for doc in latest_kit_docs(db, change_id):
        db.add(ProductKitDocument(
            change_request_id=change_id,
            doc_type=doc.doc_type if isinstance(doc.doc_type, ProductKitDocType)
                     else ProductKitDocType(doc.doc_type),
            content=(doc.content or ""),
            version=doc.version + 1,
            negotiation_version=new_ver,
        ))
        cloned += 1
    db.commit()
    db.refresh(cr)

    # 3. Package from the freshly-cloned latest docs, snapshot, dispatch.
    envelope = build_kit_envelope(cr, db, change_summary=change_summary)
    publication = snapshot_publication(
        cr, envelope, db,
        revision_reason=(change_summary[:480] if change_summary else "revision"),
        published_by=current_user.id if hasattr(current_user, "id") else None,
    )

    assignments = db.query(ChangePartnerAssignment).filter(
        ChangePartnerAssignment.change_request_id == change_id
    ).all()
    dispatch = await dispatch_kit_to_partners(
        cr, envelope, assignments, db,
        current_user.id if hasattr(current_user, "id") else None,
        mode="revision",
    )

    # Fan the round_closed(superseded_by_version) notices AFTER dispatch so the
    # partner receives them alongside the new change_communication — the pair
    # tells the whole story: "your open round is retired because v(N+1) is here".
    await send_round_events(superseded_events, db)

    return {
        "change_id": change_id,
        "negotiation_version": new_ver,
        "publication_id": publication.id,
        "docs_cloned": cloned,
        "change_summary": change_summary,
        **dispatch,
    }


@router.get("/changes/{change_id}/negotiate/round-close-summary")
def round_close_summary(change_id: str, db: DbDep, current_user: CurrentUser):
    """Consolidated round outcomes for the PM (Slice 4).

    Surfaces, across all partners: the decided counter clusters and the union
    of documents the doc-impact assessments flagged for change. The PM uses
    this to regenerate the affected kit docs, then ships v(N+1) via
    new-version-and-ship (which is PM-gated by design — D-2)."""
    # Review finding SEC-2: this exposed cross-change negotiation outcomes to any
    # authenticated user, incl. the product-kit-scoped review teams. Gate to PM.
    if current_user.role not in _NEGOTIATION_ROLES:
        raise HTTPException(403, "Only PM/PO/admin can view negotiation round summaries")
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(404, "Change not found")

    from app.services.negotiation_extended import collect_round_outcomes
    outcomes = collect_round_outcomes(change_id, db)
    docs_to_update = sorted({d for o in outcomes for d in (o.get("documents") or [])})

    # Are all current-version rounds closed? (silent-accept / responded / closed)
    open_rounds = (
        db.query(NegotiationRoundState)
        .filter(
            NegotiationRoundState.change_request_id == change_id,
            NegotiationRoundState.status == RoundStatus.OPEN.value,
        )
        .count()
    )
    return {
        "change_id": change_id,
        "negotiation_version": getattr(change, "negotiation_version", 1) or 1,
        "frozen": getattr(change, "negotiation_frozen_at", None) is not None,
        "open_rounds": open_rounds,
        "all_rounds_closed": open_rounds == 0,
        "documents_to_update": docs_to_update,
        "outcomes": outcomes,
    }


# ── Kit revision plan (the editable v(N+1) plan) ─────────────────────────────

def _serialize_plan(p) -> dict:
    return {
        "id": p.id,
        "change_request_id": p.change_request_id,
        "target_version": p.target_version,
        "status": p.status,
        "items": p.items or [],
        "summary": p.summary or "",
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _current_plan(db, change_id: str, target_version: int):
    from app.models.kit_revision_plan import KitRevisionPlan
    return (
        db.query(KitRevisionPlan)
        .filter(
            KitRevisionPlan.change_request_id == change_id,
            KitRevisionPlan.target_version == target_version,
        )
        .order_by(KitRevisionPlan.created_at.desc())
        .first()
    )


@router.get("/changes/{change_id}/negotiate/revision-plan")
def get_revision_plan(change_id: str, db: DbDep, current_user: CurrentUser):
    """The ACTIVE revision plan for this change, or {plan: null} when none.

    Active = the latest plan that hasn't shipped. Its `target_version` is the
    single number the UI labels everything with (chip + panel), so they can't
    disagree: while drafting/generating it's current+1; once generated (which
    bumped the version) it equals the current version and is ready to ship."""
    if current_user.role not in _NEGOTIATION_ROLES:
        raise HTTPException(403, "Only PM/PO/admin can view the revision plan")
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(404, "Change not found")

    from app.models.kit_revision_plan import KitRevisionPlan, RP_STATUS_SHIPPED
    active = (
        db.query(KitRevisionPlan)
        .filter(
            KitRevisionPlan.change_request_id == change_id,
            KitRevisionPlan.status != RP_STATUS_SHIPPED,
        )
        .order_by(KitRevisionPlan.created_at.desc())
        .first()
    )
    target = active.target_version if active else ((getattr(change, "negotiation_version", 1) or 1) + 1)
    return {"target_version": target, "plan": _serialize_plan(active) if active else None}


@router.post("/changes/{change_id}/negotiate/revision-plan/draft")
async def draft_revision_plan(change_id: str, db: DbDep, current_user: CurrentUser):
    """(Re)build the draft plan for v(N+1) from the round's resolved outcomes.

    Upserts the KitRevisionPlan for the target version. Overwrites items only
    while the plan is still in draft — once the PM has edited it, a re-draft is
    rejected so their edits aren't clobbered (they can clear items manually)."""
    if current_user.role not in _NEGOTIATION_ROLES:
        raise HTTPException(403, "Only PM/PO/admin can draft the revision plan")
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(404, "Change not found")

    from app.agents.revision_planner import plan_revision
    from app.models.kit_revision_plan import (
        KitRevisionPlan, RP_STATUS_DRAFT, RP_STATUS_GENERATED, RP_STATUS_NEEDS_RETRY,
        RP_STATUS_SHIPPED,
    )
    from app.services.negotiation_extended import collect_round_outcomes

    current_ver = getattr(change, "negotiation_version", 1) or 1
    target = current_ver + 1

    existing = _current_plan(db, change_id, target)
    # A generated/shipped plan has already produced (or sent) v(N+1) — re-drafting
    # would desync the plan from the docs, so it's rejected. A draft/edited plan,
    # or one stuck in 'generating' (e.g. the worker died mid-run), can be
    # re-drafted to recover.
    if existing and existing.status in (RP_STATUS_GENERATED, RP_STATUS_SHIPPED):
        raise HTTPException(
            409,
            f"v{target} has already been {existing.status}; re-draft is not allowed.",
        )

    outcomes = collect_round_outcomes(change_id, db)
    result = await plan_revision(
        outcomes=outcomes, change_title=change.title or "", current_version=current_ver,
    )
    new_status = RP_STATUS_DRAFT if result.get("ok", True) else RP_STATUS_NEEDS_RETRY

    is_new = existing is None
    if existing:
        existing.items = result["items"]
        existing.summary = result["summary"]
        existing.status = new_status
        existing.updated_by = current_user.id
        plan = existing
    else:
        plan = KitRevisionPlan(
            change_request_id=change_id,
            target_version=target,
            status=new_status,
            items=result["items"],
            summary=result["summary"],
            created_by=current_user.id,
            updated_by=current_user.id,
        )
        db.add(plan)
    try:
        db.commit()
    except IntegrityError:
        # The unique (change, target_version) index fired — the Celery sweep's
        # auto-prepare raced us and inserted the plan first. Return its row
        # instead of a 500; the PM sees the same draft either way.
        db.rollback()
        plan = _current_plan(db, change_id, target)
        if not plan:
            raise
        return _serialize_plan(plan)
    db.refresh(plan)
    if is_new and (plan.items or []):
        # First plan for this version AND it actually revises documents → hold
        # partner queries until the kit ships. An empty-items plan means "no docs
        # to change", so we must not lock the composer (nothing clears it on an
        # advance — only a shipped kit or a freeze does).
        from app.services.kit_revision_runner import notify_partners_revision_hold
        await notify_partners_revision_hold(change_id, target)
    return _serialize_plan(plan)


class RevisionPlanUpdate(BaseModel):
    items: list[dict]
    summary: str | None = None


@router.put("/changes/{change_id}/negotiate/revision-plan")
def update_revision_plan(
    change_id: str, body: RevisionPlanUpdate, db: DbDep, current_user: CurrentUser,
):
    """Save the PM's edits to the plan (items + summary). Marks it 'edited'."""
    if current_user.role not in _NEGOTIATION_ROLES:
        raise HTTPException(403, "Only PM/PO/admin can edit the revision plan")
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(404, "Change not found")
    from app.models.kit_revision_plan import RP_STATUS_EDITED
    target = (getattr(change, "negotiation_version", 1) or 1) + 1
    plan = _current_plan(db, change_id, target)
    if not plan:
        raise HTTPException(404, "No revision plan to edit — draft one first")
    # Keep only known fields per item; default include=True.
    clean = []
    for it in (body.items or []):
        clean.append({
            "doc_type": str(it.get("doc_type") or "").strip(),
            "change_instruction": str(it.get("change_instruction") or "").strip(),
            "rationale": str(it.get("rationale") or "").strip(),
            "include": bool(it.get("include", True)),
        })
    plan.items = clean
    if body.summary is not None:
        plan.summary = body.summary
    plan.status = RP_STATUS_EDITED
    plan.updated_by = current_user.id
    db.commit()
    db.refresh(plan)
    return _serialize_plan(plan)


@router.post("/changes/{change_id}/negotiate/revision-plan/generate")
async def generate_revision_kit(
    change_id: str, db: DbDep, current_user: CurrentUser, background: BackgroundTasks,
):
    """Regenerate v(N+1) from the plan (background). Bumps the version and writes
    the new docs; does NOT ship. The PM ships from Phase C afterward."""
    if current_user.role not in _NEGOTIATION_ROLES:
        raise HTTPException(403, "Only PM/PO/admin can generate the next kit version")
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(404, "Change not found")

    from app.models.kit_revision_plan import (
        RP_STATUS_GENERATED, RP_STATUS_GENERATING,
    )
    target = (getattr(change, "negotiation_version", 1) or 1) + 1
    plan = _current_plan(db, change_id, target)
    if not plan:
        raise HTTPException(404, "No revision plan — draft one first")
    if plan.status in (RP_STATUS_GENERATING, RP_STATUS_GENERATED):
        raise HTTPException(409, f"Plan is already {plan.status}")
    plan.status = RP_STATUS_GENERATING
    plan.updated_by = current_user.id
    db.commit()

    from app.services.kit_revision_runner import generate_kit_revision
    background.add_task(generate_kit_revision, change_id, plan.id)
    return {"status": "generating", "plan_id": plan.id, "target_version": target}


@router.get("/changes/{change_id}/negotiate/revision-plan/summary.docx")
def download_revision_summary(change_id: str, db: DbDep, current_user: CurrentUser):
    """Download the change-summary as a .docx (the latest plan that has one)."""
    if current_user.role not in _NEGOTIATION_ROLES:
        raise HTTPException(403, "Only PM/PO/admin can download the change summary")
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(404, "Change not found")
    from app.models.kit_revision_plan import KitRevisionPlan
    plan = (
        db.query(KitRevisionPlan)
        .filter(
            KitRevisionPlan.change_request_id == change_id,
            KitRevisionPlan.summary.isnot(None),
        )
        .order_by(KitRevisionPlan.created_at.desc())
        .first()
    )
    if not plan or not (plan.summary or "").strip():
        raise HTTPException(404, "No change summary available yet")

    import io
    from fastapi.responses import StreamingResponse
    from app.services.change_summary_doc import build_change_summary_docx

    data = build_change_summary_docx(
        change_title=change.title or "", version=plan.target_version, summary=plan.summary,
    )
    fname = f"change_summary_v{plan.target_version}.docx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/changes/{change_id}/negotiate/status")
def negotiation_status(change_id: str, db: DbDep, current_user: CurrentUser):
    """Summary of negotiation governance state for this change."""
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(404, "Change not found")

    # Cluster summary stats
    clusters = (
        db.query(NegotiationCluster)
        .filter(NegotiationCluster.change_request_id == change_id)
        .all()
    )
    pending_clusters = sum(1 for c in clusters if c.pm_decision == ClusterPMDecision.PENDING.value)

    # Round summary
    open_rounds = (
        db.query(NegotiationRoundState)
        .filter(
            NegotiationRoundState.change_request_id == change_id,
            NegotiationRoundState.status == "open",
        )
        .count()
    )

    from app.models.phase_c import CounterProposalStatus
    open_cps = (
        db.query(CounterProposal)
        .filter(
            CounterProposal.change_request_id == change_id,
            CounterProposal.status == CounterProposalStatus.OPEN,
        )
        .count()
    )

    return {
        "change_id": change_id,
        "negotiation_version": getattr(change, "negotiation_version", 1),
        "negotiation_finalized_at": (
            change.negotiation_finalized_at.isoformat()
            if change.negotiation_finalized_at else None
        ),
        "is_finalized": change.negotiation_finalized_at is not None,
        "frozen": getattr(change, "negotiation_frozen_at", None) is not None,
        "total_clusters": len(clusters),
        "pending_clusters": pending_clusters,
        "open_rounds": open_rounds,
        "open_counter_proposals": open_cps,
    }
