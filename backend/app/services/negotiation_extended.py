# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Extended negotiation service — round state tracking, clustering, silent acceptance.

Complements `negotiation_service.py` (which handles query AI drafts).
Called by:
  - A2A handler on PROPOSAL_ACKNOWLEDGED receipt → create_round_state (round 1)
  - Phase C endpoint when PM counter-backs → create_round_state (round 2)
  - Phase C endpoint on CP receipt → classify_and_cluster_cp
  - Governance endpoint → finalize_negotiation / create_new_version
  - Background sweep (or on-demand endpoint) → apply_silent_acceptances
"""
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.models.base import generate_uuid, utcnow
from app.models.change_request import ChangeRequest
from app.models.phase_c import (
    AutoDisposition,
    BRDClassification,
    CounterProposal,
    CounterProposalStatus,
    NegotiationCluster,
    NegotiationClusterMember,
    NegotiationRoundState,
    PartnerAgent,
    RoundStatus,
)


@dataclass(frozen=True)
class RoundEvent:
    """A round transition that needs to be surfaced to the partner via A2A.

    Sync helpers (advance_round_no_change, advance_or_freeze_after_close,
    apply_silent_acceptances) return a list of these so the async caller can
    fan them out via send_round_opened / send_round_closed after commit.
    Keeps the sync/async split clean without spawning tasks from inside the
    DB helpers.
    """

    kind: str            # "opened" | "closed"
    change_request_id: str
    partner_id: str
    round_number: int
    reason: str          # opened_reason or close_reason

logger = logging.getLogger(__name__)

# Round window. Default 24h; override with NEGOTIATION_ROUND_HOURS (whole hours)
# or NEGOTIATION_ROUND_MINUTES (takes precedence — lets dev/test run the loop in
# minutes instead of waiting a day for silent-acceptance to fire).
_ROUND_MINUTES_ENV = os.environ.get("NEGOTIATION_ROUND_MINUTES")
if _ROUND_MINUTES_ENV:
    _ROUND_MINUTES = max(1, int(_ROUND_MINUTES_ENV))
else:
    _ROUND_MINUTES = int(os.environ.get("NEGOTIATION_ROUND_HOURS", "24")) * 60
_ROUND_DELTA = timedelta(minutes=_ROUND_MINUTES)

# How many negotiation rounds a change gets before the specs freeze. Each
# round runs for the configurable window above (default 2 rounds × 24h).
# Override with NEGOTIATION_MAX_ROUNDS.
MAX_ROUNDS = max(1, int(os.environ.get("NEGOTIATION_MAX_ROUNDS", "2")))


def _round_window_label() -> str:
    """Human label for the round window, e.g. '24-hour' or '5-minute'."""
    if _ROUND_MINUTES % 60 == 0:
        return f"{_ROUND_MINUTES // 60}-hour"
    return f"{_ROUND_MINUTES}-minute"


# ── Round state helpers ───────────────────────────────────────────────────────

def create_round_state(
    change_request_id: str,
    partner_id: str,
    round_number: int,
    db: Session,
) -> tuple[NegotiationRoundState, bool]:
    """Create a new round state for (change, partner, round).

    Idempotent: if a round state with the same (change, partner, round)
    already exists it is returned unchanged.

    Returns (state, was_created). was_created=False on an idempotent hit —
    async callers use the flag to decide whether to fire a round_opened
    notice to the partner (never fire on an idempotent return).
    """
    existing = (
        db.query(NegotiationRoundState)
        .filter(
            NegotiationRoundState.change_request_id == change_request_id,
            NegotiationRoundState.partner_id == partner_id,
            NegotiationRoundState.round_number == round_number,
        )
        .first()
    )
    if existing:
        return existing, False

    now = datetime.now(timezone.utc)
    state = NegotiationRoundState(
        id=generate_uuid(),
        change_request_id=change_request_id,
        partner_id=partner_id,
        round_number=round_number,
        started_at=now,
        deadline_at=now + _ROUND_DELTA,
        status=RoundStatus.OPEN.value,
    )
    db.add(state)
    db.flush()
    logger.info(
        "NegotiationRound: created round=%d for change=%s partner=%s deadline=%s",
        round_number, change_request_id, partner_id, state.deadline_at.isoformat(),
    )
    return state, True


def mark_round_responded(
    change_request_id: str,
    partner_id: str,
    db: Session,
    round_number: int | None = None,
) -> None:
    """Mark a round as responded (partner submitted a CP or acceptance).

    When `round_number` is omitted, the latest OPEN round for
    (change, partner) is used — that's what inbound A2A handlers want,
    since they know the partner responded but not which round number is
    currently open. No-op when there's no open round to close.
    """
    if round_number is None:
        state = get_active_round(change_request_id, partner_id, db)
    else:
        state = (
            db.query(NegotiationRoundState)
            .filter(
                NegotiationRoundState.change_request_id == change_request_id,
                NegotiationRoundState.partner_id == partner_id,
                NegotiationRoundState.round_number == round_number,
                NegotiationRoundState.status == RoundStatus.OPEN.value,
            )
            .first()
        )
    if state and state.status == RoundStatus.OPEN.value:
        state.status = RoundStatus.RESPONDED.value
        state.closed_at = datetime.now(timezone.utc)
        db.flush()


def get_active_round(
    change_request_id: str,
    partner_id: str,
    db: Session,
) -> NegotiationRoundState | None:
    """Return the latest open round state for (change, partner), or None."""
    return (
        db.query(NegotiationRoundState)
        .filter(
            NegotiationRoundState.change_request_id == change_request_id,
            NegotiationRoundState.partner_id == partner_id,
            NegotiationRoundState.status == RoundStatus.OPEN.value,
        )
        .order_by(NegotiationRoundState.round_number.desc())
        .first()
    )


def advance_round_no_change(
    change_request_id: str,
    db: Session,
    actor_user_id: str | None = None,
) -> tuple["ChangeRequest", list[RoundEvent]]:
    """PM action: "Reviewed — no kit change needed this round."

    Closes the current round WITHOUT shipping a new kit version (so the
    published negotiation_version is unchanged), then either opens the next
    round on the same version — starting a fresh window — or, once the round
    cap is reached, freezes the negotiation.

    This is the no-document-change counterpart to new_version_and_ship: it
    lets the round cap advance on the PM's review decision even when no
    revision is needed, instead of stalling forever because the version
    never climbs to the freeze threshold.

    Returns (change_request, round_events) — the async caller fans the events
    out via send_round_events after commit so partners see round_closed +
    round_opened (or round_closed with reason=frozen when the cap is hit).
    """
    events: list[RoundEvent] = []
    cr = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.id == change_request_id)
        .first()
    )
    if not cr:
        raise ValueError(f"Change {change_request_id} not found")
    if getattr(cr, "negotiation_frozen_at", None) is not None:
        return cr, events  # already frozen — nothing to advance

    now = datetime.now(timezone.utc)
    states = (
        db.query(NegotiationRoundState)
        .filter(NegotiationRoundState.change_request_id == change_request_id)
        .all()
    )
    if not states:
        logger.warning("advance_round_no_change: no rounds for change=%s — no-op", change_request_id)
        return cr, events

    current_round = max(s.round_number for s in states)
    partner_ids = sorted({s.partner_id for s in states})

    # Close any still-live rounds at the current level — the PM has reviewed.
    for s in states:
        if s.status in (RoundStatus.OPEN.value, RoundStatus.RESPONDED.value):
            s.status = RoundStatus.CLOSED_BY_PM.value
            s.closed_at = now
            events.append(RoundEvent(
                kind="closed",
                change_request_id=change_request_id,
                partner_id=s.partner_id,
                round_number=s.round_number,
                reason="pm_forced",
            ))

    # No kit change → the partner's open counters are not actioned this round;
    # the original terms stand (the partner may re-counter in the next round).
    open_cps = (
        db.query(CounterProposal)
        .filter(
            CounterProposal.change_request_id == change_request_id,
            CounterProposal.status == CounterProposalStatus.OPEN,
        )
        .all()
    )
    for cp in open_cps:
        cp.status = CounterProposalStatus.REJECTED
        cp.resolution_text = "Reviewed — no kit change made this round."
        cp.resolved_at = now
        cp.resolved_by = actor_user_id

    if current_round < MAX_ROUNDS:
        next_round = current_round + 1
        # Reopen the negotiation for the new round: clear the finalized flag set
        # by the prior round close, so the fresh round shows as live
        # ("negotiating") instead of staying stuck at "round closed" — otherwise
        # the same "No changes needed → continue" button reappears with no
        # visible change.
        cr.negotiation_finalized_at = None
        for pid in partner_ids:
            _state, was_created = create_round_state(change_request_id, pid, next_round, db)
            if was_created:
                events.append(RoundEvent(
                    kind="opened",
                    change_request_id=change_request_id,
                    partner_id=pid,
                    round_number=next_round,
                    reason="pm_advance_no_change",
                ))
        logger.info(
            "Round advanced (no change): change=%s round %d→%d (version unchanged at v%s)",
            change_request_id, current_round, next_round, getattr(cr, "negotiation_version", 1),
        )
    else:
        cr.negotiation_frozen_at = now
        # Symmetric round_closed(reason=frozen) for the round that was just
        # closed — supplements the existing negotiation_frozen send so partners
        # get one uniform stream of round events (cap-reached vs. force-close).
        # `events` already carries the pm_forced close for this same round;
        # tag additional 'frozen' closes so the partner UI can display "round
        # closed — negotiation frozen" alongside "round closed — PM forced".
        for pid in partner_ids:
            events.append(RoundEvent(
                kind="closed",
                change_request_id=change_request_id,
                partner_id=pid,
                round_number=current_round,
                reason="frozen",
            ))
        logger.info(
            "Negotiation frozen (round cap %d reached, no change): change=%s",
            MAX_ROUNDS, change_request_id,
        )

    db.flush()
    return cr, events


def advance_or_freeze_after_close(
    change_request_id: str, db: Session,
) -> tuple[bool, list[RoundEvent]]:
    """Forward progress after a round closed with NO revision needed.

    The automated counterpart to advance_round_no_change's advance/freeze tail,
    used by the silent-acceptance path: when a round closes (silently accepted)
    and produced no outcomes to revise, the negotiation must still move forward —
    open the next round on the same version, or FREEZE once the round cap is
    reached — instead of stalling at "round closed" forever (BUG: a silent
    round-cap close never froze and never notified the partner).

    Unlike advance_round_no_change this does NOT touch counter-proposals (silent
    acceptance already resolved them as accepted). Returns (froze, events); the
    caller is responsible for notify_partners_frozen on a True result and for
    fanning the events out via send_round_events.
    """
    events: list[RoundEvent] = []
    cr = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.id == change_request_id)
        .first()
    )
    if not cr:
        return False, events
    if getattr(cr, "negotiation_frozen_at", None) is not None:
        return True, events  # already frozen

    states = (
        db.query(NegotiationRoundState)
        .filter(NegotiationRoundState.change_request_id == change_request_id)
        .all()
    )
    if not states:
        return False, events
    # A round is still open (e.g. the next round already reopened) → not our call.
    if any(s.status == RoundStatus.OPEN.value for s in states):
        return False, events

    current_round = max(s.round_number for s in states)
    partner_ids = sorted({s.partner_id for s in states})
    now = datetime.now(timezone.utc)

    if current_round < MAX_ROUNDS:
        next_round = current_round + 1
        cr.negotiation_finalized_at = None
        for pid in partner_ids:
            _state, was_created = create_round_state(change_request_id, pid, next_round, db)
            if was_created:
                events.append(RoundEvent(
                    kind="opened",
                    change_request_id=change_request_id,
                    partner_id=pid,
                    round_number=next_round,
                    reason="silent_advance",
                ))
        db.flush()
        logger.info(
            "Round advanced after silent close (no revision): change=%s round %d→%d",
            change_request_id, current_round, next_round,
        )
        return False, events

    cr.negotiation_frozen_at = now
    # round_closed(frozen) for symmetry — the silent-acceptance close already
    # fired a close event upstream in apply_silent_acceptances; layer the
    # 'frozen' reason so the partner sees the cap was reached rather than
    # inferring it from the separate negotiation_frozen notice.
    for pid in partner_ids:
        events.append(RoundEvent(
            kind="closed",
            change_request_id=change_request_id,
            partner_id=pid,
            round_number=current_round,
            reason="frozen",
        ))
    db.flush()
    logger.info(
        "Negotiation frozen after silent close (round cap %d, no revision): change=%s",
        MAX_ROUNDS, change_request_id,
    )
    return True, events


async def notify_partners_frozen(change_request_id: str, db: Session) -> int:
    """Tell each partner in the negotiation that the change has frozen, so the
    partner UI locks the decision/composer.

    A round-based freeze doesn't bump the kit version, so the partner can't
    infer the freeze from a newly-shipped kit — the Authority must signal it. The
    partner stores it on its negotiation_finalized_at (specs-locked) field.
    """
    from app.models.phase_c import A2ATaskType
    from app.services.partner_dispatch import notify_partner

    cr = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.id == change_request_id)
        .first()
    )
    if not cr:
        return 0
    frozen_at = getattr(cr, "negotiation_frozen_at", None) or datetime.now(timezone.utc)

    partner_ids = sorted({
        pid for (pid,) in (
            db.query(NegotiationRoundState.partner_id)
            .filter(NegotiationRoundState.change_request_id == change_request_id)
            .distinct()
            .all()
        )
    })
    sent = 0
    for pid in partner_ids:
        partner = db.query(PartnerAgent).filter(PartnerAgent.id == pid).first()
        if not partner:
            continue
        try:
            await notify_partner(
                partner.id,
                A2ATaskType.NEGOTIATION_FROZEN.value,
                {
                    "change_id": change_request_id,
                    "negotiation_finalized_at": frozen_at.isoformat(),
                    "reason": "round_cap_reached",
                },
                change_id=change_request_id,
                label=partner.name,
                context="negotiation frozen",
            )
            sent += 1
        except Exception:
            logger.exception(
                "notify_partners_frozen send failed: change=%s partner=%s",
                change_request_id, pid,
            )
    logger.info("notify_partners_frozen: change=%s notified=%d", change_request_id, sent)
    return sent


# ── Per-partner round notices (round_opened / round_closed) ───────────────────

async def send_round_opened(
    change_request_id: str,
    partner_id: str,
    round_number: int,
    opened_reason: str,
    db: Session,
) -> None:
    """Notify the partner that a negotiation round has opened on the Authority side.

    Round state lives only on the Authority (`negotiation_round_states`); this notice
    gives the partner a first-class signal — round number, deadline, kit
    version, and WHY the round was opened (initial ack / PM force-advance /
    silent advance / new version ship) — so the partner UI can render "round
    N of MAX, deadline T" without inspecting embedded CP payload fields.
    """
    from app.models.phase_c import A2ATaskType
    from app.services.partner_dispatch import notify_partner

    partner = db.query(PartnerAgent).filter(PartnerAgent.id == partner_id).first()
    if not partner:
        return
    state = (
        db.query(NegotiationRoundState)
        .filter(
            NegotiationRoundState.change_request_id == change_request_id,
            NegotiationRoundState.partner_id == partner_id,
            NegotiationRoundState.round_number == round_number,
        )
        .first()
    )
    cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_request_id).first()
    payload = {
        "change_id":      change_request_id,
        "round_number":   round_number,
        "max_rounds":     MAX_ROUNDS,
        "deadline_at":    state.deadline_at.isoformat() if state and state.deadline_at else None,
        "kit_version":    getattr(cr, "negotiation_version", 1) if cr else None,
        "opened_reason":  opened_reason,
    }
    try:
        await notify_partner(
            partner.id, A2ATaskType.ROUND_OPENED.value, payload,
            change_id=change_request_id, label=partner.name,
            context="round opened",
        )
    except Exception:
        logger.exception(
            "send_round_opened failed: change=%s partner=%s round=%d",
            change_request_id, partner_id, round_number,
        )


async def send_round_closed(
    change_request_id: str,
    partner_id: str,
    round_number: int,
    close_reason: str,
    db: Session,
) -> None:
    """Notify the partner that a negotiation round has closed on the Authority side.

    Minimal payload: change_id, round_number, closed_at, close_reason. When a
    next round opens, the partner gets a separate round_opened notice with
    its number and deadline — no need to duplicate that here. When
    close_reason == "frozen" the negotiation is terminal (the existing
    negotiation_frozen notice locks the composer alongside this).
    Called for every close reason: pm_forced, silent_acceptance,
    superseded_by_version, and frozen.
    """
    from app.models.phase_c import A2ATaskType
    from app.services.partner_dispatch import notify_partner

    partner = db.query(PartnerAgent).filter(PartnerAgent.id == partner_id).first()
    if not partner:
        return
    state = (
        db.query(NegotiationRoundState)
        .filter(
            NegotiationRoundState.change_request_id == change_request_id,
            NegotiationRoundState.partner_id == partner_id,
            NegotiationRoundState.round_number == round_number,
        )
        .first()
    )

    payload = {
        "change_id":     change_request_id,
        "round_number":  round_number,
        "closed_at":     state.closed_at.isoformat() if state and state.closed_at else datetime.now(timezone.utc).isoformat(),
        "close_reason":  close_reason,
    }
    try:
        await notify_partner(
            partner.id, A2ATaskType.ROUND_CLOSED.value, payload,
            change_id=change_request_id, label=partner.name,
            context="round closed",
        )
    except Exception:
        logger.exception(
            "send_round_closed failed: change=%s partner=%s round=%d",
            change_request_id, partner_id, round_number,
        )


async def send_round_events(events: list[RoundEvent], db: Session) -> None:
    """Fan a list of RoundEvent (from advance_* / apply_silent_acceptances)
    out to the corresponding partners. Closes fire before opens so partner
    logs land in causal order — first "round 1 closed", then "round 2 opened"."""
    if not events:
        return
    for e in sorted(events, key=lambda ev: (0 if ev.kind == "closed" else 1, ev.round_number)):
        if e.kind == "opened":
            await send_round_opened(
                e.change_request_id, e.partner_id, e.round_number, e.reason, db,
            )
        else:
            await send_round_closed(
                e.change_request_id, e.partner_id, e.round_number, e.reason, db,
            )


def apply_silent_acceptances(db: Session) -> tuple[list[str], list[RoundEvent]]:
    """Sweep for overdue open round states and apply silent acceptance.

    Returns (affected, events):
      - affected: legacy list of "change_id:partner_id" strings (kept for the
        celery task metadata and the /negotiation/rounds/sweep endpoint that
        already return this shape).
      - events: RoundEvent list — one round_closed(silent_acceptance) per
        state that was flipped. Async caller fans them out.

    Called by a background task or the /negotiation/rounds/sweep endpoint.
    """
    now = datetime.now(timezone.utc)
    overdue: list[NegotiationRoundState] = (
        db.query(NegotiationRoundState)
        .filter(
            NegotiationRoundState.status == RoundStatus.OPEN.value,
            NegotiationRoundState.deadline_at < now,
        )
        .all()
    )

    affected: list[str] = []
    events: list[RoundEvent] = []
    for state in overdue:
        # Create a synthetic "silent acceptance" counter-decision record
        # so the negotiation timeline shows that the Authority deemed no response = acceptance.
        state.status = RoundStatus.SILENTLY_ACCEPTED.value
        state.closed_at = now

        # Auto-close any open CPs for this (change, partner) as accepted
        open_cps = (
            db.query(CounterProposal)
            .filter(
                CounterProposal.change_request_id == state.change_request_id,
                CounterProposal.partner_id == state.partner_id,
                CounterProposal.status == CounterProposalStatus.OPEN,
            )
            .all()
        )
        for cp in open_cps:
            cp.status = CounterProposalStatus.ACCEPTED
            cp.resolution_text = (
                "Silent acceptance: partner did not respond within the "
                f"{_round_window_label()} round {state.round_number} window."
            )
            cp.resolved_at = now
            state.silent_acceptance_cp_id = cp.id  # tag last one for reference

        affected.append(f"{state.change_request_id}:{state.partner_id}")
        events.append(RoundEvent(
            kind="closed",
            change_request_id=state.change_request_id,
            partner_id=state.partner_id,
            round_number=state.round_number,
            reason="silent_acceptance",
        ))
        logger.info(
            "SilentAcceptance: change=%s partner=%s round=%d — closed %d CP(s)",
            state.change_request_id, state.partner_id, state.round_number, len(open_cps),
        )
        # authority-side notification so PM/admin see the silent close in real-time —
        # otherwise this whole path is invisible from the Authority UI (the round
        # just quietly transitions in the DB while nobody is watching).
        # Best-effort: any failure here must NOT break the sweep.
        try:
            from app.services.notifications import notify_round_silently_closed
            partner = (
                db.query(PartnerAgent)
                .filter(PartnerAgent.id == state.partner_id)
                .first()
            )
            notify_round_silently_closed(
                db,
                change_id=state.change_request_id,
                partner_name=getattr(partner, "name", None) or state.partner_id,
                round_number=state.round_number,
                cap_reached=(state.round_number >= MAX_ROUNDS),
            )
        except Exception:
            logger.exception(
                "notify_round_silently_closed failed for change=%s partner=%s round=%d",
                state.change_request_id, state.partner_id, state.round_number,
            )

    if overdue:
        db.flush()
    return affected, events


# ── Clustering helpers ────────────────────────────────────────────────────────

def _topic_slug(justification: str, max_words: int = 4) -> str:
    """Derive a stable short slug from the first N significant words."""
    words = re.sub(r"[^a-z0-9 ]", "", justification.lower()).split()
    # skip stop words
    stop = {"the", "a", "an", "we", "our", "is", "are", "to", "for", "of", "in", "and"}
    sig = [w for w in words if w not in stop]
    return "_".join(sig[:max_words]) or "general"


def _cluster_key(category: str, justification: str) -> str:
    return f"{category}::{_topic_slug(justification)}"


def _cluster_samples(clusters: list[NegotiationCluster], db: Session) -> dict[str, str]:
    """Map cluster_id → one representative member justification.

    Gives the LLM router a concrete example of what each existing cluster is
    about (its earliest member's justification), so it can decide membership on
    content rather than the short topic label alone.
    """
    cluster_ids = [c.id for c in clusters]
    if not cluster_ids:
        return {}
    members: list[NegotiationClusterMember] = (
        db.query(NegotiationClusterMember)
        .filter(NegotiationClusterMember.cluster_id.in_(cluster_ids))
        .order_by(NegotiationClusterMember.added_at)
        .all()
    )
    first_cp_by_cluster: dict[str, str] = {}
    for m in members:
        first_cp_by_cluster.setdefault(m.cluster_id, m.counter_proposal_id)
    cp_ids = list(first_cp_by_cluster.values())
    cps = (
        db.query(CounterProposal).filter(CounterProposal.id.in_(cp_ids)).all()
        if cp_ids else []
    )
    cp_just = {cp.id: (cp.justification or "") for cp in cps}
    return {cid: cp_just.get(cpid, "") for cid, cpid in first_cp_by_cluster.items()}


async def _route_to_cluster(
    cp: CounterProposal,
    category: str,
    db: Session,
) -> NegotiationCluster:
    """Pick the cluster a CP belongs to, creating a new one when needed.

    With existing clusters on this change, an LLM router decides whether the CP
    joins one of them (matching on the underlying ask, not wording) or starts a
    new topic. The first CP on a change always opens a new cluster — no LLM call.
    """
    from app.agents.cluster_router import route_counter_proposal

    existing: list[NegotiationCluster] = (
        db.query(NegotiationCluster)
        .filter(NegotiationCluster.change_request_id == cp.change_request_id)
        .order_by(NegotiationCluster.created_at)
        .all()
    )

    proposed_summary = ""
    if existing:
        samples = _cluster_samples(existing, db)
        choices = [
            {
                "index": i + 1,
                "category": c.category,
                "topic_summary": c.topic_summary or "",
                "sample": samples.get(c.id, ""),
            }
            for i, c in enumerate(existing)
        ]
        decision = await route_counter_proposal(
            category=category,
            justification=cp.justification or "",
            payload=cp.payload or {},
            existing_clusters=choices,
        )
        proposed_summary = decision.get("topic_summary") or ""
        idx = decision.get("cluster_index")
        if decision.get("decision") == "match" and isinstance(idx, int) and 1 <= idx <= len(existing):
            matched = existing[idx - 1]
            logger.info("Cluster router: cp=%s → existing cluster %s (%s)", cp.id, matched.id, matched.topic_summary)
            return matched
        logger.info("Cluster router: cp=%s → new cluster (%s)", cp.id, proposed_summary or "unlabelled")

    # Prefer the router's text-derived label. Fallback (router returned nothing,
    # e.g. on an LLM error) is built from the justification text first, with the
    # category only as a last resort — keeping cluster labels text-primary too.
    topic_summary = (
        proposed_summary
        or _topic_slug(cp.justification or "", 8).replace("_", " ").strip().title()
        or category.replace("_", " ").title()
    )
    cluster = NegotiationCluster(
        id=generate_uuid(),
        change_request_id=cp.change_request_id,
        cluster_key=_cluster_key(category, cp.justification or ""),
        category=category,
        topic_summary=topic_summary[:500],
        partner_count=0,
    )
    db.add(cluster)
    db.flush()
    return cluster


async def classify_and_cluster_cp(
    cp: CounterProposal,
    db: Session,
    brd_text: str = "",
) -> None:
    """Run BRD classification + real-time clustering for a new counter-proposal.

    Must be called AFTER the CP is flushed to DB (so its `id` is set).
    Modifies cp.brd_classification, cp.auto_disposition, cp.cluster_id in place.
    Also updates or creates the NegotiationCluster row and adds a member.
    """
    from app.agents.negotiation_classifier import classify_counter_proposal

    # ── 1. BRD classification ─────────────────────────────────────────────
    classification, disposition, detail = await classify_counter_proposal(cp, db, brd_text)
    cp.brd_classification = classification
    cp.auto_disposition = disposition
    db.flush()

    # ── 1b. Auto-resolution ───────────────────────────────────────────────
    # When the classifier reached a confident disposition we settle the CP
    # immediately and tell the partner — mandatory violation → REJECT,
    # optional-in-tolerance → ACCEPT. Escalated / uncategorized fall through
    # to clustering so the PM decides. Auto-resolved CPs still go through
    # clustering so the PM can see patterns across partners (e.g. 3 banks
    # raised the same rejected ask) even though no individual decision is needed.
    if disposition in (AutoDisposition.AUTO_REJECTED.value, AutoDisposition.AUTO_ACCEPTED.value):
        await _auto_resolve_cp(cp, disposition, db, detail)

    # ── 2. Clustering (LLM router) ────────────────────────────────────────
    # An LLM decides whether this CP joins an existing cluster (same underlying
    # ask, however worded) or starts a new topic — replacing the old
    # first-4-words `cluster_key` exact-match, which split paraphrases apart and
    # trusted the partner-supplied category. The deterministic `cluster_key` is
    # still generated and stored as a stable label / debugging aid.
    category = cp.request_category or "general"
    cluster = await _route_to_cluster(cp, category, db)

    # Avoid duplicate membership (idempotent)
    already_member = (
        db.query(NegotiationClusterMember)
        .filter(NegotiationClusterMember.counter_proposal_id == cp.id)
        .first()
    )
    if not already_member:
        member = NegotiationClusterMember(
            id=generate_uuid(),
            cluster_id=cluster.id,
            counter_proposal_id=cp.id,
            partner_id=cp.partner_id,
        )
        db.add(member)
        cp.cluster_id = cluster.id
        db.flush()
        # partner_count = DISTINCT partners, not member rows. One partner can
        # file several counters that slug to the same cluster_key; a naive +1
        # per member over-counts the partners affected (and conflict/threshold
        # logic reads this as a partner count). Recompute from the members.
        cluster.partner_count = (
            db.query(NegotiationClusterMember.partner_id)
            .filter(NegotiationClusterMember.cluster_id == cluster.id)
            .distinct()
            .count()
        )
        db.flush()

    # ── 3. AI summary for any non-empty cluster ───────────────────────────
    # Generate as soon as a cluster has at least one counter-proposal so the
    # PM sees an AI recommendation even on single- or two-partner clusters
    # (was gated to ≥3, which hid the suggestion for most real clusters).
    if (cluster.partner_count or 0) >= 1:
        await _refresh_cluster_ai_summary(cluster, db)

    # ── 4. Conflict detection (simple: two clusters in same category contradict) ──
    _detect_conflicts(cluster, db)


async def _auto_resolve_cp(cp: CounterProposal, disposition: str, db: Session,
                           detail: dict | None = None) -> None:
    """Settle a counter-proposal per its auto-disposition and notify the partner.

    AUTO_ACCEPTED → CP accepted + COUNTER_DECISION ACCEPT to the partner.
    AUTO_REJECTED → CP rejected + COUNTER_DECISION REJECT to the partner.
    The partner's handler folds the decision into its counter_decisions log
    and flips the matching outgoing query's status, exactly as it does for a
    PM-issued decision — the partner can't tell it was automated beyond the
    resolution text.

    `detail` ({"requirement", "reason"}) names the violated requirement so the rejection is
    actionable. Without it the partner was told only that "this requirement is mandatory",
    with no indication of WHICH one — so their next counter would likely repeat the error.
    """
    from app.models.phase_c import A2ATaskType
    from app.services.partner_dispatch import notify_partner

    accepted = disposition == AutoDisposition.AUTO_ACCEPTED.value
    decision = "ACCEPT" if accepted else "REJECT"
    cp.status = CounterProposalStatus.ACCEPTED if accepted else CounterProposalStatus.REJECTED
    if accepted:
        resolution_text = ("Auto-accepted: your proposed change falls within the Authority's "
                           "configured tolerance for this requirement.")
    else:
        _req = (detail or {}).get("requirement")
        _why = (detail or {}).get("reason")
        resolution_text = (
            "Auto-rejected: this conflicts with "
            + (f"'{_req}', which is marked mandatory (non-negotiable) in the Authority's BRD "
               "configuration." if _req else
               "a requirement marked mandatory (non-negotiable) in the Authority's BRD configuration.")
            + (f" Assessment: {_why}" if _why else "")
        )
        # Surface the auto-rejection to the Authority operators too — previously only the partner
        # was told, so the PM had no signal that a bank had been turned down automatically.
        try:
            from app.services.notifications import notify_mandatory_rejection
            _p = db.query(PartnerAgent).filter(PartnerAgent.id == cp.partner_id).first()
            notify_mandatory_rejection(
                db, change_id=cp.change_request_id,
                partner_name=getattr(_p, "name", None) or str(cp.partner_id),
                cp_id=cp.id, requirement_label=_req, reason=_why,
            )
        except Exception:  # noqa: BLE001 — alerting must never break resolution
            logger.exception("mandatory-rejection notification failed for cp=%s", cp.id)
    cp.resolution_text = resolution_text
    cp.resolved_at = datetime.now(timezone.utc)

    try:
        from app.services.negotiation_service import record_linked_message
        record_linked_message(
            db,
            change_request_id=cp.change_request_id,
            partner_id=cp.partner_id,
            role="po_approved",
            content=resolution_text,
            event_kind="resolution",
            counter_proposal_id=cp.id,
        )
    except Exception:
        logger.exception("Auto-resolve dual-write NM failed for cp=%s", cp.id)

    db.commit()

    partner = db.query(PartnerAgent).filter(PartnerAgent.id == cp.partner_id).first()
    if partner:
        try:
            await notify_partner(
                partner.id,
                # Protocol v1: The Authority's decision on a partner counter is now a
                # first-class counter_decision task type (§6.7), not smuggled
                # inside clarification_response via message_kind.
                A2ATaskType.COUNTER_DECISION.value,
                {
                    "change_id":              cp.change_request_id,
                    "decision":               decision,
                    "in_response_to":         cp.counter_proposal_id or cp.id,
                    "negotiation_round":      cp.negotiation_round or 1,
                    "resolution_text":        resolution_text,
                    "original_justification": cp.justification or "",
                },
                change_id=cp.change_request_id,
                label=partner.name,
                context="counter decision (auto-resolve)",
            )
        except Exception:
            logger.exception("Auto-resolve partner notify failed for cp=%s", cp.id)

    logger.info(
        "Auto-resolved cp=%s disposition=%s decision=%s change=%s partner=%s",
        cp.id, disposition, decision, cp.change_request_id, cp.partner_id,
    )


async def _refresh_cluster_ai_summary(cluster: NegotiationCluster, db: Session) -> None:
    """Regenerate AI summary + recommendation for a cluster."""
    from app.agents.cluster_analyzer import analyze_cluster

    members: list[NegotiationClusterMember] = (
        db.query(NegotiationClusterMember)
        .filter(NegotiationClusterMember.cluster_id == cluster.id)
        .all()
    )
    cp_ids = [m.counter_proposal_id for m in members]
    cps: list[CounterProposal] = (
        db.query(CounterProposal).filter(CounterProposal.id.in_(cp_ids)).all()
    )
    partner_ids = [cp.partner_id for cp in cps]
    partners: list[PartnerAgent] = (
        db.query(PartnerAgent).filter(PartnerAgent.id.in_(partner_ids)).all()
    )
    partner_name_map = {p.id: p.name for p in partners}

    justifications = [cp.justification or "" for cp in cps]
    partner_names = [partner_name_map.get(cp.partner_id, "Unknown") for cp in cps]

    try:
        result = await analyze_cluster(
            category=cluster.category,
            topic_summary=cluster.topic_summary or "",
            justifications=justifications,
            partner_names=partner_names,
        )
        cluster.ai_summary = result["summary"]
        cluster.ai_recommendation = result["recommendation"]
        cluster.confidence_score = result["confidence"]
        cluster.updated_at = datetime.now(timezone.utc)
        db.flush()
        logger.info("Cluster %s: AI summary refreshed (n=%d)", cluster.id, len(cps))
    except Exception as exc:
        logger.warning("Cluster AI summary failed for %s: %s", cluster.id, exc)


def _detect_conflicts(new_cluster: NegotiationCluster, db: Session) -> None:
    """Simple conflict detection: flag two clusters in the same category
    if one has ai_recommendation='accept' and another 'reject'.
    """
    same_cat_clusters: list[NegotiationCluster] = (
        db.query(NegotiationCluster)
        .filter(
            NegotiationCluster.change_request_id == new_cluster.change_request_id,
            NegotiationCluster.category == new_cluster.category,
            NegotiationCluster.id != new_cluster.id,
        )
        .all()
    )
    my_rec = new_cluster.ai_recommendation
    for other in same_cat_clusters:
        if not my_rec or not other.ai_recommendation:
            continue
        if {my_rec, other.ai_recommendation} == {"accept", "reject"}:
            new_cluster.conflict_with_cluster_id = other.id
            other.conflict_with_cluster_id = new_cluster.id
            db.flush()
            logger.info("Conflict detected between clusters %s and %s", new_cluster.id, other.id)


# ── Governance helpers ────────────────────────────────────────────────────────

async def classify_and_cluster_background(cp_id: str, brd_text: str = "") -> None:
    """BackgroundTask wrapper: opens its own DB session and runs classify_and_cluster_cp.

    Mirrors the pattern used by auto_draft_background in negotiation_service.py.
    """
    from app.core.database import SessionLocal
    db = SessionLocal()
    try:
        cp = db.query(CounterProposal).filter(CounterProposal.id == cp_id).first()
        if not cp:
            logger.warning("classify_and_cluster_background: CP %s not found", cp_id)
            return
        # Ground the AI tolerance check on the actual BRD when the caller
        # didn't pass text (the A2A executor doesn't). Latest version wins.
        if not brd_text:
            from app.models.brd import BRD
            brd = (
                db.query(BRD)
                .filter(BRD.change_request_id == cp.change_request_id)
                .order_by(BRD.version.desc())
                .first()
            )
            brd_text = (brd.content or "") if brd else ""
        await classify_and_cluster_cp(cp, db, brd_text=brd_text)
        db.commit()
    except Exception:
        logger.exception("classify_and_cluster_background failed for cp=%s", cp_id)
        db.rollback()
    finally:
        db.close()


def _detach_from_cluster(cp: CounterProposal, db: Session) -> None:
    """Remove a CP from its pending-PM cluster — it no longer needs a decision.

    Deletes the membership row, decrements partner_count, and clears
    cp.cluster_id. A cluster left with no members is deleted so the PM's view
    doesn't show a ghost. Called when a re-classification (after a PM toggles a
    requirement) auto-resolves a counter that was previously escalated into a
    cluster. The cluster's ai_summary is left as-is — it refreshes on the next
    member change or manual refresh.
    """
    members = (
        db.query(NegotiationClusterMember)
        .filter(NegotiationClusterMember.counter_proposal_id == cp.id)
        .all()
    )
    for m in members:
        cluster = (
            db.query(NegotiationCluster)
            .filter(NegotiationCluster.id == m.cluster_id)
            .first()
        )
        db.delete(m)
        db.flush()
        if cluster:
            cluster.partner_count = max(0, (cluster.partner_count or 1) - 1)
            remaining = (
                db.query(NegotiationClusterMember)
                .filter(NegotiationClusterMember.cluster_id == cluster.id)
                .count()
            )
            if remaining == 0:
                db.delete(cluster)
            db.flush()
    cp.cluster_id = None
    db.flush()


async def reclassify_open_counter_proposals(change_request_id: str, brd_text: str = "") -> int:
    """Re-run BRD classification on every OPEN partner counter-proposal.

    Called (as a background task) when the PM toggles a requirement — mandatory
    flag, category, or tolerance — so the auto-accept/reject disposition
    reflects the PM's edited configuration rather than the config that was live
    when the counter first arrived.

    Scope is deliberately narrow:
      - Only OPEN, partner-originated counters are touched. Already-resolved
        ones (auto-accepted/rejected, PM-decided, silently accepted) are left
        alone so partners aren't re-notified about a decision already sent.
      - A counter that now resolves to auto_accept/auto_reject is settled and
        the partner notified (exactly as on first arrival) and detached from
        its cluster. One that still escalates keeps its existing cluster — its
        topic didn't change, only its disposition was re-checked.

    Returns the number of counters that flipped to an auto-resolution.
    """
    from app.agents.negotiation_classifier import classify_counter_proposal
    from app.core.database import SessionLocal
    from app.models.brd import BRD

    db = SessionLocal()
    flipped = 0
    try:
        if not brd_text:
            brd = (
                db.query(BRD)
                .filter(BRD.change_request_id == change_request_id)
                .order_by(BRD.version.desc())
                .first()
            )
            brd_text = (brd.content or "") if brd else ""

        open_cps = (
            db.query(CounterProposal)
            .filter(
                CounterProposal.change_request_id == change_request_id,
                CounterProposal.status == CounterProposalStatus.OPEN,
                CounterProposal.originator == "partner",
            )
            .all()
        )
        for cp in open_cps:
            classification, disposition, detail = await classify_counter_proposal(cp, db, brd_text)
            cp.brd_classification = classification
            cp.auto_disposition = disposition
            db.flush()
            if disposition in (AutoDisposition.AUTO_REJECTED.value, AutoDisposition.AUTO_ACCEPTED.value):
                # Pull it out of the pending cluster, then settle + notify.
                _detach_from_cluster(cp, db)
                await _auto_resolve_cp(cp, disposition, db, detail)  # commits internally
                flipped += 1
            else:
                db.commit()
        logger.info(
            "Reclassify: change=%s re-evaluated %d open CP(s); %d auto-resolved",
            change_request_id, len(open_cps), flipped,
        )
    except Exception:
        logger.exception("reclassify_open_counter_proposals failed for change=%s", change_request_id)
        db.rollback()
    finally:
        db.close()
    return flipped


def finalize_negotiation(
    change_request_id: str,
    actor_user_id: str,
    db: Session,
) -> tuple[ChangeRequest, list[RoundEvent]]:
    """Lock the negotiation for a change.

    - Sets negotiation_finalized_at on the ChangeRequest.
    - Auto-closes all remaining open CounterProposals as 'auto_closed_finalized'.

    Returns (change, round_events); the async caller fans the events out via
    send_round_events after commit so partners see round_closed(pm_forced) for
    any live round the finalize retired.
    """
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_request_id).first()
    if not change:
        raise ValueError(f"Change {change_request_id} not found")

    now = datetime.now(timezone.utc)
    change.negotiation_finalized_at = now

    open_cps = (
        db.query(CounterProposal)
        .filter(
            CounterProposal.change_request_id == change_request_id,
            CounterProposal.status == CounterProposalStatus.OPEN,
        )
        .all()
    )
    # Finalizing used to blanket-ACCEPT every open counter-proposal. That silently undid
    # the BRD mandatory guard: a counter already classified as MANDATORY_VIOLATION (e.g.
    # classified before it could be auto-resolved, or flagged by a later reclassify after
    # requirements were added) was converted straight to ACCEPTED — a bank deviation from
    # a non-negotiable requirement slipping in through the back door.
    #
    # A violation is REJECTED on finalize, never accepted. This check is deterministic
    # (reads the persisted classification), so it holds in this sync path with no LLM call.
    _violating = 0
    _unchecked = 0
    for cp in open_cps:
        if cp.brd_classification == BRDClassification.MANDATORY_VIOLATION.value:
            cp.status = CounterProposalStatus.REJECTED
            cp.resolution_text = (
                "Rejected on finalize: this conflicts with a BRD requirement marked "
                "mandatory (non-negotiable). Finalizing the negotiation cannot waive a "
                "mandatory requirement."
            )
            _violating += 1
        else:
            if cp.brd_classification in (None, "", BRDClassification.UNCATEGORIZED.value):
                _unchecked += 1
            cp.status = CounterProposalStatus.ACCEPTED
            cp.resolution_text = "Negotiation finalized — specs locked by the Authority PM."
        cp.resolved_at = now
        cp.resolved_by = actor_user_id

    if _violating:
        logger.warning(
            "finalize_negotiation: change=%s REJECTED %d counter-proposal(s) that violate "
            "mandatory BRD requirements (they were not accepted by the finalize).",
            change_request_id, _violating,
        )
    if _unchecked:
        # Accepting counters that never passed a BRD check is a real risk, not a detail —
        # make it visible rather than letting the PM believe everything was validated.
        logger.warning(
            "finalize_negotiation: change=%s accepted %d counter-proposal(s) with NO BRD "
            "classification — they were never checked against mandatory requirements.",
            change_request_id, _unchecked,
        )
        try:
            from app.services.notifications import notify
            from app.models.notification import NotificationType
            notify(
                db,
                title=f"Negotiation finalized with {_unchecked} unchecked counter-proposal(s)",
                message=(
                    f"Change {change_request_id} was finalized and {_unchecked} open "
                    "counter-proposal(s) were accepted without ever being classified "
                    "against BRD mandatory requirements (no brd_requirements configured, "
                    "or classification never ran). Review them manually."
                ),
                ntype=NotificationType.MANDATORY_REJECTION,
                related_id=change_request_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("finalize unchecked-CP notification failed")

    # Finalize = close this round now (the PM ends it early instead of waiting
    # for the deadline). Close any OPEN round states so the change moves to
    # "round closed → prepare next version" (open_rounds → 0). This is NOT the
    # terminal freeze — that's negotiation_frozen_at, set after v3.
    open_round_states = (
        db.query(NegotiationRoundState)
        .filter(
            NegotiationRoundState.change_request_id == change_request_id,
            NegotiationRoundState.status == RoundStatus.OPEN.value,
        )
        .all()
    )
    events: list[RoundEvent] = []
    for st in open_round_states:
        st.status = RoundStatus.CLOSED_BY_PM.value
        st.closed_at = now
        events.append(RoundEvent(
            kind="closed",
            change_request_id=change_request_id,
            partner_id=st.partner_id,
            round_number=st.round_number,
            reason="pm_forced",
        ))

    db.flush()
    logger.info(
        "Negotiation finalized for change=%s by user=%s — closed %d CPs, %d round(s)",
        change_request_id, actor_user_id, len(open_cps), len(open_round_states),
    )
    return change, events


def collect_round_outcomes(change_request_id: str, db: Session) -> list[dict]:
    """Consolidate the closing round's outcomes across all partners (Slice 4).

    Pulls two signals for a change:
      1. Cluster decisions — cross-partner counter-proposal groups with a PM
         decision (or an AI recommendation as fallback).
      2. Doc-impact decisions — from each partner's latest resolver
         recommendation (the `doc_impact` block written in Step 5).

    Returns a list of {topic, decision, rationale, documents} dicts. This is
    what the PM regenerates the kit from, and what feeds the version change
    summary. Read-only — no writes.
    """
    import json as _json

    from app.models.resolver_recommendation import ResolverRecommendation

    outcomes: list[dict] = []

    # Scope to the CURRENT round window only. Outcomes from EARLIER rounds were
    # already addressed by an earlier kit version, so they must not be
    # re-surfaced when preparing the next one. The current round = the LATEST
    # round that actually opened (highest round_number present); round_start is
    # its earliest start across partners. Using the latest-opened round (rather
    # than round_number == version) is robust when the version has been bumped
    # but that version's round hasn't opened yet (generated-but-not-shipped).
    _all_rounds = (
        db.query(NegotiationRoundState)
        .filter(NegotiationRoundState.change_request_id == change_request_id)
        .all()
    )
    round_start = None
    if _all_rounds:
        _max_rn = max((r.round_number or 0) for r in _all_rounds)
        round_start = min(
            (r.started_at for r in _all_rounds if (r.round_number or 0) == _max_rn and r.started_at),
            default=None,
        )

    # 1. Clusters raised in THIS round (cross-partner counter groups). We surface
    # the PM decision when present, else the AI recommendation — so a still-
    # pending cluster the round raised is shown to the planner as context (it
    # decides whether a doc change follows; a 'reject' won't drive one).
    clusters = (
        db.query(NegotiationCluster)
        .filter(NegotiationCluster.change_request_id == change_request_id)
        .all()
    )
    for c in clusters:
        if round_start and getattr(c, "created_at", None) and c.created_at < round_start:
            continue  # prior round — already handled by an earlier version
        decision = (getattr(c, "pm_decision", None) or getattr(c, "ai_recommendation", None) or "pending")
        rationale = (getattr(c, "pm_decision_text", None) or getattr(c, "ai_summary", None) or "")
        outcomes.append({
            "topic": c.topic_summary or c.category or "",
            "decision": str(decision),
            "rationale": rationale,
            "documents": [],
        })

    # 2. Doc-impact from THIS round's queries. The only gate is the round window
    # (so prior rounds — already shipped in an earlier version — don't reappear).
    # A current-round query that flags a doc gap is a candidate change the PM
    # reviews/edits in the plan, regardless of whether the answer was inline or
    # escalated.
    recs = (
        db.query(ResolverRecommendation)
        .filter(ResolverRecommendation.change_request_id == change_request_id)
        .order_by(ResolverRecommendation.created_at.desc())
        .all()
    )
    seen_msgs: set[str] = set()
    for r in recs:
        if not r.a2a_message_id or r.a2a_message_id in seen_msgs:
            continue
        seen_msgs.add(r.a2a_message_id)
        if round_start and getattr(r, "created_at", None) and r.created_at < round_start:
            continue  # prior round — already in an earlier version
        try:
            content = _json.loads(r.content)
        except (ValueError, TypeError):
            continue
        impact = content.get("doc_impact") if isinstance(content, dict) else None
        if impact and impact.get("needs_doc_change") and impact.get("documents"):
            outcomes.append({
                "topic": (content.get("action_summary") or "partner query"),
                "decision": "doc_update",
                "rationale": impact.get("rationale") or "",
                "documents": impact.get("documents") or [],
            })
    return outcomes


def create_new_version(
    change_request_id: str,
    db: Session,
) -> int:
    """Increment the negotiation_version on the ChangeRequest.

    Returns the new version number. Partners will see a "New version available"
    banner when their negotiation_version_accepted is reset to False.
    """
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_request_id).first()
    if not change:
        raise ValueError(f"Change {change_request_id} not found")

    current_version = getattr(change, "negotiation_version", 1) or 1
    new_version = current_version + 1
    change.negotiation_version = new_version
    # Also reset finalized_at so negotiation reopens for v2
    change.negotiation_finalized_at = None
    db.flush()
    logger.info("New version created: change=%s v%d → v%d", change_request_id, current_version, new_version)
    return new_version


def close_open_rounds_for_version_ship(
    change_request_id: str, db: Session,
) -> list[RoundEvent]:
    """Close any OPEN / RESPONDED rounds because a new kit version is shipping.

    Called by the new-version-and-ship path just before create_new_version so
    that the round is retired with reason=superseded_by_version. Without this,
    partners with an open round would see the new kit arrive with no notice
    that the prior round was abandoned. Returns the close events; the caller
    fans them out via send_round_events after commit.

    Idempotent: rounds already CLOSED_BY_PM / SILENTLY_ACCEPTED are left alone
    (their close events fired via advance_* / apply_silent_acceptances).
    """
    events: list[RoundEvent] = []
    now = datetime.now(timezone.utc)
    live_states = (
        db.query(NegotiationRoundState)
        .filter(
            NegotiationRoundState.change_request_id == change_request_id,
            NegotiationRoundState.status.in_(
                (RoundStatus.OPEN.value, RoundStatus.RESPONDED.value)
            ),
        )
        .all()
    )
    for s in live_states:
        s.status = RoundStatus.CLOSED_BY_PM.value
        s.closed_at = now
        events.append(RoundEvent(
            kind="closed",
            change_request_id=change_request_id,
            partner_id=s.partner_id,
            round_number=s.round_number,
            reason="superseded_by_version",
        ))
    if live_states:
        db.flush()
        logger.info(
            "close_open_rounds_for_version_ship: change=%s closed %d live round(s)",
            change_request_id, len(live_states),
        )
    return events
