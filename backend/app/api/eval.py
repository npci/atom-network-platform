# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Eval harness APIs — read and policy config (Phase 2)."""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import AdminUser, DbDep, CurrentUser
from app.models.change_request import ChangeRequest
from app.models.eval_policy_audit import EvalPolicyAudit
from app.models.eval_verdict import EvalVerdict
from app.models.user import UserRole
from app.schemas.eval import (
    EvalPolicyAuditListResponse,
    EvalLatestByCheckpointResponse,
    EvalLatestCheckpointResponse,
    EvalOverrideRequest,
    EvalOverrideResponse,
    EvalPolicyListResponse,
    EvalPolicyUpdateRequest,
    EvalPolicyUpdateResponse,
    EvalVerdictListResponse,
)
from app.services.evaluation.checkpoints import CheckpointId, FIRST_WAVE_CHECKPOINTS, PolicyMode
from app.services.evaluation.contracts import all_contracts, get_contract
from app.services.evaluation.policy import get_policy_mode, list_policy_overrides, set_policy_mode
from app.services.evaluation.store import get_by_id, get_history, get_latest, save_override_verdict

router = APIRouter(tags=["eval"])
PRODUCTION_POLICY_CONFIRM_TEXT = "I understand this changes production eval gates"


def _get_change_or_404(db: Session, change_id: str) -> ChangeRequest:
    change = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not change:
        raise HTTPException(status_code=404, detail="Change request not found")
    return change


@router.get("/changes/{change_id}/eval/verdicts", response_model=EvalVerdictListResponse)
def list_verdicts(
    change_id: str,
    checkpoint: Optional[str] = Query(None, description="Filter by checkpoint ID"),
    limit: int = Query(50, ge=1, le=200),
    db: DbDep = None,
    _: CurrentUser = None,
):
    """Return verdict history for a change request, newest first.

    Optionally filter by checkpoint ID.
    """
    _get_change_or_404(db, change_id)
    rows = get_history(db, change_id, checkpoint_id=checkpoint, limit=limit)
    return {
        "change_request_id": change_id,
        "checkpoint": checkpoint,
        "count": len(rows),
        "verdicts": [r.to_dict() for r in rows],
    }


@router.get(
    "/changes/{change_id}/eval/latest",
    response_model=EvalLatestCheckpointResponse | EvalLatestByCheckpointResponse,
)
def latest_verdict(
    change_id: str,
    checkpoint: Optional[str] = Query(None, description="Specific checkpoint ID"),
    db: DbDep = None,
    _: CurrentUser = None,
):
    """Return the latest verdict for a change request.

    If checkpoint is specified, returns the latest for that checkpoint only.
    If not specified, returns the latest for each first-wave checkpoint.
    """
    _get_change_or_404(db, change_id)

    if checkpoint:
        row = get_latest(db, change_id, checkpoint)
        if not row:
            return {
                "change_request_id": change_id,
                "checkpoint": checkpoint,
                "verdict": None,
                "message": "No verdict recorded yet for this checkpoint.",
            }
        return {
            "change_request_id": change_id,
            "checkpoint": checkpoint,
            "verdict": row.to_dict(),
        }

    # Return latest for all first-wave checkpoints
    result = {}
    for cp in FIRST_WAVE_CHECKPOINTS:
        row = get_latest(db, change_id, cp)
        result[cp.value] = row.to_dict() if row else None

    return {
        "change_request_id": change_id,
        "checkpoints": result,
    }


@router.get("/admin/eval/policies", response_model=EvalPolicyListResponse)
def list_eval_policies(
    db: DbDep = None,
    _: AdminUser = None,
):
    overrides = list_policy_overrides(db)
    entries = []
    for contract in all_contracts():
        checkpoint_id = contract.checkpoint_id.value
        if checkpoint_id in overrides:
            entries.append({
                "checkpoint_id": checkpoint_id,
                "policy_mode": overrides[checkpoint_id].value,
                "source": "config",
            })
        else:
            entries.append({
                "checkpoint_id": checkpoint_id,
                "policy_mode": contract.policy_mode.value,
                "source": "contract_default",
            })
    return {"policies": entries}


