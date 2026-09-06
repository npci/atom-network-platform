# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Kit revision runner — regenerate the next kit version from the PM's plan.

Background orchestration for the round-close → review → generate flow:
  1. For each doc the (PM-edited) revision plan flags, regenerate its content via
     the product-kit agent, using the plan's change instruction + the current
     content as the revision input (full feature context is prepended by the
     agent).
  2. Docs not in the plan are carried forward unchanged.
  3. Bump negotiation_version and write the new-version ProductKitDocument rows.
  4. Mark the plan `generated`. Nothing is shipped — the PM ships from Phase C.

Reuses the simple `stream_product_kit_doc` agent (same path the "Revise" button
uses). Binary renders (docx/pptx/xlsx) are left to re-render from the markdown,
matching the existing revision convention.
"""
import logging

from app.core.database import SessionLocal

logger = logging.getLogger(__name__)


def _gather_context(change_id: str, db) -> dict:
    from app.models.brd import BRD
    from app.models.canvas import ProductCanvas
    from app.models.change_request import ChangeRequest
    from app.models.research import ResearchOutput
    from app.models.tech_spec import TechSpec

    cr = db.get(ChangeRequest, change_id)
    research = (
        db.query(ResearchOutput).filter(ResearchOutput.change_request_id == change_id)
        .order_by(ResearchOutput.version.desc()).first()
    )
    canvas = (
        db.query(ProductCanvas).filter(ProductCanvas.change_request_id == change_id)
        .order_by(ProductCanvas.version.desc()).first()
    )
    brd = (
        db.query(BRD).filter(BRD.change_request_id == change_id)
        .order_by(BRD.version.desc()).first()
    )
    ts = (
        db.query(TechSpec).filter(TechSpec.change_request_id == change_id)
        .order_by(TechSpec.version.desc()).first()
    )
    # Accuracy S6: ground kit revisions in the REAL approved schemas + binding ledger.
    from app.models.xsd import XSD
    xsd = (db.query(XSD).filter(XSD.change_request_id == change_id)
           .order_by(XSD.version.desc()).first())
    decisions = ""
    try:
        from app.services.decision_ledger import build_decisions_block
        decisions = build_decisions_block(change_id, db)
    except Exception:  # noqa: BLE001
        decisions = ""
    return {
        "enriched_prompt": (cr.enhanced_prompt or cr.initial_prompt) if cr else "",
        "research_report": research.combined_report if research else "No research report available.",
        "canvas_content": canvas.content if canvas else "No canvas available.",
        "brd_content": brd.content if brd else "No BRD available.",
        "tech_spec_content": ts.content if ts else "No tech spec available.",
        "xsd_content": (xsd.content or "") if xsd else "",
        "decisions_block": decisions,
    }


async def _regenerate_doc(doc_type: str, instruction: str, existing_content: str, ctx: dict) -> str:
    """Regenerate one kit doc's content. Empty conversation_history so the agent
    prepends the full feature context; the instruction + current content go in
    the user message as a revision request."""
    from app.agents.product_kit_agent import stream_product_kit_doc

    import asyncio

    new_msg = (
        f"Revise the existing {doc_type} per this instruction:\n{instruction}\n\n"
        f"--- CURRENT {doc_type} (revise and emit the COMPLETE updated document) ---\n"
        f"{existing_content or '(no existing content)'}"
    )
    # Retry transient mid-stream drops (provider RemoteProtocolError / incomplete
    # chunked read). Each attempt restreams from scratch; partial output from a
    # failed attempt is discarded.
    last_err: Exception | None = None
    for attempt in range(3):
        chunks: list[str] = []
        try:
            async for c in stream_product_kit_doc(
                doc_type=doc_type,
                enriched_prompt=ctx["enriched_prompt"],
                research_report=ctx["research_report"],
                canvas_content=ctx["canvas_content"],
                brd_content=ctx["brd_content"],
                tech_spec_content=ctx["tech_spec_content"],
                conversation_history=[],
                new_user_message=new_msg,
                xsd_content=ctx.get("xsd_content", ""),
                decisions_block=ctx.get("decisions_block", ""),
            ):
                chunks.append(c)
            return "".join(chunks).strip()
        except Exception as e:
            last_err = e
            logger.warning("regenerate %s attempt %d/3 failed: %s", doc_type, attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
    raise last_err if last_err else RuntimeError("regeneration failed")


def find_changes_ready_for_revision(db) -> list[str]:
    """Change ids whose rounds are ALL closed, not frozen, target version ≤ 3,
    and which don't yet have a revision plan for that target. These are ready
    for an auto-drafted v(N+1) plan."""
    from app.models.change_request import ChangeRequest
    from app.models.kit_revision_plan import KitRevisionPlan
    from app.models.phase_c import NegotiationRoundState, RoundStatus

    change_ids = [r[0] for r in db.query(NegotiationRoundState.change_request_id).distinct().all()]
    ready: list[str] = []
    for cid in change_ids:
        open_n = (
            db.query(NegotiationRoundState)
            .filter(
                NegotiationRoundState.change_request_id == cid,
                NegotiationRoundState.status == RoundStatus.OPEN.value,
            )
            .count()
        )
        if open_n:
            continue
        cr = db.get(ChangeRequest, cid)
        if not cr or getattr(cr, "negotiation_frozen_at", None) is not None:
            continue
        target = (getattr(cr, "negotiation_version", 1) or 1) + 1
        if target > 3:  # round 1→v2, round 2→v3; v3 is final (freeze)
            continue
        exists = (
            db.query(KitRevisionPlan)
            .filter(
                KitRevisionPlan.change_request_id == cid,
                KitRevisionPlan.target_version == target,
            )
            .first()
        )
        if exists:
            continue
        ready.append(cid)
    return ready


async def notify_partners_revision_hold(change_id: str, target_version: int, *, in_progress: bool = True) -> None:
    """Advise every assigned partner that a kit revision is in progress (or has
    cleared). Partners hold queries while in_progress=True; the flag is cleared
    implicitly when the new kit ships (change_communication), so today we only
    ever push True — kept parameterised for an explicit clear if needed."""
    from sqlalchemy import select

    from app.models.phase_c import ChangePartnerAssignment, PartnerAgent
    from app.a2a_common.protocol import A2ATaskType
    from app.services.partner_dispatch import notify_partner

    db = SessionLocal()
    try:
        assignments = db.scalars(
            select(ChangePartnerAssignment).where(
                ChangePartnerAssignment.change_request_id == change_id,
            )
        ).all()
        for a in assignments:
            partner = db.get(PartnerAgent, a.partner_id)
            if not partner:
                continue
            try:
                await notify_partner(
                    partner.id,
                    A2ATaskType.REVISION_IN_PROGRESS.value,
                    {"in_progress": in_progress, "target_version": target_version},
                    change_id=change_id,
                    label=partner.name,
                    context="revision hold notify",
                )
            except Exception:
                logger.exception("revision-hold notify failed change=%s partner=%s", change_id, a.partner_id)
    finally:
        db.close()


async def auto_prepare_revision(change_id: str) -> bool:
    """Draft the v(N+1) revision plan from the closed round's outcomes so it's
    waiting in the Negotiation Hub. Idempotent (skips if a plan for the target
    version already exists). Returns True if a plan was created."""
    from app.agents.revision_planner import plan_revision
    from app.models.change_request import ChangeRequest
    from app.models.kit_revision_plan import KitRevisionPlan, RP_STATUS_DRAFT, RP_STATUS_NEEDS_RETRY
    from app.services.negotiation_extended import collect_round_outcomes

    db = SessionLocal()
    try:
        cr = db.get(ChangeRequest, change_id)
        if not cr or getattr(cr, "negotiation_frozen_at", None) is not None:
            return False

        # Race guard: re-check open rounds at commit time. The sweep that picked
        # this change may have scanned during the transient window after v(N)
        # shipped but before round N opened; by the time we run here round N can
        # be OPEN again (partner acked the new kit). Preparing v(N+1) now would
        # wrongly put the partner on hold while a round is open for them to
        # respond in. Never auto-revise while ANY round is open.
        from app.models.phase_c import NegotiationRoundState, RoundStatus
        open_rounds = (
            db.query(NegotiationRoundState)
            .filter(
                NegotiationRoundState.change_request_id == change_id,
                NegotiationRoundState.status == RoundStatus.OPEN.value,
            )
            .count()
        )
        if open_rounds:
            logger.info(
                "auto_prepare_revision: change=%s has %d open round(s) — skipping (round still open)",
                change_id, open_rounds,
            )
            return False

        current_ver = getattr(cr, "negotiation_version", 1) or 1
        target = current_ver + 1
        if target > 3:
            return False

        # Gap guard: only prepare v(N+1) once the round for the CURRENT version
        # (round_number == N) exists and has closed. In the window after v(N)
        # ships but before round N opens, max(round_number) is still N-1 — don't
        # mistake "no open rounds yet" for "round N closed".
        from sqlalchemy import func as _func
        max_round = (
            db.query(_func.max(NegotiationRoundState.round_number))
            .filter(NegotiationRoundState.change_request_id == change_id)
            .scalar()
        ) or 0
        if max_round < current_ver:
            logger.info(
                "auto_prepare_revision: change=%s round for v%d not opened yet "
                "(max_round=%d) — skipping",
                change_id, current_ver, max_round,
            )
            return False
        if (
            db.query(KitRevisionPlan)
            .filter(
                KitRevisionPlan.change_request_id == change_id,
                KitRevisionPlan.target_version == target,
            )
            .first()
        ):
            return False

        outcomes = collect_round_outcomes(change_id, db)
        # No resolved outcomes this round (e.g. it closed silently with nothing
        # decided) → there is no kit revision to draft, but the negotiation must
        # still move forward: open the next round, or FREEZE once the round cap
        # is reached (otherwise a silent round-cap close stalls in limbo and the
        # partner is never told it's frozen). Mirrors the PM's "no change needed"
        # advance/freeze, automated.
        if not outcomes:
            from app.services.negotiation_extended import (
                advance_or_freeze_after_close,
                notify_partners_frozen,
                send_round_events,
            )
            froze, events = advance_or_freeze_after_close(change_id, db)
            db.commit()
            logger.info(
                "auto_prepare_revision: no outcomes for change=%s — %s",
                change_id, "frozen (round cap reached)" if froze else "advanced to next round",
            )
            # Fan round_opened(silent_advance) / round_closed(frozen) events
            # so partners see the transition even though no kit was shipped.
            await send_round_events(events, db)
            if froze:
                try:
                    await notify_partners_frozen(change_id, db)
                except Exception:
                    logger.exception(
                        "notify_partners_frozen failed after silent close: change=%s", change_id
                    )
            else:
                # Advanced (not frozen). If a prior round had set the query hold,
                # nothing else clears it on an advance — only ship / freeze do —
                # so the partner composer would stay locked through the next
                # round. Clear it explicitly. Idempotent on the partner side.
                try:
                    await notify_partners_revision_hold(change_id, target, in_progress=False)
                except Exception:
                    logger.exception(
                        "revision-hold clear failed after silent advance: change=%s", change_id
                    )
            return False
        result = await plan_revision(
            outcomes=outcomes, change_title=cr.title or "", current_version=current_ver,
        )
        status = RP_STATUS_DRAFT if result.get("ok", True) else RP_STATUS_NEEDS_RETRY
        plan = KitRevisionPlan(
            change_request_id=change_id,
            target_version=target,
            status=status,
            items=result["items"],
            summary=result["summary"],
        )
        db.add(plan)
        db.commit()
        logger.info(
            "Auto-prepared revision plan: change=%s v%d items=%d status=%s",
            change_id, target, len(result["items"]), status,
        )
        # Hold the partner composer only when the plan actually revises documents.
        # An empty-items plan means "no docs to change" — locking the composer
        # through the next round in that case leaves the partner unable to reply
        # while the Authority has decided nothing needs revising.
        if result.get("items"):
            try:
                await notify_partners_revision_hold(change_id, target)
            except Exception:
                logger.exception("revision-hold notification failed (plan committed): change=%s v%d", change_id, target)
        return True
    except Exception:
        logger.exception("auto_prepare_revision failed for change=%s", change_id)
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        db.close()


async def generate_kit_revision(change_id: str, plan_id: str) -> None:
    """Background task: regenerate v(N+1) docs from the plan, bump the version,
    mark the plan generated. Does NOT ship."""
    from app.models.change_request import ChangeRequest
    from app.models.kit_revision_plan import (
        KitRevisionPlan, RP_STATUS_EDITED, RP_STATUS_GENERATED, RP_STATUS_GENERATING,
    )
    from app.models.product_kit import ProductKitDocType, ProductKitDocument
    from app.services.negotiation_extended import create_new_version
    from app.services.product_kit_query import latest_kit_docs

    db = SessionLocal()
    try:
        plan = db.get(KitRevisionPlan, plan_id)
        cr = db.get(ChangeRequest, change_id)
        if not plan or not cr:
            return

        ctx = _gather_context(change_id, db)
        _valid_types = {e.value for e in ProductKitDocType}
        current_docs = {
            (d.doc_type.value if hasattr(d.doc_type, "value") else d.doc_type): d
            for d in latest_kit_docs(db, change_id)
        }

        # Regenerate each included, supported doc.
        regenerated: dict[str, str] = {}
        for item in (plan.items or []):
            if not item.get("include", True):
                continue
            dt = item.get("doc_type")
            if dt not in _valid_types:
                continue
            existing = current_docs.get(dt)
            try:
                content = await _regenerate_doc(
                    dt, item.get("change_instruction", ""),
                    (existing.content if existing else ""), ctx,
                )
                if content:
                    regenerated[dt] = content
            except Exception:
                logger.exception("Kit revision: regenerate failed doc=%s change=%s (carrying forward)", dt, change_id)

        # Bump the published version, then write the new-version doc rows for the
        # UNION of existing docs and regenerated ones: carry each current doc
        # forward (regenerated content where we have it), AND emit any doc the
        # plan introduced that the kit didn't have yet — otherwise its freshly
        # generated content would be silently dropped.
        new_ver = create_new_version(change_id, db)
        db.commit()
        for dt in current_docs.keys() | regenerated.keys():
            doc = current_docs.get(dt)
            content = regenerated.get(dt, (doc.content or "") if doc else "")
            db.add(ProductKitDocument(
                change_request_id=change_id,
                doc_type=ProductKitDocType(dt) if not (doc and isinstance(doc.doc_type, ProductKitDocType)) else doc.doc_type,
                content=content,
                version=(doc.version + 1) if doc else 1,
                negotiation_version=new_ver,
            ))

        plan.status = RP_STATUS_GENERATED
        plan.target_version = new_ver
        db.commit()
        logger.info(
            "Kit revision generated: change=%s v%d regenerated=%d carried=%d new=%d",
            change_id, new_ver, len(regenerated),
            len(current_docs.keys() - regenerated.keys()),
            len(regenerated.keys() - current_docs.keys()),
        )
    except Exception:
        logger.exception("generate_kit_revision failed for change=%s plan=%s", change_id, plan_id)
        try:
            db.rollback()
            plan = db.get(KitRevisionPlan, plan_id)
            if plan and plan.status == RP_STATUS_GENERATING:
                plan.status = RP_STATUS_EDITED
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
