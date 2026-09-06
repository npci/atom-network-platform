# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Certification lifecycle timeline aggregator.

Joins six source tables into one chronologically-ordered event stream so the
"Cert Status" UI can show the full lifecycle of a change request and a
user-friendly activity log.

Sources combined:
  1. product_kit_documents       — cert test cases generated (Phase A)
  2. a2a_messages                — every A2A task in/out
  3. assignment_status_history   — every status transition + block/unblock
  4. cert_runs                   — run started + run completed
  5. cert_test_results           — per-TC outcomes (rolled up to one event
                                   per run by default; expand on request)
  6. cert_triage                 — AI verdict generated

Each event is normalised to:
  { timestamp, kind, severity, title, description, change_id, change_title,
    partner_id, partner_name, actor: {kind,name}, details: {...} }

The endpoint accepts optional filters: change_id, partner_id, limit,
since (ISO8601), kinds=comma-list. Always returns events sorted by timestamp
descending.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.core.deps import DbDep, CurrentUser
from app.models.change_request import ChangeRequest
from app.models.phase_c import (
    A2ADirection, A2AMessage, A2ATaskType,
    AssignmentStatusHistory, CertRun, CertTestResult, CertTriage,
    ChangePartnerAssignment, PartnerAgent,
)
from app.models.product_kit import ProductKitDocType, ProductKitDocument
from app.models.user import User
from app.models.cert_sync import CertSimulatorSyncLog

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cert-timeline"])


# ── Severity rules ────────────────────────────────────────────────────────────

_KIND_SEVERITY = {
    "test_cases_generated":   "info",
    "kit_communicated":       "info",
    "partner_acknowledged":   "info",
    "partner_progress":       "info",
    "partner_ready":          "success",
    "cert_started":           "info",
    "cert_completed_pass":    "success",
    "cert_completed_fail":    "warning",
    "triage_generated":       "warning",
    "approved_for_prod":      "success",
    "marked_live":            "success",
    "blocked":                "error",
    "unblocked":              "info",
    "withdrawn":              "error",
    "query":                  "info",
    "clarification":          "info",
    "defect_notice":          "warning",
    "defect_resolution":      "info",
    "test_suite_registered":  "info",
}

_TASK_TYPE_KIND = {
    "change_communication":   "kit_communicated",
    "change_acknowledgement": "partner_acknowledged",
    "status_update":          "partner_progress",
    "cert_readiness_declaration":  "partner_ready",
    "query":                  "query",
    "clarification_response": "clarification",
    "defect_notice":          "defect_notice",
    "defect_resolution":      "defect_resolution",
}


def _user_name(db, uid: str | None, cache: dict) -> str | None:
    if not uid: return None
    if uid in cache: return cache[uid]
    u = db.get(User, uid)
    cache[uid] = u.username if u else uid
    return cache[uid]


def _partner_name(db, pid: str | None, cache: dict) -> str | None:
    if not pid: return None
    if pid in cache: return cache[pid]
    p = db.get(PartnerAgent, pid)
    cache[pid] = p.name if p else pid
    return cache[pid]