@router.put("/admin/eval/policies", response_model=EvalPolicyUpdateResponse)
def update_eval_policies(
    body: EvalPolicyUpdateRequest,
    db: DbDep = None,
    current_user: AdminUser = None,
):
    reason = body.reason.strip()
    if len(reason) < 8:
        raise HTTPException(status_code=400, detail="reason must be at least 8 non-space characters.")

    app_env = (settings.app_env or "development").lower()
    if app_env == "production":
        if not body.confirm_production:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Production policy update requires confirm_production=true "
                    "and exact confirm_text."
                ),
            )
        if (body.confirm_text or "").strip() != PRODUCTION_POLICY_CONFIRM_TEXT:
            raise HTTPException(
                status_code=400,
                detail=f"Production confirm_text mismatch. Expected: '{PRODUCTION_POLICY_CONFIRM_TEXT}'",
            )

    parsed_updates: list[tuple[CheckpointId, PolicyMode]] = []
    for checkpoint_raw, mode_raw in body.policies.items():
        try:
            checkpoint = CheckpointId(checkpoint_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown checkpoint_id: {checkpoint_raw}")

        try:
            mode = PolicyMode(mode_raw)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid policy_mode '{mode_raw}' for checkpoint '{checkpoint_raw}'",
            )

        parsed_updates.append((checkpoint, mode))

    updated: list[dict] = []
    try:
        for checkpoint, mode in parsed_updates:
            old_mode = get_policy_mode(db, checkpoint, fallback=get_contract(checkpoint).policy_mode)
            if old_mode == mode:
                continue

            set_policy_mode(db, checkpoint, mode, commit=False)
            db.add(EvalPolicyAudit(
                checkpoint_id=checkpoint.value,
                old_policy_mode=old_mode.value,
                new_policy_mode=mode.value,
                actor_user_id=getattr(current_user, "id", None),
                actor_username=getattr(current_user, "username", "unknown"),
                reason=reason,
                app_env=app_env,
            ))
            updated.append({
                "checkpoint_id": checkpoint.value,
                "policy_mode": mode.value,
                "source": "config",
            })
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise

    return {"updated": updated}


def _compute_change_impact(db, change_id: str) -> dict:
    """Aggregate the harness's impact on a single change request.

    Returns:
      - verdict counts (PASS/WARN/FAIL/total)
      - overrides (manual unblocks)
      - hard_fail_codes_caught (deduplicated list)
      - reasons_total (sum of `reasons` length across all verdicts; the
        "issues the harness raised that wouldn't otherwise have been visible")
      - retry_runs (verdicts where previous_verdict_id is set; proxy for
        retry-with-critique events)
      - artifact_stats (BRD FR count, Tech Spec error code count, etc.)
    """
    from collections import Counter
    rows = (
        db.query(EvalVerdict)
        .filter(EvalVerdict.change_request_id == change_id)
        .order_by(EvalVerdict.created_at)
        .all()
    )
    counts = Counter(r.verdict for r in rows)
    overrides = sum(1 for r in rows if r.is_override)
    retry_runs = sum(1 for r in rows if r.previous_verdict_id)
    reasons_total = sum(len(r.reasons_json or []) for r in rows)
    hard_codes: Counter = Counter()
    for r in rows:
        for c in (r.hard_fail_codes or []):
            hard_codes[c] += 1
    critic_runs = sum(1 for r in rows if r.critic_model)

    # Artifact-level stats — what the harness influenced or measured.
    from app.models.brd import BRD as BRDModel
    from app.models.tech_spec import TechSpec as TSModel
    from app.models.canvas import ProductCanvas as CanvasModel
    from app.models.research import ResearchOutput
    from app.models.clarification import Clarification

    def _latest(model_cls):
        return (
            db.query(model_cls)
            .filter(model_cls.change_request_id == change_id)
            .order_by(model_cls.version.desc())
            .first()
        )
    import re
    fr_pattern = re.compile(r"\bFR-\d+\b")
    error_code_pattern = re.compile(r"\b(?:U\d{1,3}|Z\d{1,2}|RB|XT|XD|YB|YC|YD|00)\b")

    brd = _latest(BRDModel)
    ts = _latest(TSModel)
    canvas = _latest(CanvasModel)
    research = _latest(ResearchOutput)
    clar = (
        db.query(Clarification)
        .filter(Clarification.change_request_id == change_id)
        .order_by(Clarification.version.desc())
        .first()
    )

    def _len(text):
        return len(text or "")

    artifact_stats = {
        "brd_chars":            _len(getattr(brd, "content", None)),
        "brd_fr_count":         len(set(fr_pattern.findall(getattr(brd, "content", "") or ""))),
        "tech_spec_chars":      _len(getattr(ts, "content", None)),
        "tech_spec_fr_count":   len(set(fr_pattern.findall(getattr(ts, "content", "") or ""))),
        "tech_spec_error_codes": len(set(error_code_pattern.findall(getattr(ts, "content", "") or ""))),
        "canvas_chars":         _len(getattr(canvas, "content", None)),
        "research_chars":       _len(getattr(research, "combined_report", None)),
        "clarification_questions": len(((clar.questions or []) if clar else [])),
        "clarification_status": (clar.status if clar else None),
    }

    return {
        "change_request_id": change_id,
        "verdicts": {
            "total": len(rows),
            "PASS":  counts.get("PASS", 0),
            "WARN":  counts.get("WARN", 0),
            "FAIL":  counts.get("FAIL", 0),
        },
        "overrides":             overrides,
        "retry_runs":            retry_runs,
        "reasons_total":         reasons_total,
        "critic_runs":           critic_runs,
        "hard_fail_codes_top":   [{"code": c, "count": n} for c, n in hard_codes.most_common(8)],
        "artifact_stats":        artifact_stats,
    }


@router.get("/changes/{change_id}/eval/impact")
def change_impact(change_id: str, db: DbDep = None, _: CurrentUser = None):
    """Per-change harness impact summary — what did the eval layer catch?"""
    _get_change_or_404(db, change_id)
    return _compute_change_impact(db, change_id)


@router.get("/admin/eval/compare")
def eval_compare(
    change_a: str = Query(..., description="Control change (typically harness disabled)"),
    change_b: str = Query(..., description="Treatment change (typically harness enabled)"),
    db: DbDep = None,
    _: AdminUser = None,
):
    """Side-by-side comparison of two change requests' harness impact.

    Intended workflow: run the same prompt as two changes — one with
    every checkpoint policy set to 'disabled' (control), one with the
    full harness on (treatment). Then hit this endpoint to see what the
    harness caught and how artifacts differ.
    """
    if change_a == change_b:
        raise HTTPException(status_code=400, detail="change_a and change_b must differ")
    cr_a = _get_change_or_404(db, change_a)
    cr_b = _get_change_or_404(db, change_b)
    a = _compute_change_impact(db, change_a)
    b = _compute_change_impact(db, change_b)
    a["title"] = cr_a.title or ""
    b["title"] = cr_b.title or ""

    def _delta(field_path: list[str]) -> dict:
        a_val = a
        b_val = b
        for f in field_path:
            a_val = a_val.get(f, 0) if isinstance(a_val, dict) else 0
            b_val = b_val.get(f, 0) if isinstance(b_val, dict) else 0
        return {"a": a_val, "b": b_val, "delta": (b_val or 0) - (a_val or 0)}

    diff = {
        "verdicts_total":         _delta(["verdicts", "total"]),
        "verdicts_fail":          _delta(["verdicts", "FAIL"]),
        "verdicts_warn":          _delta(["verdicts", "WARN"]),
        "verdicts_pass":          _delta(["verdicts", "PASS"]),
        "overrides":              _delta(["overrides"]),
        "reasons_total":          _delta(["reasons_total"]),
        "retry_runs":             _delta(["retry_runs"]),
        "brd_fr_count":           _delta(["artifact_stats", "brd_fr_count"]),
        "tech_spec_fr_count":     _delta(["artifact_stats", "tech_spec_fr_count"]),
        "tech_spec_error_codes":  _delta(["artifact_stats", "tech_spec_error_codes"]),
        "brd_chars":              _delta(["artifact_stats", "brd_chars"]),
        "tech_spec_chars":        _delta(["artifact_stats", "tech_spec_chars"]),
    }

    return {"a": a, "b": b, "diff": diff}