def _evt(*, ts: datetime | None, kind: str, title: str, description: str = "",
         change_id: str | None = None, change_title: str | None = None,
         partner_id: str | None = None, partner_name: str | None = None,
         actor_kind: str = "system", actor_name: str | None = None,
         details: dict | None = None) -> dict | None:
    if ts is None:
        return None
    return {
        "timestamp":    ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "kind":         kind,
        "severity":     _KIND_SEVERITY.get(kind, "info"),
        "title":        title,
        "description":  description,
        "change_id":    change_id,
        "change_title": change_title,
        "partner_id":   partner_id,
        "partner_name": partner_name,
        "actor":        {"kind": actor_kind, "name": actor_name or "system"},
        "details":      details or {},
    }


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/cert-status/timeline")
def cert_timeline(
    db: DbDep,
    _: CurrentUser,
    change_id: str | None = None,
    partner_id: str | None = None,
    limit: int = Query(default=300, ge=1, le=1000),
    kinds: str | None = None,
):
    """Aggregate every cert-related event into a single sorted timeline."""
    user_cache: dict[str, str] = {}
    partner_cache: dict[str, str] = {}
    change_cache: dict[str, str] = {}

    def title_of(cid: str | None) -> str | None:
        if not cid:
            return None
        if cid in change_cache:
            return change_cache[cid]
        cr = db.get(ChangeRequest, cid)
        change_cache[cid] = (cr.title or cid[:8]) if cr else cid
        return change_cache[cid]

    events: list[dict] = []

    # ── 1. Cert test-cases generated (Phase A) ──────────────────────────────
    # ProductKitDocument keeps version history; emit one event per change for
    # the latest cert_test_cases version only (else the timeline double-counts).
    q = select(ProductKitDocument).where(
        ProductKitDocument.doc_type == ProductKitDocType.CERT_TEST_CASES
    )
    if change_id:
        q = q.where(ProductKitDocument.change_request_id == change_id)
    _latest_cert: dict[str, ProductKitDocument] = {}
    for _d in db.scalars(q).all():
        cur = _latest_cert.get(_d.change_request_id)
        if cur is None or _d.version > cur.version:
            _latest_cert[_d.change_request_id] = _d
    for d in _latest_cert.values():
        if not d.created_at:
            continue
        evt = _evt(
            ts=d.created_at,
            kind="test_cases_generated",
            title="Cert test cases generated",
            description=f"Phase A produced cert_test_cases doc ({len(d.content or '')} chars)",
            change_id=d.change_request_id,
            change_title=title_of(d.change_request_id),
            actor_kind="system",
            actor_name="Phase A agent",
            details={"doc_id": d.id, "version": d.version, "size": len(d.content or "")},
        )
        if evt: events.append(evt)

    # ── 2. A2A messages — kit comms, acks, queries, status, defects ────────
    qm = select(A2AMessage)
    if change_id:
        qm = qm.where(A2AMessage.change_request_id == change_id)
    if partner_id:
        qm = qm.where(A2AMessage.partner_id == partner_id)
    for m in db.scalars(qm).all():
        tt = m.task_type.value if hasattr(m.task_type, "value") else m.task_type
        kind = _TASK_TYPE_KIND.get(tt)
        if not kind:
            # Skip cert_test_request/response — those are covered by cert_runs.
            continue
        direction = m.direction.value if hasattr(m.direction, "value") else m.direction
        pname = _partner_name(db, m.partner_id, partner_cache)
        if kind == "kit_communicated":
            # Tell the truth about delivery. This row previously always read "delivered",
            # even for `delivery_failed` messages — the timeline actively misreported a
            # bank as having received a kit it never got.
            if m.status == "delivery_failed":
                title = f"Change kit FAILED to send to {pname}"
                desc  = ("Delivery error"
                         + (f": {m.error_code}" if m.error_code else "")
                         + " — kit NOT received, needs resend")
            elif m.status == "pending":
                title = f"Change kit not sent to {pname}"
                desc  = "Partner has no A2A endpoint configured — kit was never dispatched"
            else:
                title  = f"Change kit delivered to {pname}"
                desc   = "Product Kit (BRD, Tech Spec, cert test cases, …) sent via A2A"
            actor_kind = "system"
            actor_name = "NPCI"
        elif kind == "partner_acknowledged":
            title  = f"{pname} acknowledged the change"
            desc   = "Partner formally accepted the change request"
            actor_kind = "partner"
            actor_name = pname
        elif kind == "partner_progress":
            step = (m.payload or {}).get("step", "step")
            title  = f"{pname} reported {step}"
            desc   = (m.payload or {}).get("notes") or ""
            actor_kind = "partner"
            actor_name = pname
        elif kind == "partner_ready":
            title  = f"{pname} declared ready for certification"
            desc   = "All implementation milestones confirmed"
            actor_kind = "partner"
            actor_name = pname
        elif kind == "query":
            title  = f"{pname} asked a clarification query"
            desc   = ((m.payload or {}).get("query_text") or "")[:120]
            actor_kind = "partner"
            actor_name = pname
        elif kind == "clarification":
            title  = f"the Authority responded to {pname}'s query"
            desc   = ((m.payload or {}).get("response_text") or "")[:120]
            actor_kind = "user"
            actor_name = "the Authority PO"
        elif kind == "defect_notice":
            title  = f"Defect raised on {pname}"
            desc   = ((m.payload or {}).get("notes") or "")[:120]
            actor_kind = "system" if direction == "outbound" else "partner"
            actor_name = "NPCI" if direction == "outbound" else pname
        elif kind == "defect_resolution":
            title  = f"Defect resolved on {pname}"
            desc   = ((m.payload or {}).get("notes") or "")[:120]
            actor_kind = "partner"
            actor_name = pname
        else:
            continue
        evt = _evt(
            ts=m.created_at,
            kind=kind,
            title=title,
            description=desc,
            change_id=m.change_request_id,
            change_title=title_of(m.change_request_id),
            partner_id=m.partner_id,
            partner_name=pname,
            actor_kind=actor_kind,
            actor_name=actor_name,
            details={"task_type": tt, "direction": direction, "message_id": m.id},
        )
        if evt: events.append(evt)

    # ── 3. assignment_status_history — admin transitions + auto promotions ─
    qh = select(AssignmentStatusHistory).join(
        ChangePartnerAssignment,
        AssignmentStatusHistory.assignment_id == ChangePartnerAssignment.id,
    )
    if change_id:
        qh = qh.where(ChangePartnerAssignment.change_request_id == change_id)
    if partner_id:
        qh = qh.where(ChangePartnerAssignment.partner_id == partner_id)

    for h in db.scalars(qh).all():
        a = db.get(ChangePartnerAssignment, h.assignment_id)
        if not a:
            continue
        pname = _partner_name(db, a.partner_id, partner_cache)
        actor_kind = "user" if h.actor_user_id else ("partner" if h.actor_partner_id else "system")
        actor_name = (
            _user_name(db, h.actor_user_id, user_cache)
            or _partner_name(db, h.actor_partner_id, partner_cache)
            or "system"
        )
        to_status = h.to_status or ""
        from_status = h.from_status or ""

        # Map history rows to specific event kinds.
        if "(blocked)" in to_status and "(blocked)" not in from_status:
            kind, title = "blocked", f"{pname} blocked"
        elif "(blocked)" in from_status and "(blocked)" not in to_status:
            kind, title = "unblocked", f"{pname} unblocked"
        elif to_status == "withdrawn":
            kind, title = "withdrawn", f"{pname} withdrawn"
        elif to_status == "ready_for_production":
            kind, title = "approved_for_prod", f"{pname} approved for production"
        elif to_status == "in_production":
            kind, title = "marked_live", f"{pname} marked live in production"
        elif to_status == "certifying":
            kind, title = "cert_started", f"Cert run started for {pname}"
        elif to_status == "certified":
            kind, title = "cert_completed_pass", f"{pname} certified"
        elif to_status == "ready_for_certification":
            kind, title = "partner_ready", f"{pname} declared ready for certification"
        elif to_status == "accepted":
            kind, title = "partner_acknowledged", f"{pname} acknowledged"
        elif to_status in ("applied", "tested"):
            kind, title = "partner_progress", f"{pname} reached '{to_status}'"
        else:
            # Generic fallback for any other transition
            kind, title = "partner_progress", f"{pname}: {from_status or '—'} → {to_status}"

        evt = _evt(
            ts=h.created_at,
            kind=kind,
            title=title,
            description=h.reason or "",
            change_id=a.change_request_id,
            change_title=title_of(a.change_request_id),
            partner_id=a.partner_id,
            partner_name=pname,
            actor_kind=actor_kind,
            actor_name=actor_name,
            details={"from_status": from_status, "to_status": to_status, "history_id": h.id},
        )
        if evt: events.append(evt)

    # ── 4. cert_runs — completed events (started events are already covered
    #      via assignment_status_history). We add a "completed" event using
    #      completed_at + summary counts.
    qr = select(CertRun)
    if change_id:
        qr = qr.where(CertRun.change_request_id == change_id)
    if partner_id:
        qr = qr.where(CertRun.partner_id == partner_id)
    for r in db.scalars(qr).all():
        if not r.completed_at:
            continue
        pname = _partner_name(db, r.partner_id, partner_cache)
        all_pass = (r.failed or 0) == 0 and (r.passed or 0) > 0
        kind = "cert_completed_pass" if all_pass else "cert_completed_fail"
        title = (
            f"Cert run #{r.run_number} completed: {r.passed}/{r.total} passed"
            if all_pass
            else f"Cert run #{r.run_number} completed with {r.failed} failure(s)"
        )
        evt = _evt(
            ts=r.completed_at,
            kind=kind,
            title=title,
            description=(
                f"{r.passed or 0} pass · {r.failed or 0} fail · {r.skipped or 0} skip"
                f" / {r.total or 0} total"
            ),
            change_id=r.change_request_id,
            change_title=title_of(r.change_request_id),
            partner_id=r.partner_id,
            partner_name=pname,
            actor_kind="system",
            actor_name="cert engine",
            details={
                "run_id":    r.id,
                "run_number": r.run_number,
                "passed":    r.passed,
                "failed":    r.failed,
                "skipped":   r.skipped,
                "total":     r.total,
            },
        )
        if evt: events.append(evt)

    # ── 5. cert_triage — AI verdicts ───────────────────────────────────────
    qt = select(CertTriage).join(
        CertTestResult, CertTriage.cert_test_result_id == CertTestResult.id
    ).join(CertRun, CertTestResult.cert_run_id == CertRun.id)
    if change_id:
        qt = qt.where(CertRun.change_request_id == change_id)
    if partner_id:
        qt = qt.where(CertRun.partner_id == partner_id)
    # group triage rows by (run_id, verdict) so we don't spam the timeline
    # with 1 event per failed TC. One event per (run, verdict) tuple.
    triage_groups: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "min_ts": None, "run_id": None, "partner_id": None, "change_id": None})
    for t in db.scalars(qt).all():
        result = db.get(CertTestResult, t.cert_test_result_id)
        if not result:
            continue
        run = db.get(CertRun, result.cert_run_id)
        if not run:
            continue
        vk = t.ai_verdict.value if hasattr(t.ai_verdict, "value") else t.ai_verdict
        key = (run.id, vk)
        g = triage_groups[key]
        g["count"] += 1
        if g["min_ts"] is None or t.created_at < g["min_ts"]:
            g["min_ts"] = t.created_at
        g["run_id"] = run.id
        g["run_number"] = run.run_number
        g["partner_id"] = run.partner_id
        g["change_id"] = run.change_request_id
        g["verdict"] = vk

    for g in triage_groups.values():
        pname = _partner_name(db, g["partner_id"], partner_cache)
        verdict_label = {
            "partner_code_bug": "partner code bug",
            "test_case_issue":  "test case issue",
            "env_issue":        "env issue",
        }.get(g["verdict"], g["verdict"])
        evt = _evt(
            ts=g["min_ts"],
            kind="triage_generated",
            title=f"AI triage: {g['count']} tc(s) flagged as {verdict_label} for {pname}",
            description=f"Run #{g['run_number']} · verdict: {verdict_label}",
            change_id=g["change_id"],
            change_title=title_of(g["change_id"]),
            partner_id=g["partner_id"],
            partner_name=pname,
            actor_kind="system",
            actor_name="AI triage",
            details={"verdict": g["verdict"], "count": g["count"], "run_number": g["run_number"]},
        )
        if evt: events.append(evt)

    # ── 6. cert_simulator_sync_log — Phase A → cert-agent test-case pushes ──
    qs = select(CertSimulatorSyncLog).where(CertSimulatorSyncLog.operation == "apply")
    if change_id:
        qs = qs.where(CertSimulatorSyncLog.change_request_id == change_id)
    for s in db.scalars(qs).all():
        sumr = s.summary or {}
        applied = sumr.get("applied", 0)
        failed_n = len(sumr.get("failed") or [])
        subset_tag = sumr.get("subset") or f"cr-{(s.change_request_id or '')[:8]}"
        actor_name_v = _user_name(db, s.actor_user_id, user_cache) or "system"
        sev = "info" if failed_n == 0 else "warning"
        evt = _evt(
            ts=s.created_at,
            kind="test_suite_registered",
            title="Test suite registered with cert simulator",
            description=(
                f"{applied} applied · {failed_n} failed · subset {subset_tag}"
            ),
            change_id=s.change_request_id,
            change_title=title_of(s.change_request_id),
            partner_id=s.cert_engine_partner_id,
            partner_name=_partner_name(db, s.cert_engine_partner_id, partner_cache),
            actor_kind="user" if s.actor_user_id else "system",
            actor_name=actor_name_v,
            details={
                "applied": applied,
                "failed":  failed_n,
                "subset":  subset_tag,
                "log_id":  s.id,
            },
        )
        if evt:
            evt["severity"] = sev
            events.append(evt)

    # ── Filter by `kinds` if given, then sort + cap ────────────────────────
    if kinds:
        wanted = {k.strip() for k in kinds.split(",") if k.strip()}
        events = [e for e in events if e["kind"] in wanted]

    events.sort(key=lambda e: e["timestamp"], reverse=True)
    events = events[:limit]

    return {
        "events":    events,
        "count":     len(events),
        "filters":   {"change_id": change_id, "partner_id": partner_id, "kinds": kinds, "limit": limit},
    }


@router.get("/cert-status/timeline/changes")
def cert_timeline_changes(db: DbDep, _: CurrentUser):
    """Lightweight list of all changes with cert activity — used by the UI's
    change selector dropdown without loading the full timeline."""
    rows = []
    for cr in db.scalars(select(ChangeRequest).order_by(ChangeRequest.created_at.desc())).all():
        # has any cert-relevant data?
        has_assignment = db.scalars(
            select(ChangePartnerAssignment).where(ChangePartnerAssignment.change_request_id == cr.id).limit(1)
        ).first()
        if not has_assignment:
            continue
        rows.append({
            "id":         cr.id,
            "title":      cr.title or "(untitled)",
            "status":     cr.status.value if hasattr(cr.status, "value") else str(cr.status),
            "created_at": cr.created_at.isoformat() if cr.created_at else None,
        })
    return {"changes": rows}