@router.get("/admin/eval/metrics")
def eval_metrics(
    since: Optional[str] = Query(None, description="ISO timestamp lower bound (inclusive)"),
    until: Optional[str] = Query(None, description="ISO timestamp upper bound (exclusive)"),
    checkpoint: Optional[str] = Query(None, description="Filter to one checkpoint"),
    db: DbDep = None,
    _: AdminUser = None,
):
    """Aggregate verdict stats across all changes for the operator dashboard.

    Returns global counters plus a per-checkpoint breakdown:
      - PASS / WARN / FAIL counts and rates
      - override count (verdicts where is_override is true)
      - avg latency_ms (critic + deterministic combined)
      - top hard_fail_codes (up to 5 most-frequent)
      - per-day trend over the filter window (capped at 30 days back)

    Filters are optional and AND-combined. The page renders fast — all
    counters are computed with SQL aggregates against eval_verdicts.
    """
    from collections import Counter
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func

    # ── Parse timestamp filters ───────────────────────────────────────────
    def _parse(ts: str | None) -> datetime | None:
        if not ts:
            return None
        try:
            value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid ISO timestamp: {ts}")

    since_dt = _parse(since)
    until_dt = _parse(until)
    if since_dt and until_dt and until_dt <= since_dt:
        raise HTTPException(status_code=400, detail="until must be after since")

    # ── Base query ────────────────────────────────────────────────────────
    query = db.query(EvalVerdict)
    if since_dt:
        query = query.filter(EvalVerdict.created_at >= since_dt)
    if until_dt:
        query = query.filter(EvalVerdict.created_at < until_dt)
    if checkpoint:
        query = query.filter(EvalVerdict.checkpoint_id == checkpoint)

    rows = query.all()

    # ── Global counters ───────────────────────────────────────────────────
    total = len(rows)
    counts_by_verdict = Counter(r.verdict for r in rows)
    overrides = sum(1 for r in rows if r.is_override)
    avg_latency = (
        sum(r.latency_ms or 0 for r in rows) / total if total else 0.0
    )
    critic_runs = sum(1 for r in rows if r.critic_model)
    critic_share = (critic_runs / total) if total else 0.0

    # ── Per-checkpoint breakdown ──────────────────────────────────────────
    per_cp: dict[str, dict] = {}
    for r in rows:
        cp = r.checkpoint_id
        bucket = per_cp.setdefault(cp, {
            "checkpoint_id": cp,
            "total": 0,
            "PASS": 0, "WARN": 0, "FAIL": 0,
            "overrides": 0,
            "critic_runs": 0,
            "latency_sum_ms": 0,
            "_hard_fail_codes": Counter(),
            "_dates": Counter(),
        })
        bucket["total"] += 1
        bucket[r.verdict] = bucket.get(r.verdict, 0) + 1
        if r.is_override:
            bucket["overrides"] += 1
        if r.critic_model:
            bucket["critic_runs"] += 1
        bucket["latency_sum_ms"] += int(r.latency_ms or 0)
        for code in (r.hard_fail_codes or []):
            bucket["_hard_fail_codes"][code] += 1
        if r.created_at:
            bucket["_dates"][r.created_at.date().isoformat()] += 1

    # Trim helper-internal counters into shipped lists.
    for cp, b in per_cp.items():
        b["avg_latency_ms"] = (b["latency_sum_ms"] / b["total"]) if b["total"] else 0.0
        b["top_hard_fail_codes"] = [
            {"code": c, "count": n} for c, n in b.pop("_hard_fail_codes").most_common(5)
        ]
        # 30-day trend window
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date()
        b["trend"] = sorted(
            ({"date": d, "count": n} for d, n in b.pop("_dates").items() if d >= cutoff.isoformat()),
            key=lambda x: x["date"],
        )
        b.pop("latency_sum_ms", None)

    # ── Global top hard-fail codes ────────────────────────────────────────
    global_codes: Counter = Counter()
    for r in rows:
        for code in (r.hard_fail_codes or []):
            global_codes[code] += 1

    return {
        "global": {
            "total": total,
            "PASS": counts_by_verdict.get("PASS", 0),
            "WARN": counts_by_verdict.get("WARN", 0),
            "FAIL": counts_by_verdict.get("FAIL", 0),
            "overrides": overrides,
            "avg_latency_ms": avg_latency,
            "critic_share": critic_share,
            "top_hard_fail_codes": [
                {"code": c, "count": n} for c, n in global_codes.most_common(8)
            ],
        },
        "checkpoints": sorted(per_cp.values(), key=lambda b: (-b["total"], b["checkpoint_id"])),
        "window": {
            "since": since_dt.isoformat() if since_dt else None,
            "until": until_dt.isoformat() if until_dt else None,
            "checkpoint": checkpoint,
        },
    }


@router.get("/admin/eval/verdicts")
def list_all_eval_verdicts(
    change_id: Optional[str] = Query(None, description="Filter by change request id"),
    checkpoint: Optional[str] = Query(None, description="Filter by checkpoint id"),
    verdict: Optional[str] = Query(None, description="Filter by verdict: PASS, WARN, FAIL"),
    policy_mode: Optional[str] = Query(None, description="Filter by policy mode at time of decision"),
    is_override: Optional[bool] = Query(None, description="Only override rows when true"),
    since: Optional[str] = Query(None, description="ISO timestamp lower bound (inclusive)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: DbDep = None,
    _: AdminUser = None,
):
    """Admin-only: list every eval verdict across all change requests, newest first.

    Joins the change request's title for easy operator scanning. All filters
    are optional and AND-combined. Returns a paginated slice plus the total
    matching count so the UI can render a pager.
    """
    query = db.query(EvalVerdict)

    if change_id:
        query = query.filter(EvalVerdict.change_request_id == change_id)
    if checkpoint:
        query = query.filter(EvalVerdict.checkpoint_id == checkpoint)
    if verdict:
        v = verdict.strip().upper()
        if v not in ("PASS", "WARN", "FAIL"):
            raise HTTPException(status_code=400, detail="verdict must be PASS, WARN, or FAIL")
        query = query.filter(EvalVerdict.verdict == v)
    if policy_mode:
        pm = policy_mode.strip().lower()
        if pm not in {m.value for m in PolicyMode}:
            raise HTTPException(status_code=400, detail=f"Unknown policy_mode: {policy_mode}")
        query = query.filter(EvalVerdict.policy_mode == pm)
    if is_override is not None:
        query = query.filter(EvalVerdict.is_override.is_(bool(is_override)))
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="since must be ISO-8601")
        query = query.filter(EvalVerdict.created_at >= since_dt)

    total = query.count()
    rows = (
        query.order_by(EvalVerdict.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    change_ids = list({r.change_request_id for r in rows})
    title_by_id: dict[str, str] = {}
    if change_ids:
        for cr in (
            db.query(ChangeRequest)
            .filter(ChangeRequest.id.in_(change_ids))
            .all()
        ):
            title_by_id[cr.id] = cr.title or (cr.initial_prompt or "")[:80]

    items = []
    for row in rows:
        item = row.to_dict()
        item["change_request_title"] = title_by_id.get(row.change_request_id) or ""
        items.append(item)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(items),
        "items": items,
    }


@router.get("/admin/eval/policy-audit", response_model=EvalPolicyAuditListResponse)
def list_eval_policy_audit(
    checkpoint: Optional[str] = Query(None, description="Filter by checkpoint ID"),
    limit: int = Query(50, ge=1, le=200),
    db: DbDep = None,
    _: AdminUser = None,
):
    if checkpoint:
        try:
            CheckpointId(checkpoint)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown checkpoint_id: {checkpoint}")

    query = db.query(EvalPolicyAudit)
    if checkpoint:
        query = query.filter(EvalPolicyAudit.checkpoint_id == checkpoint)
    rows = query.order_by(EvalPolicyAudit.created_at.desc()).limit(limit).all()
    return {"count": len(rows), "items": [row.to_dict() for row in rows]}


def _reconstruct_artifacts_for_checkpoint(
    db: Session, change_id: str, cp: CheckpointId
) -> tuple[dict, dict, list[str], list[str]] | None:
    """Rebuild the source + target artifact dicts for one checkpoint by
    loading the latest persisted artifacts on this change.

    Returns (source_artifacts, target_artifacts, source_ids, target_ids)
    or None when a required artifact is missing.
    """
    from app.models.brd import BRD
    from app.models.canvas import ProductCanvas
    from app.models.change_request import ChangeRequest
    from app.models.clarification import Clarification
    from app.models.research import ResearchOutput
    from app.models.tech_spec import TechSpec

    cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not cr:
        return None

    def _latest(model_cls):
        return (
            db.query(model_cls)
            .filter(model_cls.change_request_id == change_id)
            .order_by(model_cls.version.desc())
            .first()
        )

    if cp == CheckpointId.INITIAL_TO_PROMPT_ENHANCED:
        if not cr.enhanced_prompt:
            return None
        return (
            {"initial_prompt": {"type": "initial_prompt", "content": cr.initial_prompt or ""}},
            {"enhanced_prompt": {"type": "enhanced_prompt", "content": cr.enhanced_prompt}},
            [], [],
        )

    if cp == CheckpointId.PROMPT_TO_RESEARCH:
        research = _latest(ResearchOutput)
        if not research:
            return None
        return (
            {"enhanced_prompt": {"type": "enhanced_prompt", "content": cr.enhanced_prompt or cr.initial_prompt or ""}},
            {"research_summary": {"type": "research_summary", "content": research.combined_report or ""}},
            [], [research.id] if research.id else [],
        )

    if cp == CheckpointId.RESEARCH_TO_CANVAS:
        research = _latest(ResearchOutput)
        canvas = _latest(ProductCanvas)
        if not canvas:
            return None
        return (
            {"research_summary": {"type": "research_summary", "content": (research.combined_report or "") if research else ""}},
            {"product_canvas": {"type": "product_canvas", "content": canvas.content or ""}},
            [research.id] if research and research.id else [],
            [canvas.id] if canvas.id else [],
        )

    if cp == CheckpointId.CANVAS_TO_CLARIFICATION:
        canvas = _latest(ProductCanvas)
        clar = _latest(Clarification)
        if not clar:
            return None
        return (
            {"product_canvas": {"type": "product_canvas", "content": (canvas.content or "") if canvas else ""}},
            {"clarification_thread": {
                "type": "clarification_thread",
                "questions": clar.questions or [],
                "answers": clar.answers or {},
                "status": clar.status or "",
            }},
            [canvas.id] if canvas and canvas.id else [],
            [clar.id] if clar.id else [],
        )

    if cp == CheckpointId.CLARIFICATION_TO_BRD:
        canvas = _latest(ProductCanvas)
        clar = _latest(Clarification)
        brd = _latest(BRD)
        if not brd:
            return None
        return (
            {
                "product_canvas": {"type": "product_canvas", "content": (canvas.content or "") if canvas else ""},
                "clarification_thread": {
                    "type": "clarification_thread",
                    "questions": (clar.questions or []) if clar else [],
                    "answers": (clar.answers or {}) if clar else {},
                },
            },
            {"brd_document": {"type": "brd", "content": brd.content or ""}},
            [aid for aid in [canvas.id if canvas else None, clar.id if clar else None] if aid],
            [brd.id] if brd.id else [],
        )

    if cp == CheckpointId.BRD_TO_TECH_SPEC:
        brd = _latest(BRD)
        ts = _latest(TechSpec)
        if not ts:
            return None
        return (
            {"brd_document": {"type": "brd", "content": (brd.content or "") if brd else ""}},
            {"tech_spec_document": {"type": "tech_spec", "content": ts.content or ""}},
            [brd.id] if brd and brd.id else [],
            [ts.id] if ts.id else [],
        )

    if cp == CheckpointId.TECH_SPEC_TO_XSD:
        ts = _latest(TechSpec)
        from app.models.xsd import XSD
        xsd = _latest(XSD)
        if not xsd:
            return None
        return (
            {"tech_spec_document": {"type": "tech_spec", "content": (ts.content or "") if ts else ""}},
            {"xsd_assessment_decision": {
                "type": "xsd_assessment",
                "decision": xsd.decision if hasattr(xsd, "decision") else getattr(xsd, "status", None),
                "schema_content": getattr(xsd, "schema_content", None) or "",
            }},
            [ts.id] if ts and ts.id else [],
            [xsd.id] if xsd.id else [],
        )

    # Other checkpoints don't yet have a deterministic reverse-load path
    # (product_kit_to_phase_c_communication, phase_c_query_to_po_response).
    # The re-run endpoint returns 400 for those.
    return None


@router.post("/changes/{change_id}/eval/rerun")
async def rerun_eval(
    change_id: str,
    checkpoint: str = Query(..., description="Checkpoint id to re-evaluate"),
    db: DbDep = None,
    _: AdminUser = None,
):
    """Re-run the advisory evaluation for a checkpoint on an existing change.

    Useful after rubric or critic model changes — score the same artifacts
    against the latest configuration. Writes a fresh verdict row.
    """
    _get_change_or_404(db, change_id)
    try:
        cp = CheckpointId(checkpoint)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown checkpoint: {checkpoint}")

    materials = _reconstruct_artifacts_for_checkpoint(db, change_id, cp)
    if not materials:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot re-run {cp.value} for this change — required artifacts "
                "are not yet persisted, or this checkpoint does not support re-run."
            ),
        )

    source, target, source_ids, target_ids = materials
    from app.services.evaluation.runner import run_advisory
    row = await run_advisory(
        db=db,
        change_request_id=change_id,
        checkpoint_id=cp,
        source_artifacts=source,
        target_artifacts=target,
        source_artifact_ids=source_ids,
        target_artifact_ids=target_ids,
    )
    if not row:
        raise HTTPException(status_code=500, detail="Re-run produced no verdict")
    return {
        "verdict_id": row.id,
        "checkpoint_id": cp.value,
        "verdict": row.verdict,
        "passed": bool(row.passed),
    }


@router.post("/changes/{change_id}/eval/override", response_model=EvalOverrideResponse)
def override_eval_verdict(
    change_id: str,
    body: EvalOverrideRequest,
    db: DbDep = None,
    current_user: CurrentUser = None,
):
    _get_change_or_404(db, change_id)

    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="Override reason is required.")

    try:
        checkpoint = CheckpointId(body.checkpoint_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown checkpoint_id: {body.checkpoint_id}")

    contract = get_contract(checkpoint)
    role_value = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    is_admin = current_user.role == UserRole.ADMIN
    if (role_value not in set(contract.override_allowed_roles)) and not is_admin:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Role '{role_value}' cannot override checkpoint '{checkpoint.value}'. "
                f"Allowed roles: {contract.override_allowed_roles}"
            ),
        )

    if body.previous_verdict_id:
        previous = get_by_id(db, body.previous_verdict_id)
        if not previous:
            raise HTTPException(status_code=404, detail="previous_verdict_id not found.")
        if previous.change_request_id != change_id or previous.checkpoint_id != checkpoint.value:
            raise HTTPException(
                status_code=400,
                detail="previous_verdict_id does not belong to this change/checkpoint.",
            )
    else:
        previous = get_latest(db, change_id, checkpoint)
        if not previous:
            raise HTTPException(status_code=404, detail="No verdict found to override.")

    if str(previous.verdict).upper() == "PASS":
        raise HTTPException(status_code=400, detail="Cannot override a PASS verdict.")

    effective_policy = get_policy_mode(db, checkpoint, fallback=contract.policy_mode)
    override_row = save_override_verdict(
        db,
        previous_verdict=previous,
        override_actor=current_user.username,
        override_reason=body.reason.strip(),
        policy_mode=effective_policy,
    )
    if not override_row:
        raise HTTPException(status_code=500, detail="Failed to persist override verdict.")

    return {
        "change_request_id": change_id,
        "checkpoint_id": checkpoint.value,
        "override_verdict": override_row.to_dict(),
    }


