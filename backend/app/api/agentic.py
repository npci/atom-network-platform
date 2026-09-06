# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""REST + WebSocket surface for the agentic codegen pipeline (THE BOOK §13).

Makes the orchestrator invokable: start a run, poll/stream its events (the
durable `agentic_events` feed — §3), approve the exact manifest hash, cancel.
The heavy work runs in Celery (`agentic.drive` / `agentic.push`); these routes
only validate, persist, and dispatch.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, WebSocket
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.deps import DbDep, CurrentUser, AgenticUser, authenticate_ws
from app.agents import agentic_state as S
from app.agents import repo_scope
from app.agents.agentic_orchestrator import events_for
from app.models.agentic import AgenticRun, ChangeManifest
from app.models.change_request import ChangeRequest
from app.models.user import UserRole

logger = logging.getLogger("app.agentic")
router = APIRouter()


class StartAgenticRequest(BaseModel):
    repo_ids: list[str]
    intent: str = ""
    # Phase A/B split: "full" (legacy single run), "xsd" (Phase A), "code" (Phase B).
    kind: str = "full"


class ApproveRequest(BaseModel):
    manifest_hash: str
    # False = approve but DEFER the git push: the run completes, the change can move
    # on, and the branch is pushed later via POST /agentic/runs/{id}/push.
    push: bool = True
    # Push even when the adversarial review left a blocker-severity finding open. Default
    # False = the push is HARD-BLOCKED on an unresolved blocker (must be an explicit human act).
    override_blockers: bool = False
    # Required when override_blockers=True (Codex P2 audit fix). The reason is persisted as a
    # durable event so a compliance / post-incident review can trace WHO overrode WHAT and WHY.
    override_reason: str | None = None


def _change_or_404(db: Session, change_id: str) -> ChangeRequest:
    cr = db.get(ChangeRequest, change_id)
    if cr is None:
        raise HTTPException(404, "Change not found")
    return cr


def _run_or_404(db: Session, run_id: str) -> AgenticRun:
    run = db.get(AgenticRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


# Product/tech planning roles that collaborate on the Change-Analysis flow
# (clarifications + plan ratification). Analysis is multi-party (PM = functional,
# tech-lead = technical), so it is ROLE-gated, not maker-checker author-gated.
_ANALYSIS_ROLES = frozenset({
    UserRole.ADMIN, UserRole.TECH_LEAD, UserRole.PRODUCT_MANAGER, UserRole.PRODUCT_OWNER,
})


def _authz_analysis(current_user) -> None:
    """Gate for the collaborative Change-Analysis decisions (answer clarifications /
    ratify the plan). Open to the planning roles — NOT just the run's author — so the
    PM and tech-lead can both participate regardless of who started the run."""
    if getattr(current_user, "role", None) not in _ANALYSIS_ROLES:
        raise HTTPException(403, "only product/tech planning roles can drive the analysis")


def _can_read(run: AgenticRun, current_user) -> bool:
    """Boolean form of the read gate (shared by the single-run endpoints and the
    per-change LIST, so listing can't leak runs a single GET would 403)."""
    if getattr(run, "kind", None) == "analysis" and getattr(current_user, "role", None) in _ANALYSIS_ROLES:
        return True
    owner = getattr(run, "created_by", None)
    if owner is None:
        return True  # pre-authz run — role gate (admin/tech_lead) already applied upstream
    return current_user.role == UserRole.ADMIN or current_user.id == owner


def _authz_read(run: AgenticRun, current_user) -> None:
    """Ownership check for read endpoints: the run's AUTHOR or an ADMIN. Change-Analysis
    runs are collaborative, so any planning role may read them (PM needs the questions).
    Legacy runs (created_by is None) fall back to the role gate only."""
    if not _can_read(run, current_user):
        raise HTTPException(403, "access denied")


def _authz_write(run: AgenticRun, current_user) -> None:
    """Maker-checker: only the run's AUTHOR or an ADMIN may approve/push/cancel/
    mutate it. Reads stay open to the admin+tech_lead set (the AgenticUser gate).
    Legacy runs created before created_by existed (None) fall back to the role gate
    so in-flight runs aren't locked out across the deploy."""
    owner = getattr(run, "created_by", None)
    if owner is None:
        return  # pre-authz run — role gate (admin/tech_lead) already applied upstream
    if current_user.role == UserRole.ADMIN or current_user.id == owner:
        return
    raise HTTPException(403, "only the run's author or an admin can act on this run")


def _authz_resume(run: AgenticRun, current_user) -> None:
    """Who may retry a stalled/failed run. Change-Analysis runs are collaborative, so
    any planning role can retry one (matching the clarification/plan gates) — a PM
    whose analysis crashed shouldn't need an admin to un-stick it from the dead-end
    UI. Other kinds (xsd/code) keep the admin/tech-lead + maker-checker gate."""
    if getattr(run, "kind", None) == "analysis":
        _authz_analysis(current_user)
        return
    if current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD):
        raise HTTPException(403, "only admin or tech-lead can resume this run")
    _authz_write(run, current_user)


def _push_view(run: AgenticRun) -> dict:
    """Push status for the UI: pushed (branch on origin) vs push_deferred (approved,
    awaiting an explicit 'Push to git'). ``push_stale`` = the branch on git was pushed
    under an OLDER manifest than the one currently frozen (fix rounds after the push) —
    what the user sees in the panel is NOT what git holds until they push again."""
    from sqlalchemy.orm import object_session
    deferred = bool((getattr(run, "handoff_json", None) or {}).get("push_deferred"))
    pushed, stale = False, False
    s = object_session(run)
    if s is not None:
        from app.models.agentic import AgenticRunRepo
        rows = s.query(AgenticRunRepo).filter(
            AgenticRunRepo.run_id == run.id, AgenticRunRepo.push_state == "pushed").all()
        pushed = bool(rows)
        stale = any(r.pushed_manifest_hash is not None
                    and r.pushed_manifest_hash != run.manifest_hash for r in rows)
    return {"pushed": pushed, "push_deferred": deferred and not pushed, "push_stale": stale}


def _run_view(run: AgenticRun) -> dict:
    return {"run_id": run.id, "change_request_id": run.change_request_id,
            "phase": run.phase, "status": run.status, "manifest_hash": run.manifest_hash,
            "cancel_requested": run.cancel_requested, "error": run.error,
            "error_code": getattr(run, "error_code", None),
            "created_by": getattr(run, "created_by", None),
            **_push_view(run),
            "kind": getattr(run, "kind", "full"), "parent_run_id": getattr(run, "parent_run_id", None),
            # Repos chosen for this run — lets a later stage (e.g. the XSD page) carry the
            # planning/analysis run's repo selection through instead of re-asking.
            "selected_repo_ids": list(getattr(run, "selected_repo_ids", None) or []),
            # ⛔ a disruptive change the human explicitly accepted after being warned —
            # a permanent DANGER flag wherever this run is inspected.
            "accepted_risk": bool((getattr(run, "handoff_json", None) or {}).get("accepted_risk")),
            # Element-level schema diff (deterministic, from the frozen XsdScope):
            # {"repo_id:path": {new: [...], modified: [...], deprecated: [...]}} —
            # drives the PM-friendly "what changes in each schema" summary in the UI.
            "xsd_changes": (((getattr(run, "handoff_json", None) or {}).get("xsd_scope") or {})
                            .get("diff_record") or None),
            # Durable verification state (so the UI badge survives reload / REST-only views /
            # audit, instead of being reconstructed from event replay). `verified` is set when
            # the build passed; `verify_skipped` when the human skipped the gate. A change past
            # review with neither was never built-verified.
            "verified": bool((getattr(run, "handoff_json", None) or {}).get("verified")),
            "verify_skipped": bool((getattr(run, "handoff_json", None) or {}).get("verify_skipped")),
            # On-demand re-verification result (POST /reverify): {status, at, reason?,
            # gates?, modules_built?, modules_failed?}. None until first re-verify.
            "last_reverify": (getattr(run, "handoff_json", None) or {}).get("last_reverify")}


def _approved_xsd_run(db: Session, change_id: str) -> AgenticRun | None:
    """The Phase-A (kind='xsd') run for this change whose XSD manifest was approved —
    the parent a Phase-B (code) run continues from."""
    runs = db.scalars(
        select(AgenticRun)
        .where(AgenticRun.change_request_id == change_id, AgenticRun.kind == "xsd")
        .order_by(AgenticRun.created_at.desc())
    ).all()
    for r in runs:
        man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == r.id)
               .order_by(ChangeManifest.created_at.desc()).first())
        if man is not None and man.approved_at is not None:
            return r
    return None


@router.get("/agentic/preflight")
def agentic_preflight(current_user: CurrentUser):
    """Are all the dependencies for code gen in place? Returns ``ready`` + a list of
    plain-language ``problems`` (no worker / Redis / git / GITLAB_TOKEN / LLM key) so
    the UI can warn BEFORE the user starts a run that would otherwise hang."""
    from app.agents.codegen_preflight import check_dependencies
    problems = check_dependencies()
    return {"ready": not problems, "problems": problems}


@router.get("/agentic/health")
def agentic_health(db: DbDep, current_user: AgenticUser):
    """Ops snapshot of the agentic subsystem — active vs stuck runs, recent failures
    by code, and workspace disk headroom — so an operator can triage without DB
    spelunking. 'stuck' = active but no heartbeat for > 2 lease windows."""
    import shutil
    from datetime import timedelta
    from app.models.base import utcnow
    from app.models.agentic import AgenticStatus

    now = utcnow()
    stuck_after = timedelta(seconds=settings.agentic_lease_ttl_seconds * 2)
    active = db.query(AgenticRun).filter(AgenticRun.status == AgenticStatus.ACTIVE.value).all()
    stuck = [r for r in active
             if r.lease_owner and (now - (r.last_heartbeat_at or r.updated_at or now)) > stuck_after]
    recent_fail = (db.query(AgenticRun)
                   .filter(AgenticRun.status == AgenticStatus.FAILED.value)
                   .order_by(AgenticRun.updated_at.desc()).limit(20).all())
    by_code: dict[str, int] = {}
    for r in recent_fail:
        by_code[r.error_code or "unknown"] = by_code.get(r.error_code or "unknown", 0) + 1
    try:
        free_mb = shutil.disk_usage(settings.agentic_workspace_root).free // (1024 * 1024)
    except OSError:
        free_mb = None
    floor = getattr(settings, "agentic_min_disk_free_mb", 0) or 0
    return {
        "active_runs": len(active),
        "stuck_runs": len(stuck),
        "stuck_run_ids": [r.id for r in stuck],
        "recent_failures_by_code": by_code,
        "workspace_disk_free_mb": free_mb,
        "workspace_disk_low": (free_mb is not None and floor > 0 and free_mb < floor),
        "lease_ttl_seconds": settings.agentic_lease_ttl_seconds,
        "model": settings.claude_model,
    }


@router.get("/agentic/runs")
def list_runs(db: DbDep, current_user: AgenticUser, limit: int = 50):
    """Run history (newest first) so the user can revisit past code-gen runs — each
    row carries the intent (from its change_request) + status/phase/when."""
    from app.models.change_request import ChangeRequest
    rows = db.scalars(
        select(AgenticRun).order_by(AgenticRun.created_at.desc()).limit(max(1, min(limit, 200)))
    ).all()
    out = []
    for r in rows:
        cr = db.get(ChangeRequest, r.change_request_id) if r.change_request_id else None
        out.append({
            "run_id": r.id, "status": r.status, "phase": r.phase,
            "intent": (cr.title if cr else None) or "(no title)",
            "repos": len(r.selected_repo_ids or []),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "error": r.error,
        })
    return {"runs": out}


@router.post("/changes/{change_id}/agentic/start")
def start_agentic(change_id: str, body: StartAgenticRequest, db: DbDep, current_user: AgenticUser):
    """Start (or return the existing active) agentic run for a change. Validates
    that the selection is known + indexed (§5), then dispatches the driver."""
    if not settings.use_agentic_tool_loop:
        raise HTTPException(409, "Agentic codegen is disabled (use_agentic_tool_loop=false)")
    _change_or_404(db, change_id)

    kind = (body.kind or "full").lower()
    valid_kinds = ("full", "xsd", "code") + (("analysis",) if settings.use_change_analysis else ())
    if kind not in valid_kinds:
        raise HTTPException(400, f"kind must be one of {', '.join(valid_kinds)}")

    # Resolve the EFFECTIVE selection BEFORE validating it. A code (Phase B) run
    # continues an APPROVED Phase-A run and MUST span exactly the repos Phase A
    # approved — inherit the parent's selection rather than trusting the caller's
    # list (the UI fills it with EVERY registered repo, including un-indexed ones,
    # so validating the caller's list here would wrongly reject on a repo the user
    # never selected). The adopted workspace only holds the parent's repos anyway.
    parent_id = workspace_id = None
    repo_ids = list(body.repo_ids or [])
    if kind == "code":
        parent = _approved_xsd_run(db, change_id)
        if parent is None:
            raise HTTPException(409, "no approved Phase-A (XSD) run for this change — approve the XSDs first")
        parent_id = parent.id
        workspace_id = parent.workspace_run_id or parent.id
        if parent.selected_repo_ids:
            repo_ids = list(parent.selected_repo_ids)

    try:
        repo_scope.validate_selection(db, repo_ids)             # §5 hard gate → 400
    except repo_scope.RepoSelectionError as e:
        raise HTTPException(400, str(e))
    # Dependency preflight — tell the human EXACTLY what's broken (no worker / Redis /
    # git / GITLAB_TOKEN / LLM key) in plain language, instead of creating a run that
    # silently sits at "Getting ready" forever.
    from app.agents.codegen_preflight import assert_ready_or_message
    _pf = assert_ready_or_message()
    if _pf:
        logger.warning("codegen preflight BLOCKED start for change=%s:\n%s", change_id, _pf)
        raise HTTPException(503, _pf)

    run, created = S.create_run(db, change_id, repo_ids, kind=kind,
                                parent_run_id=parent_id, workspace_run_id=workspace_id,
                                created_by=current_user.id)
    db.commit()
    if created:
        from app.services.celery_tasks import agentic_drive_task
        agentic_drive_task.delay(run.id, body.intent)
    return {**_run_view(run), "created": created}


@router.post("/changes/{change_id}/agentic/rerun-code")
def rerun_code(change_id: str, body: StartAgenticRequest, db: DbDep, current_user: AgenticUser):
    """Re-run Phase B (code generation) from the APPROVED Phase A baseline as a FRESH run —
    for testing the agent's consistency across attempts.

    Each re-run resets the shared workspace to the EXACT Phase-A-approved state (pinned base
    SHA + approved XSDs, with NO carried-over Phase-B edits) and regenerates from scratch.
    Nothing is cached. The prior code run (if any) is cancelled to free the one-active-run
    slot, but its row/events/diff are kept so you can compare outputs run-to-run."""
    from app.models.agentic import AgenticStatus
    if not settings.use_agentic_tool_loop:
        raise HTTPException(409, "Agentic codegen is disabled (use_agentic_tool_loop=false)")
    _change_or_404(db, change_id)
    parent = _approved_xsd_run(db, change_id)
    if parent is None:
        raise HTTPException(409, "no approved Phase-A (XSD) run for this change — approve the XSDs first")

    # Free the one-active-run-per-change slot. A prior CODE run (usually parked at the approval
    # gate, or terminal) is cancelled so a fresh attempt can start; an active NON-code run means
    # Phase A is still going — refuse rather than disrupt it.
    active = S._active_run(db, change_id)
    if active is not None:
        from app.models.base import utcnow
        # A restart/crash leaves lease_owner set but the lease expired — that run is
        # driver-less, so treat it like a parked/lease-free run and cancel it.
        lease_dead = active.lease_owner is None or (
            active.lease_expires_at is not None and active.lease_expires_at < utcnow()
        )
        if (getattr(active, "kind", None) or "full") != "code":
            raise HTTPException(409, "another run is in progress for this change — wait for it to finish")
        if active.phase in {"awaiting_human_approval", "awaiting_verify_decision"} or lease_dead:
            try:
                S.mark_terminal(db, active, AgenticStatus.CANCELLED)
                db.commit()
            except Exception:  # noqa: BLE001 — illegal transition (e.g. mid-push)
                raise HTTPException(409, "the current code run can't be cancelled right now — cancel it, then re-run")
        else:
            raise HTTPException(409, "the current code run is still generating — cancel it or wait, then re-run")

    from app.agents.codegen_preflight import assert_ready_or_message
    _pf = assert_ready_or_message()
    if _pf:
        raise HTTPException(503, _pf)

    run, created = S.create_run(db, change_id, list(parent.selected_repo_ids or []), kind="code",
                                parent_run_id=parent.id,
                                workspace_run_id=parent.workspace_run_id or parent.id,
                                created_by=current_user.id)
    if not created:                                  # an active run reappeared (race) — don't no-op
        raise HTTPException(409, "a run is already active for this change; try again in a moment")
    # Flag the clean-slate reset for _adopt_parent_workspace (pinned base + approved XSDs).
    run.handoff_json = {"fresh_codegen": True}
    db.commit()
    from app.services.celery_tasks import agentic_drive_task
    agentic_drive_task.delay(run.id, body.intent)
    return {**_run_view(run), "created": True, "reran_from_parent": parent.id}


class EnsureAnalysisRequest(BaseModel):
    repo_ids: list[str] | None = None   # optional; defaults to role-flagged core+app repos
    restart: bool = False               # true → discard the existing analysis run and start fresh


@router.post("/changes/{change_id}/analysis/ensure")
def ensure_analysis(change_id: str, body: EnsureAnalysisRequest, db: DbDep, current_user: CurrentUser):
    """Auto-start the code-grounded Change-Analysis run for a change at the
    clarification stage (accuracy S2/S3 activation glue). Idempotent + flag-gated:
    - use_change_analysis OFF → {started:false, reason:'disabled'} (legacy clarification stays).
    - an analysis run already exists → return it (no duplicate)…
    - …UNLESS body.restart=true — then terminate the existing run and start a brand-new one
      from scratch (clean transcript/plan), reusing the same repo selection by default. This
      is the clarify page's "Restart from scratch" control.
    - else create + dispatch one, defaulting repos to the indexed role=core + role=app repos.
    Change author or admin/tech_lead may trigger it."""
    cr = _change_or_404(db, change_id)
    if (current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD)
            and cr.created_by != current_user.id):
        raise HTTPException(403, "access denied")
    if not settings.use_change_analysis:
        return {"started": False, "reason": "disabled"}

    existing = db.scalars(
        select(AgenticRun).where(AgenticRun.change_request_id == change_id, AgenticRun.kind == "analysis")
        .order_by(AgenticRun.created_at.desc())
    ).first()
    reuse_repos: list[str] = []
    if existing is not None:
        if not body.restart:
            return {"started": False, "reason": "exists", **_run_view(existing)}
        # Restart: free the one-active-run-per-change slot so a fresh run can be created.
        # A parked/driver-less run terminates immediately; a terminal one is already free.
        # Layered fallbacks so a phase/transition quirk can never 500 the restart button:
        # proper cancel → mark_terminal → direct status free (only goal is status != 'active').
        # Each level commits ON ITS OWN and rolls back on failure so the NEXT level runs on a
        # CLEAN transaction: an IntegrityError from a concurrent live-driver emit (a (run_id, seq)
        # clash) leaves Postgres in InFailedSqlTransaction, and every subsequent statement — the
        # fallbacks AND a shared trailing commit — would then fail too, 500-ing the very button
        # that is supposed to be un-500-able. The final direct-status level emits nothing.
        if existing.status == "active":
            try:
                S.honour_cancel(db, existing)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                try:
                    S.mark_terminal(db, existing, S.AgenticStatus.CANCELLED)
                    db.commit()
                except Exception:  # noqa: BLE001 — last resort: free the unique-active slot directly
                    db.rollback()
                    existing.status = S.AgenticStatus.CANCELLED.value
                    existing.phase = "cancelled"
                    existing.lease_owner = None
                    existing.lease_expires_at = None
                    db.commit()
        # Re-run with the SAME repos the operator originally chose (the default-picker could
        # otherwise land on different repos than the run they're re-testing).
        reuse_repos = list(existing.selected_repo_ids or [])

    repo_ids = list(body.repo_ids or []) or reuse_repos
    if not repo_ids:
        from app.models.code_repo import CodeRepo
        # Default to the first INDEXED core + INDEXED app repo (matches the picker's
        # default_repo_ids in analysis_repo_options) — never auto-pick an un-indexed
        # repo the user never selected.
        def _first_indexed(role: str):
            return db.scalars(
                select(CodeRepo).where(
                    CodeRepo.role == role,
                    CodeRepo.last_indexed_at.isnot(None),
                    CodeRepo.chunks_count > 0,
                ).order_by(CodeRepo.created_at)
            ).first()
        repo_ids = [r.id for r in (_first_indexed("core"), _first_indexed("app")) if r is not None]
    if not repo_ids:
        return {"started": False, "reason": "no_role_flagged_repos"}
    try:
        repo_scope.validate_selection(db, repo_ids)
    except repo_scope.RepoSelectionError as e:
        return {"started": False, "reason": f"repos_not_indexed: {e}"}

    # Dependency preflight — same guard /agentic/start has had all along. Without it
    # this endpoint created the run and dispatched blind, so a worker-host problem
    # (no worker, no consumer on the agentic queue, Redis auth, git/GITLAB_TOKEN, no
    # LLM key) produced a ZOMBIE run: the clarification panel spun on "Getting ready…"
    # with an empty event stream and no error anywhere the user could see it. Report
    # the cause in plain language instead, and do it BEFORE create_run so a broken
    # dependency doesn't burn the change's one-active-run slot on a run that can
    # never start.
    from app.agents.codegen_preflight import assert_ready_or_message
    _pf = assert_ready_or_message()
    if _pf:
        logger.warning("analysis preflight BLOCKED start for change=%s:\n%s", change_id, _pf)
        raise HTTPException(503, _pf)

    run, created = S.create_run(db, change_id, repo_ids, kind="analysis", created_by=current_user.id)
    db.commit()
    if created:
        from app.services.celery_tasks import agentic_drive_task
        agentic_drive_task.delay(run.id, cr.enhanced_prompt or cr.initial_prompt)
    return {"started": created, "reason": "created" if created else "exists", **_run_view(run)}


@router.get("/changes/{change_id}/analysis/repos")
def analysis_repo_options(change_id: str, db: DbDep, current_user: CurrentUser):
    """Indexed repos selectable for the Change-Analysis run + the default
    (role=core + role=app) pre-selection. Drives the clarification-stage repo
    picker so the codebase scope is CHOSEN at planning and carried downstream.
    enabled=false → the flow is off; the panel shows legacy clarification."""
    cr = _change_or_404(db, change_id)
    if (current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD)
            and cr.created_by != current_user.id):
        raise HTTPException(403, "access denied")
    if not settings.use_change_analysis:
        return {"enabled": False, "reason": "disabled", "repos": [], "default_repo_ids": []}
    from app.models.code_repo import CodeRepo
    repos = db.scalars(select(CodeRepo).order_by(CodeRepo.role, CodeRepo.created_at)).all()
    def _indexed(r):
        return r.last_indexed_at is not None
    out = [{"id": r.id, "label": r.label, "role": r.role,
            "indexed": _indexed(r), "chunks": r.chunks_count or 0} for r in repos]
    core = next((r for r in repos if r.role == "core" and _indexed(r)), None)
    app_ = next((r for r in repos if r.role == "app" and _indexed(r)), None)
    return {"enabled": True, "repos": out,
            "default_repo_ids": [r.id for r in (core, app_) if r is not None]}


@router.get("/changes/{change_id}/analysis")
def get_change_analysis(change_id: str, db: DbDep, current_user: CurrentUser):
    """Latest Change-Analysis plan for a change (accuracy S3). The PM-facing
    functional_plan + the technical_analysis (shown behind an expander) +
    ratification status. Change author or admin/tech_lead may read."""
    cr = _change_or_404(db, change_id)
    if (current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD)
            and cr.created_by != current_user.id):
        raise HTTPException(403, "access denied")
    from app.models.change_analysis import ChangeAnalysis
    ca = (db.query(ChangeAnalysis)
          .filter(ChangeAnalysis.change_request_id == change_id)
          .order_by(ChangeAnalysis.version.desc()).first())
    if ca is None:
        return {"exists": False}
    from app.services.change_collisions import cross_change_collisions
    # Surface the clarification Q&A (what the agent asked + the option the PM chose)
    # so the completed view can show the decisions the plan was built on.
    from app.services import decision_ledger as DL
    clarifications = [
        {"question": e.question, "options": e.options or [], "chosen": e.chosen}
        for e in DL.active_entries(db, change_id)
        if e.kind == "clarification"
    ]
    return {
        "exists": True, "id": ca.id, "version": ca.version, "status": ca.status,
        "functional_plan": ca.functional_plan or {},
        "technical_analysis": ca.technical_analysis or {},
        "flow_spec": ca.flow_spec or {},
        "pm_ratified": ca.pm_ratified_at is not None,
        "tech_ratified": ca.tech_ratified_at is not None,
        # S8 advisory: other open changes touching the same schema files.
        "collisions": cross_change_collisions(db, change_id),
        "clarifications": clarifications,
    }


def _revision_for(ca) -> dict | None:
    """The plan_revisions changelog entry that explains why this version exists."""
    revs = (ca.technical_analysis or {}).get("plan_revisions") or []
    for r in reversed(revs):
        if isinstance(r, dict) and r.get("version") == ca.version:
            return r
    return None


@router.get("/changes/{change_id}/analysis/versions")
def list_change_analysis_versions(change_id: str, db: DbDep, current_user: CurrentUser):
    """All plan versions for a change (newest first) — id/version/status/created_at
    plus the changelog entry explaining why each version exists. Powers the
    plan-version compare view."""
    cr = _change_or_404(db, change_id)
    if (current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD)
            and cr.created_by != current_user.id):
        raise HTTPException(403, "access denied")
    from app.models.change_analysis import ChangeAnalysis
    rows = (db.query(ChangeAnalysis)
            .filter(ChangeAnalysis.change_request_id == change_id)
            .order_by(ChangeAnalysis.version.desc()).all())
    return {"versions": [{
        "id": ca.id, "version": ca.version, "status": ca.status,
        "created_at": ca.created_at.isoformat() if ca.created_at else None,
        "revision": _revision_for(ca),
    } for ca in rows]}


@router.get("/changes/{change_id}/analysis/versions/{version}")
def get_change_analysis_version(change_id: str, version: int, db: DbDep, current_user: CurrentUser):
    """One specific plan version (same shape as the latest-plan endpoint) so the
    compare view can render previous vs current side by side."""
    cr = _change_or_404(db, change_id)
    if (current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD)
            and cr.created_by != current_user.id):
        raise HTTPException(403, "access denied")
    from app.models.change_analysis import ChangeAnalysis
    ca = (db.query(ChangeAnalysis)
          .filter(ChangeAnalysis.change_request_id == change_id,
                  ChangeAnalysis.version == version).first())
    if ca is None:
        return {"exists": False}
    return {
        "exists": True, "id": ca.id, "version": ca.version, "status": ca.status,
        "functional_plan": ca.functional_plan or {},
        "technical_analysis": ca.technical_analysis or {},
        "flow_spec": ca.flow_spec or {},
        "revision": _revision_for(ca),
    }


class QuickStartRequest(BaseModel):
    repo_ids: list[str]
    intent: str


@router.post("/agentic/quick-start")
def quick_start(body: QuickStartRequest, db: DbDep, current_user: AgenticUser):
    """Chat-the-intent entry: pick indexed repo(s) + describe the change, no BRD/TSD
    needed. Creates a lightweight change_request to anchor the run, validates the
    selection (§5), and dispatches the pipeline."""
    if not settings.use_agentic_tool_loop:
        raise HTTPException(409, "Agentic codegen is disabled (use_agentic_tool_loop=false)")
    if not body.intent.strip():
        raise HTTPException(400, "intent is required")
    try:
        repo_scope.validate_selection(db, body.repo_ids)
    except repo_scope.RepoSelectionError as e:
        raise HTTPException(400, str(e))
    from app.agents.codegen_preflight import assert_ready_or_message
    _pf = assert_ready_or_message()
    if _pf:
        logger.warning("codegen preflight BLOCKED quick-start:\n%s", _pf)
        raise HTTPException(503, _pf)

    cr = ChangeRequest(title=body.intent.strip()[:120], initial_prompt=body.intent.strip(),
                       created_by=current_user.id)
    db.add(cr)
    db.flush()
    run, _ = S.create_run(db, cr.id, body.repo_ids, created_by=current_user.id)
    db.commit()
    from app.services.celery_tasks import agentic_drive_task
    agentic_drive_task.delay(run.id, body.intent.strip())
    return {**_run_view(run), "change_request_id": cr.id, "created": True}


@router.get("/changes/{change_id}/agentic/runs")
def list_change_runs(change_id: str, db: DbDep, current_user: CurrentUser, kind: str | None = None):
    """Runs anchored to a change (newest first), optionally filtered by kind — lets the
    XSD (Phase A) and Phase-B pages restore/stream the current run after a reload."""
    q = select(AgenticRun).where(AgenticRun.change_request_id == change_id)
    if kind:
        q = q.where(AgenticRun.kind == kind)
    rows = db.scalars(q.order_by(AgenticRun.created_at.desc())).all()
    rows = [r for r in rows if _can_read(r, current_user)]   # don't list runs a GET would 403
    return {"runs": [{**_run_view(r), "created_at": r.created_at.isoformat() if r.created_at else None}
                     for r in rows]}


@router.get("/agentic/runs/{run_id}")
def get_run(run_id: str, db: DbDep, current_user: AgenticUser):
    run = _run_or_404(db, run_id)
    _authz_read(run, current_user)
    return _run_view(run)


def _diff_text(d) -> str:
    """Render a stored diff artifact to unified-diff text. Accepts both shapes: the
    legacy plain-string blob and the structured v2 ``{"files": [{patch, ...}]}``."""
    if isinstance(d, str):
        return d
    return "".join((f.get("patch") or "") for f in ((d or {}).get("files") or []))


@router.get("/agentic/runs/{run_id}/diff")
def get_diff(run_id: str, db: DbDep, current_user: AgenticUser):
    """The change-set per repo, as a durable artifact: the diff captured at freeze
    (stored on the manifest — survives workspace GC + the post-push commit), falling
    back to the LIVE workspace diff (incl new/untracked files) before the manifest
    is frozen. So the changes stay inspectable during AND long after the run.

    ``diffs`` is unified-diff TEXT per repo (patch previews may be bounded).
    ``stats`` is the exact per-file numbers per repo — ``{rid: {path: {op, add, del,
    truncated}}}`` — computed from the FULL diff at freeze, so the UI never derives
    counts (or the file list) from possibly-bounded text. Absent for legacy runs
    frozen before the structured artifact and for live pre-freeze reads."""
    run = _run_or_404(db, run_id)
    _authz_read(run, current_user)
    from app.agents import workspace_local
    from app.agents.platform_adapter import adapter
    man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run_id)
           .order_by(ChangeManifest.created_at.desc()).first())
    stored = (man.diffs if man else None) or {}
    # Once THIS run's manifest is frozen, its change-set is final: a repo absent from it
    # was NOT touched by this run. Crucially, the workspace is SHARED with the Phase-B
    # child, so reading it live would surface Phase B's code edits inside the Phase-A
    # view. So only fall back to the live workspace pre-freeze (no manifest yet).
    frozen = man is not None and bool(stored)
    ws_id = run.workspace_run_id or run_id     # Phase B reads Phase A's shared tree
    diffs: dict = {}
    stats: dict = {}
    served: dict = {}                          # per-repo source — logged so "what did the
    for rid in (run.selected_repo_ids or []):  # panel actually show?" is answerable later
        if stored.get(rid):
            d = stored[rid]
            diffs[rid] = _diff_text(d)          # durable artifact (preferred)
            served[rid] = "stored-v2" if isinstance(d, dict) else "stored-legacy"
            if isinstance(d, dict):
                stats[rid] = {f["path"]: {"op": f.get("op"), "add": f.get("add"),
                                          "del": f.get("del"),
                                          "truncated": bool(f.get("truncated"))}
                              for f in (d.get("files") or []) if f.get("path")}
            continue
        if frozen:
            diffs[rid] = "(no changes in this phase)"   # not in the frozen manifest → untouched here
            served[rid] = "frozen-untouched"
            continue
        rd = workspace_local.repo_dir(ws_id, rid)
        if not (rd / ".git").exists():
            diffs[rid] = "(workspace cleaned up — no stored diff)"
            served[rid] = "workspace-gone"
            continue
        served[rid] = "live-worktree"
        # Live (pre-freeze): include new/untracked files via intent-to-add; hide .lease,
        # build output (target/ …) and JAXB generated sources (incl. pom-declared output
        # dirs outside target/) so a repo that forgot to gitignore them doesn't surface
        # mvn-verify artifacts — same exclusion the frozen change-set applies.
        _excl = [f":(exclude,glob)**/{d}/**" for d in workspace_local._BUILD_OUTPUT_DIRS]
        _excl += [f":(exclude){p}" for p in workspace_local.jaxb_generated_prefixes(rd)]
        adapter.run_command(rd, ["git", "add", "-A", "-N"])
        # vs the recorded base (not HEAD): locally-committed agent work stays visible
        res = adapter.run_command(rd, ["git", "diff", workspace_local.recorded_base(ws_id, rid),
                                       "--", ".", ":(exclude).lease", ":(exclude).base_sha", *_excl])
        adapter.run_command(rd, ["git", "reset", "-q"])
        diffs[rid] = (res.stdout or "").strip() or "(no changes)"
    logger.info("diff_view: run=%s manifest=%s served=%s bytes=%s", run_id,
                (getattr(man, "manifest_hash", None) or "-")[:12], served,
                {r: len(v) for r, v in diffs.items()})
    return {"diffs": diffs, "stats": stats}


@router.get("/agentic/runs/{run_id}/xsd-files")
def get_xsd_files(run_id: str, db: DbDep, current_user: AgenticUser):
    """The changed/created **.xsd** schema files as frozen at the Phase-A handoff —
    the readable schema view + the downloadable set to share with partners. Durable
    past GC and push.

    Only `.xsd` is returned. The handoff blob also carries any non-schema Phase-A
    edits (e.g. a touched .java) used to rehydrate the workspace for Phase B, but
    those are not schema and must not appear in the download."""
    run = _run_or_404(db, run_id)
    _authz_read(run, current_user)
    files = ((getattr(run, "handoff_json", None) or {}).get("xsd_files") or [])
    return {"files": [{"path": f.get("path"), "content": f.get("content")}
                      for f in files
                      if (f.get("path") or "").lower().endswith(".xsd")]}


@router.get("/agentic/runs/{run_id}/workspace-zip")
def download_workspace_zip(run_id: str, db: DbDep, current_user: AgenticUser):
    """The run's generated code as a ZIP — the full working tree of every selected
    repo (e.g. ``network/`` + ``network-2.0/``), downloadable as soon as code gen parks at
    the approval gate and BEFORE any push, so a developer can inspect the agent's
    changes locally. The frozen per-repo diff is included at the archive root as
    ``<repo>.changes.diff``. 404s once the workspace is GC'd — the durable diff
    stays available via GET /diff."""
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    from app.agents import workspace_local
    from app.models.code_repo import CodeRepo
    run = _run_or_404(db, run_id)
    _authz_read(run, current_user)
    ws_id = run.workspace_run_id or run_id      # Phase B shares Phase A's tree
    names: dict[str, str] = {}
    for rid in (run.selected_repo_ids or []):
        repo = db.get(CodeRepo, rid)
        base = ((repo.gitlab_repo if repo else "").rstrip("/").rsplit("/", 1)[-1]) or rid
        names[rid] = base if base not in names.values() else f"{base}-{rid[:8]}"
    man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run_id)
           .order_by(ChangeManifest.created_at.desc()).first())
    diffs = (man.diffs if man else None) or {}
    extra = {f"{names[rid]}.changes.diff": _diff_text(d) for rid, d in diffs.items()
             if rid in names and _diff_text(d)}
    try:
        zip_path = workspace_local.export_zip(ws_id, names, extra_files=extra)
    except workspace_local.WorkspaceError as exc:
        raise HTTPException(404, str(exc))
    fname = f"agent-code-{(run.change_request_id or 'change')[:8]}-{run_id[:8]}.zip"
    return FileResponse(str(zip_path), media_type="application/zip", filename=fname,
                        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)))


@router.get("/agentic/runs/{run_id}/events")
def get_events(run_id: str, db: DbDep, current_user: CurrentUser, after_seq: int = -1):
    run = _run_or_404(db, run_id)
    _authz_read(run, current_user)
    return {"events": events_for(db, run_id, after_seq=after_seq)}


# Every transcript-producing section of the pipeline, in flow order, and where its material
# comes from. A section draws from either (or both):
#   stages — folders in the generic per-change capture tree (app/core/transcripts.py), which
#            covers the single-shot / streamed stages that have no AgenticRun at all;
#   kinds  — AgenticRun kinds, contributing the durable `agentic_events` feed plus that run's
#            08_codegen/<run_id>/ dump.
# `full` is the LEGACY combined run (xsd→code chained in one run, the old default for
# /agentic/start); its transcripts live under 08_codegen/<run_id>/ like any code run, so it is
# folded into the code-generation bucket rather than dropped — otherwise a change that used the
# legacy flow would export nothing. `other` catches 99_other/, the never-drop bucket for calls
# whose agent name isn't in the stage registry.
_TRANSCRIPT_SECTIONS: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    # key,                 zip prefix,             disk stage folders,                  run kinds
    ("prompt_enhancement", "01-prompt-enhancement", ("01_prompt_enhancement",),          ()),
    ("enrichment",         "02-enrichment",         ("02_enrichment",),                  ()),
    ("deep_research",      "03-deep-research",      ("03_deep_research",),               ()),
    ("canvas",             "04-canvas",             ("04_canvas",),                      ()),
    ("brd",                "05-brd",                ("05_brd",),                         ()),
    ("tech_spec",          "06-tech-spec",          ("06_tech_spec",),                   ()),
    ("product_kit",        "07-product-kit",        ("07b_circular", "07c_product_note"), ()),
    ("clarification",      "08-clarification",      (),                                  ("analysis",)),
    ("xsd",                "09-xsd-generation",     (),                                  ("xsd",)),
    ("planning",           "10-planning",           ("07_planning",),                    ()),
    ("code",               "11-code-generation",    (),                                  ("code", "full")),
    ("ea_review",          "12-ea-review",          (),                                  ("gov_ea",)),
    ("infosec_review",     "13-infosec-review",     (),                                  ("gov_is",)),
    ("other",              "99-other",              ("99_other",),                       ()),
]

_TRANSCRIPT_README = (
    "Transcript export\n"
    "=================\n\n"
    "One folder per pipeline section, numbered in flow order:\n"
    "  01-prompt-enhancement/  05-brd/             09-xsd-generation/   13-infosec-review/\n"
    "  02-enrichment/          06-tech-spec/       10-planning/         99-other/\n"
    "  03-deep-research/       07-product-kit/     11-code-generation/\n"
    "  04-canvas/              08-clarification/   12-ea-review/\n\n"
    "Sections driven by an agentic run (clarification, XSD, code generation, the two\n"
    "governance reviews) contain one <run_id>/ folder per run, holding:\n"
    "  events.jsonl   the durable agentic_events feed — always present. Secret-redacted;\n"
    "                 long turn/tool text is head-capped (~4000 chars).\n"
    "  transcripts/   full verbatim per-LLM-call dumps (system prompt, exact messages,\n"
    "                 tools, response). Present only when disk capture was enabled for the\n"
    "                 run; absent if capture was off or the tree was cleaned up.\n\n"
    "The remaining sections have no run of their own — they hold the verbatim per-LLM-call\n"
    "dumps directly, and are present only when disk capture was on at the time.\n\n"
    "A section with nothing recorded is omitted entirely. See manifest.json for per-section\n"
    "event / transcript-file counts.\n"
)


def _zip_tree(zf, root, prefix: str) -> int:
    """Add every file under `root` to `zf` beneath `prefix`. Returns the file count
    (0 when the directory doesn't exist — disk capture is best-effort, §transcripts)."""
    if not root.is_dir():
        return 0
    n = 0
    for f in sorted(root.rglob("*")):
        if f.is_file():
            zf.write(f, arcname=f"{prefix}/{f.relative_to(root).as_posix()}")
            n += 1
    return n


@router.get("/changes/{change_id}/agentic/transcripts-zip")
def download_transcripts_zip(change_id: str, db: DbDep, current_user: AgenticUser,
                             section: str | None = None):
    """Every recorded transcript for a change, across the whole pipeline, as one ZIP.

    Covers both transcript sources: the durable `agentic_events` feed of each agentic run
    (clarification, XSD, code generation, EA/InfoSec review) and the verbatim per-LLM-call
    disk dumps of the single-shot stages that have no run at all (prompt enhancement,
    enrichment, deep research, canvas, BRD, tech spec, planning, product kit).

    `?section=<key>` narrows the export to one section — the keys in `_TRANSCRIPT_SECTIONS`,
    which is what each stage page's download button passes."""
    import zipfile
    import tempfile
    import datetime as _dt
    from pathlib import Path
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    from app.core import transcripts as _t

    _change_or_404(db, change_id)
    sections = _TRANSCRIPT_SECTIONS
    if section:
        sections = [s for s in sections if s[0] == section]
        if not sections:
            raise HTTPException(400, f"Unknown transcript section '{section}'")

    kinds = {k for _, _, _, ks in sections for k in ks}
    by_kind: dict[str, list] = {}
    if kinds:
        rows = db.scalars(
            select(AgenticRun)
            .where(AgenticRun.change_request_id == change_id, AgenticRun.kind.in_(kinds))
            .order_by(AgenticRun.created_at.desc())
        ).all()
        for r in rows:
            if _can_read(r, current_user):   # never leak a run a GET would 403
                by_kind.setdefault(r.kind, []).append(r)

    cdir = _t.change_dir(change_id)
    manifest = {"change_id": change_id,
                "section": section or "all",
                "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "sections": []}
    found = 0
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for key, prefix, stages, kinds_ in sections:
                for kind in kinds_:
                    for run in by_kind.get(kind, []):
                        base = f"{prefix}/{run.id}"
                        evs = events_for(db, run.id)
                        zf.writestr(f"{base}/events.jsonl",
                                    "\n".join(json.dumps(e, default=str, ensure_ascii=False) for e in evs))
                        n_files = _zip_tree(zf, _t.run_transcript_dir(run.id, change_id=change_id),
                                            f"{base}/transcripts")
                        found += 1
                        manifest["sections"].append(
                            {"section": key, "phase": prefix, "kind": kind, "run_id": run.id,
                             "events": len(evs), "transcript_files": n_files,
                             "created_at": run.created_at.isoformat() if run.created_at else None})
                for stage in stages:
                    n_files = _zip_tree(zf, cdir / stage, f"{prefix}/{stage}")
                    if n_files:
                        found += 1
                        manifest["sections"].append(
                            {"section": key, "phase": prefix, "stage": stage,
                             "transcript_files": n_files})
            if not found:
                raise HTTPException(404, f"No transcripts recorded for {section or 'this change'}")
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("README.txt", _TRANSCRIPT_README)
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    tmp.close()
    fname = f"transcripts-{change_id[:8]}{'-' + section if section else ''}.zip"
    return FileResponse(tmp.name, media_type="application/zip", filename=fname,
                        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)))


# Human-readable names for the raw agent codenames the usage ledger stores in `section`,
# and the phase a section belongs to when the row has no explicit run `kind` (pre-codegen
# pipeline calls). Anything not mapped is Title-Cased ("some_agent" → "Some Agent"); only a
# genuinely missing name shows as "Unknown".
_SECTION_LABEL = {
    "prompt_enhancer": "Prompt Enhancer", "taxonomy": "Feature Taxonomy",
    "deep_researcher": "Deep Researcher", "enrichment": "Context Enrichment",
    "query_understanding": "Query Understanding", "canvas": "Product Canvas",
    "brd": "BRD Author", "tech_spec": "TSD Author", "xsd": "XSD Discovery",
    "code_change": "Code Generation", "adversarial": "Adversarial Review",
    "code_review": "Code Review", "is_review": "InfoSec Review", "code_planner": "Code Planner",
    "build_triager": "Build Triage", "change_walkthrough": "Dev/Tester Walkthrough",
    "cert_triage": "Cert Triage", "uat_triage": "UAT Triage", "self_correction": "Self-Correction",
    "json_recovery": "JSON Recovery", "version_change_summary": "Change Summary",
    "negotiation": "Negotiation", "escalation_advisor": "Escalation Advisor",
    "revision_planner": "Revision Planner", "product_kit": "Product Kit",
    "stuck_helper": "Recovery Helper", "stuck_helper_validator": "Recovery Validator",
    "approach_proposal": "Approach Proposal", "diff_stats_gate": "Completeness Gate",
    # Docgen pipeline, split per document (docgen_runner tags each run by its doc_type).
    "docgen_brd": "BRD", "docgen_tsd": "Tech Specification",
    "docgen_tech_spec": "Tech Specification", "docgen_circular": "Circular",
    "docgen_product_note": "Product Note", "docgen_product_kit": "Product Kit",
    "docgen_doc": "Document Generation", "brd_tier_classifier": "BRD Tier Classifier",
    # Governance review stages (pre-build EA/InfoSec gates).
    "gov_ea_review": "EA Governance Review", "gov_is_review": "InfoSec Governance Review",
    "gov_fix": "Governance Fixer",
}
# Section → workflow phase. Phases mirror the stepper the user sees (Prompt Enhancement →
# Deep Research → Product Canvas → Clarification → BRD → XSD Update → Tech Specification →
# Product Kit), plus the agentic Analysis/Code phases. Used to bucket a usage row by phase when
# it has no explicit run kind (the document-pipeline calls); codegen rows carry kind directly.
_SECTION_PHASE = {
    "prompt_enhancer": "prompt_enhancement",
    "taxonomy": "research", "deep_researcher": "research", "enrichment": "research",
    "query_understanding": "research",
    "canvas": "canvas",
    "question_generator": "clarification", "proposals_extractor": "clarification",
    "brd": "brd", "tech_spec": "tsd",
    "xsd": "xsd", "code_change": "code", "adversarial": "code", "code_review": "code",
    "code_planner": "code", "build_triager": "code", "change_walkthrough": "code",
    "uat_triage": "code", "self_correction": "code", "json_recovery": "support",
    "product_kit": "product_kit",
    # Docgen documents (section set by docgen_runner per doc_type) + the BRD tier classifier.
    "docgen_brd": "brd", "brd_tier_classifier": "brd",
    "docgen_tsd": "tsd", "docgen_tech_spec": "tsd",
    "docgen_circular": "product_kit", "docgen_product_note": "product_kit",
    "docgen_product_kit": "product_kit",
    # Certification track.
    "negotiation": "negotiation", "escalation_advisor": "negotiation",
    "revision_planner": "negotiation", "cert_triage": "cert_triage",
    # Governance stages (rows also carry the run kind — this covers kind-less calls).
    "gov_ea_review": "gov_ea", "gov_is_review": "gov_is", "gov_fix": "gov_ea",
}
# Phase labels match the workflow stepper exactly.
_PHASE_LABEL = {
    "prompt_enhancement": "Prompt Enhancement", "research": "Deep Research",
    "canvas": "Product Canvas", "clarification": "Clarification", "brd": "BRD",
    "analysis": "Analysis", "xsd": "XSD Update", "tsd": "Tech Specification",
    "code": "Code Generation", "full": "Code Generation",
    "gov_ea": "EA Review", "gov_is": "InfoSec Review",
    "product_kit": "Product Kit", "negotiation": "Negotiation",
    "cert_triage": "Cert Triage", "support": "Support", "other": "Other",
}
# Phase → top-level group for the Usage breakdown. Phase A is the design/spec pipeline
# (prompt → … → product kit), Phase B is build/delivery, Phase C is certification, and Cert
# Agent is the cert-triage agent. The agentic code-gen runs (analysis/xsd/code) live in
# Phase B but are ALSO surfaced on their own — see _AGENTIC_CODEGEN_PHASES.
_PHASE_GROUP = {
    "prompt_enhancement": "phase_a", "research": "phase_a", "canvas": "phase_a",
    "clarification": "phase_a", "brd": "phase_a", "xsd": "phase_a", "tsd": "phase_a",
    "product_kit": "phase_a",
    "analysis": "phase_b", "code": "phase_b", "full": "phase_b", "build": "phase_b",
    "gov_ea": "phase_b", "gov_is": "phase_b",
    "negotiation": "phase_c",
    "cert_triage": "cert",
    "support": "other", "other": "other",
}
_GROUP_LABEL = {"phase_a": "Phase A — Idea to Design", "phase_b": "Phase B — Build & Deliver",
                "phase_c": "Phase C — Certification", "cert": "Cert Agent", "other": "Other"}
_GROUP_ORDER = {"phase_a": 0, "phase_b": 1, "phase_c": 2, "cert": 3, "other": 4}


def _section_label(section: str | None) -> str:
    if not section:
        return "Unknown"
    return _SECTION_LABEL.get(section) or section.replace("_", " ").title()


def _phase_key(kind: str | None, section: str | None) -> str:
    """The phase a usage row belongs to: the run kind when set (codegen), else derived from
    the agent's section (pre-codegen pipeline), else 'other'."""
    if kind:
        return kind
    return _SECTION_PHASE.get(section or "", "other")


def _usage_rollup(records) -> dict:
    """Roll a list of LlmUsageRecord into {calls, tokens{...}, total_tokens, cost_usd,
    cost_complete}. cost_usd uses the stored per-row cost when present, else (re)prices via the
    cost table; an unpriced row flips cost_complete=False so partial $ is never shown as final."""
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    cost = 0.0
    cost_complete = True
    for r in records:
        tot["input"]       += r.input_tokens or 0
        tot["output"]      += r.output_tokens or 0
        tot["cache_read"]  += r.cache_read_tokens or 0
        tot["cache_write"] += r.cache_write_tokens or 0
        if r.cost_usd is not None:
            cost += r.cost_usd
        else:
            cost_complete = False
    total_tokens = tot["input"] + tot["output"] + tot["cache_read"] + tot["cache_write"]
    return {"calls": len(records), "tokens": tot, "total_tokens": total_tokens,
            "cost_usd": round(cost, 4), "cost_complete": cost_complete}


@router.get("/agentic/runs/{run_id}/usage")
def get_run_usage(run_id: str, db: DbDep, current_user: CurrentUser):
    """Per-run LLM token spend + estimated USD cost, from the `llm_usage_records` ledger."""
    from app.models.agentic import LlmUsageRecord
    run = _run_or_404(db, run_id)
    _authz_read(run, current_user)
    rows = db.query(LlmUsageRecord).filter(LlmUsageRecord.run_id == run_id).all()
    return {"run_id": run_id, **_usage_rollup(rows)}


def _require_usage_admin(current_user):
    """Cross-change usage views are operator-level (spend visibility)."""
    if current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD):
        raise HTTPException(403, "usage dashboard is restricted to admin / tech-lead")


@router.get("/agentic/usage/changes")
def usage_by_change(db: DbDep, current_user: CurrentUser):
    """Per-change LLM spend, split into two lists: ``changes`` are flow changes that went
    through the document pipeline (they produced a BRD), and ``codegen_changes`` were started
    directly from the Agentic Code Gen console (quick-start: straight to code, no prompt
    enhancement / BRD / flow). Operator-level."""
    _require_usage_admin(current_user)
    from app.models.agentic import LlmUsageRecord
    from app.models.change_request import ChangeRequest
    from app.models.brd import BRD
    rows = db.query(LlmUsageRecord).filter(LlmUsageRecord.change_request_id.isnot(None)).all()
    by_change: dict[str, list] = {}
    for r in rows:
        by_change.setdefault(r.change_request_id, []).append(r)
    cids = list(by_change.keys())
    titles = dict(db.query(ChangeRequest.id, ChangeRequest.title)
                  .filter(ChangeRequest.id.in_(cids)).all()) if cids else {}
    # A change that produced a BRD went through the document flow; one without a BRD was
    # started directly from the Agentic Code Gen console (quick-start) — code, no flow.
    flow_ids = {cid for (cid,) in db.query(BRD.change_request_id)
                .filter(BRD.change_request_id.in_(cids)).distinct().all()} if cids else set()
    flow, codegen = [], []
    for cid, rs in by_change.items():
        item = {"change_request_id": cid, "title": titles.get(cid) or cid[:8],
                "is_codegen": cid not in flow_ids, **_usage_rollup(rs)}
        (flow if cid in flow_ids else codegen).append(item)
    flow.sort(key=lambda x: (x["cost_usd"], x["total_tokens"]), reverse=True)
    codegen.sort(key=lambda x: (x["cost_usd"], x["total_tokens"]), reverse=True)
    return {"changes": flow, "codegen_changes": codegen, "grand_total": _usage_rollup(rows)}


@router.get("/agentic/usage/other")
def usage_other(db: DbDep, current_user: CurrentUser):
    """LLM spend NOT attributable to a change — the non-flow agents (ad-hoc calls, background
    jobs). Grouped by section (agent). Operator-level."""
    _require_usage_admin(current_user)
    from app.models.agentic import LlmUsageRecord
    rows = db.query(LlmUsageRecord).filter(LlmUsageRecord.change_request_id.is_(None)).all()
    by_section: dict[str, list] = {}
    for r in rows:
        by_section.setdefault(r.section or "", []).append(r)
    secs = [{"section": _section_label(s), **_usage_rollup(rs)} for s, rs in by_section.items()]
    secs.sort(key=lambda x: x["total_tokens"], reverse=True)
    return {**_usage_rollup(rows), "sections": secs}


@router.get("/agentic/usage/changes/{change_id}")
def usage_change_detail(change_id: str, db: DbDep, current_user: CurrentUser):
    """One change's spend broken down PER PHASE (run kind: analysis / xsd / code) → PER SECTION
    (agent). Readable by the change author or an operator."""
    from app.models.agentic import LlmUsageRecord
    cr = _change_or_404(db, change_id)
    if (current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD)
            and getattr(cr, "created_by", None) != current_user.id):
        raise HTTPException(403, "access denied")
    rows = db.query(LlmUsageRecord).filter(LlmUsageRecord.change_request_id == change_id).all()
    phases: dict[str, dict] = {}
    for r in rows:
        pk = _phase_key(r.kind, r.section)
        ph = phases.setdefault(pk, {"rows": [], "by_section": {}})
        ph["rows"].append(r)
        ph["by_section"].setdefault(r.section or "", []).append(r)
    out = []
    for k, ph in phases.items():
        secs = [{"section": _section_label(s), **_usage_rollup(rs)} for s, rs in ph["by_section"].items()]
        secs.sort(key=lambda x: x["total_tokens"], reverse=True)
        out.append({"phase": _PHASE_LABEL.get(k, k.title()), "phase_key": k,
                    **_usage_rollup(ph["rows"]), "sections": secs})
    # Order mirrors the workflow stepper: Prompt Enhancement → Deep Research → Product Canvas →
    # Clarification → BRD → (Analysis) → XSD Update → Tech Specification → Code Generation →
    # Product Kit, then certification, support/other.
    order = {"prompt_enhancement": 0, "research": 1, "canvas": 2, "clarification": 3, "brd": 4,
             "analysis": 5, "xsd": 6, "tsd": 7, "code": 8, "full": 8,
             "gov_ea": 8.4, "gov_is": 8.5, "product_kit": 9,
             "negotiation": 10, "cert_triage": 11, "support": 12, "other": 13}
    out.sort(key=lambda x: order.get(x["phase_key"], 6))

    # Group the ordered phases into Phase A / Phase B / Phase C / Cert Agent (+ Other) — the
    # flow view the dashboard renders. Group totals partition the change spend (each phase is in
    # exactly one group).
    phase_rows = {k: ph["rows"] for k, ph in phases.items()}
    groups_map: dict[str, dict] = {}
    for p in out:
        gk = _PHASE_GROUP.get(p["phase_key"], "other")
        g = groups_map.setdefault(gk, {"phases": [], "rows": []})
        g["phases"].append(p)
        g["rows"].extend(phase_rows.get(p["phase_key"], []))
    groups = [{"group": gk, "label": _GROUP_LABEL.get(gk, gk.title()),
               **_usage_rollup(g["rows"]), "phases": g["phases"]} for gk, g in groups_map.items()]
    groups.sort(key=lambda x: _GROUP_ORDER.get(x["group"], 9))

    return {"change_request_id": change_id, "title": cr.title, **_usage_rollup(rows),
            "phases": out, "groups": groups}


def _unresolved_blockers(man) -> list:
    """Must-not-ship findings the adversarial review left open on this manifest (empty when none).
    The push gate refuses to ship these unless a human explicitly overrides. Uses the SAME
    must-block rule as _phase_review (blocker-severity OR sensitive category) so the gate's
    enforcement matches the has_blocker flag that set it."""
    rev = (getattr(man, "review", None) or {})
    if not rev.get("has_blocker"):
        return []
    from app.agents.agentic_orchestrator import is_must_block
    return [i for i in (rev.get("items") or [])
            if is_must_block(i.get("category"), i.get("severity"))]


def _blocker_block_detail(blk: list) -> str:
    head = blk[0] if blk else {}
    where = f"{(head.get('file') or '?').split('/')[-1]}:{head.get('line') or '?'}"
    return (f"⛔ Push blocked — {len(blk)} unresolved blocker-severity review finding(s) "
            f"(e.g. {where}: {(head.get('why') or '')[:140]}). Resolve it (start over / retry) "
            f"or re-approve with override_blockers=true to push anyway.")


def _approve_governance_stage(db, run, body: "ApproveRequest", current_user):
    """Approval of a governance stage's staged fixes (user decision #4). Same
    blocker-override contract as the codegen gate; delivery depends on the parent:
    parent pushed → fix commits push to the SAME feature branch (via push_run's
    governance route); parent push-deferred → record only, the deferred final push
    carries the fixes (overlay). Zero-fix parks complete immediately either way."""
    from app.agents import governance_orchestrator as G
    from app.agents.agentic_events import emit_event
    from app.models.agentic import AgenticRunRepo
    # Replay guard (stale tab / HTTP retry): the stage already completed — running
    # the approval again would re-finish it and chain_next_stage would spawn a
    # DUPLICATE next-stage run (create_run dedups only ACTIVE runs), flipping the
    # change's all_passed back to false and re-locking Build.
    if run.status == "completed":
        return {**_run_view(run), "approved": True, "already_completed": True}
    if run.status != "active":
        raise HTTPException(409, f"stage run is {run.status} — restart governance reviews to retry")
    man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run.id)
           .order_by(ChangeManifest.created_at.desc()).first())
    parent_id = ((run.handoff_json or {}).get("governance") or {}).get("parent_run_id") or run.parent_run_id
    parent_pushed = (db.query(AgenticRunRepo.run_id)
                     .filter(AgenticRunRepo.run_id == parent_id,
                             AgenticRunRepo.push_state == "pushed").first() is not None)
    if not body.push and parent_pushed and (man.operations or []):
        # Stage delivery is parent-dependent by design (decision #4): on a pushed
        # parent the fixes go out as commits at approval — there is no later
        # stage-push path. Refuse loudly rather than silently ignoring the flag.
        raise HTTPException(400,
            "push=false is not supported for a governance stage whose parent is already "
            "pushed — approving delivers the fix commits to the feature branch (use the "
            "default push=true)")
    blk = _unresolved_blockers(man)
    if blk and not body.override_blockers:
        raise HTTPException(409, _blocker_block_detail(blk))
    if blk and body.override_blockers:
        reason = (body.override_reason or "").strip()
        if len(reason) < 8:
            raise HTTPException(400,
                "override_reason is required when override_blockers=true (min 8 chars) — "
                "this is captured for compliance audit.")
        emit_event(db, run.id, "blocker_override",
                   {"by": getattr(current_user, "id", None), "via": "governance_approve",
                    "count": len(blk), "reason": reason[:1000],
                    "blockers": [{"file": b.get("file"), "line": b.get("line"),
                                  "category": b.get("category"),
                                  "why": (b.get("why") or "")[:200]} for b in blk[:10]],
                    "action": f"⚠ GOVERNANCE OVERRIDE — {len(blk)} unresolved finding(s) waived; reason recorded"})
        h = dict(run.handoff_json or {})
        gov = dict(h.get("governance") or {})
        gov["overridden"] = {"by": getattr(current_user, "id", None),
                             "reason": reason[:1000], "count": len(blk)}
        h["governance"] = gov
        run.handoff_json = h
    if not parent_pushed or not (man.operations or []):
        # No remote write to make (deferred parent, or nothing staged): record + chain.
        db.commit()
        G.approve_deferred_stage(db, run)
        return {**_run_view(run), "approved": True, "push_deferred": not parent_pushed}
    db.commit()
    from app.services.celery_tasks import agentic_push_task
    agentic_push_task.delay(run.id)
    return {**_run_view(run), "approved": True}


@router.post("/agentic/runs/{run_id}/approve")
def approve_run(run_id: str, body: ApproveRequest, db: DbDep, current_user: AgenticUser):
    """Approve the EXACT frozen manifest hash (§11/§12). ``push=True`` (default)
    dispatches the guarded push immediately; ``push=False`` completes the run with
    the push DEFERRED — the change moves on, the workspace is GC-guarded, and the
    branch is pushed later via POST /agentic/runs/{id}/push."""
    from app.agents import manifest as M
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if not M.approve(db, run_id, body.manifest_hash, current_user.id):
        raise HTTPException(409, "manifest_hash does not match the frozen manifest (stale or tampered)")
    if (run.kind or "").startswith("gov_"):
        return _approve_governance_stage(db, run, body, current_user)
    if not body.push:
        from app.agents.agentic_events import emit_event
        from app.models.agentic import AgenticStatus
        run.handoff_json = {**(getattr(run, "handoff_json", None) or {}), "push_deferred": True}
        S.mark_terminal(db, run, AgenticStatus.COMPLETED)
        emit_event(db, run.id, "approved_push_deferred",
                   {"action": "✓ Approved — git push deferred (push anytime from this page)"})
        db.commit()
        return {**_run_view(run), "approved": True}
    # Hard-block the push on an unresolved blocker-severity review finding — pushing one is an
    # explicit human act (override_blockers=true), never the silent default. Raising before the
    # commit leaves the approval un-persisted, so the run stays at the gate for the human.
    man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run_id)
           .order_by(ChangeManifest.created_at.desc()).first())
    blk = _unresolved_blockers(man)
    if blk and not body.override_blockers:
        raise HTTPException(409, _blocker_block_detail(blk))
    if blk and body.override_blockers:
        # Codex P2 audit fix: a blocker override is a compliance event — require an explicit
        # written reason and persist it durably so post-incident review can trace who/what/why.
        reason = (body.override_reason or "").strip()
        if len(reason) < 8:
            raise HTTPException(400,
                "override_reason is required when override_blockers=true (min 8 chars) — "
                "this is captured for compliance audit.")
        from app.agents.agentic_events import emit_event
        emit_event(db, run_id, "blocker_override",
                   {"by": getattr(current_user, "id", None),
                    "via": "approve", "count": len(blk),
                    "reason": reason[:1000],
                    "blockers": [{"file": b.get("file"), "line": b.get("line"),
                                  "category": b.get("category"),
                                  "why": (b.get("why") or "")[:200]} for b in blk[:10]],
                    "action": f"⚠ BLOCKER OVERRIDE — {len(blk)} unresolved blocker(s) pushed; reason recorded"})
    db.commit()
    from app.services.celery_tasks import agentic_push_task
    agentic_push_task.delay(run_id)
    return {**_run_view(run), "approved": True}


@router.post("/agentic/runs/{run_id}/push")
def push_run_now(run_id: str, db: DbDep, current_user: AgenticUser,
                 override_blockers: bool = False, override_reason: str | None = None):
    """Push an APPROVED run's branch now — the deferred half of 'Approve — push
    later'. Valid whenever the manifest is approved and nothing has been pushed yet;
    re-opens the completed run for its single remote write (COMPLETED → PUSHING)."""
    from app.agents.governance_orchestrator import STAGES as _GOV_STAGES, is_governance_kind
    from app.models.agentic import AgenticRunRepo
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run_id)
           .order_by(ChangeManifest.created_at.desc()).first())
    if man is None or man.approved_at is None:
        raise HTTPException(409, "manifest not approved — approve the changes first")
    if is_governance_kind(run.kind) and run.status != "active":
        # A stage's remote write happens exactly once, at approval (pushed parent →
        # push_stage_fixes) or via the parent's deferred-push overlay. Re-opening a
        # COMPLETED stage here would create the feature branch holding ONLY the fix
        # commit — bricking the parent's own deferred push against the brand-new-
        # branch guard — and _finish_stage would chain a DUPLICATE next stage.
        # An ACTIVE parked stage stays pushable: that is the retry for an approved
        # stage whose dispatched push was lost.
        raise HTTPException(409, "governance stage already finished — its fixes are delivered at "
                                 "approval, or by the parent run's deferred push (overlay)")
    # A running governance stage edits this run's shared workspace — dispatching the
    # deferred push mid-stage would race those edits AND violate the one-active-run
    # constraint when this run flips back to status=active below. Flag-gated like
    # the phase_b Build gate: with governance disabled the UI cannot approve or
    # cancel an orphaned stage, so this 409 must not outlive the feature. Excludes
    # the run being pushed — a parked stage retrying its own approved push is not
    # racing itself.
    if getattr(settings, "governance_reviews_enabled", False):
        _active_gov = (db.query(AgenticRun)
                       .filter(AgenticRun.change_request_id == run.change_request_id,
                               AgenticRun.kind.in_(tuple(_GOV_STAGES)),
                               AgenticRun.status == "active",
                               AgenticRun.id != run_id).first())
        if _active_gov is not None:
            raise HTTPException(409, "a governance review stage is in progress — push after it finishes "
                                     f"({_active_gov.kind} run {_active_gov.id[:8]} is {_active_gov.phase})")
    # Same hard gate as /approve: an unresolved blocker-severity finding can't be pushed silently.
    blk = _unresolved_blockers(man)
    if blk and not override_blockers:
        raise HTTPException(409, _blocker_block_detail(blk))
    if blk and override_blockers:
        reason = (override_reason or "").strip()
        if len(reason) < 8:
            raise HTTPException(400,
                "override_reason is required when override_blockers=true (min 8 chars) — "
                "this is captured for compliance audit.")
        from app.agents.agentic_events import emit_event
        emit_event(db, run_id, "blocker_override",
                   {"by": getattr(current_user, "id", None),
                    "via": "push", "count": len(blk), "reason": reason[:1000],
                    "blockers": [{"file": b.get("file"), "line": b.get("line"),
                                  "category": b.get("category"),
                                  "why": (b.get("why") or "")[:200]} for b in blk[:10]],
                    "action": f"⚠ BLOCKER OVERRIDE — {len(blk)} unresolved blocker(s) pushed; reason recorded"})
    # "Already pushed" only counts when git holds the CURRENT approved content. A repo
    # pushed under an older manifest (re-frozen after fix rounds) is STALE — the whole
    # point of this endpoint is to publish the state the human just approved, so let
    # the push proceed and _push_all re-push the stale repos to a fresh branch.
    pushed_rows = db.query(AgenticRunRepo).filter(AgenticRunRepo.run_id == run_id,
                                                  AgenticRunRepo.push_state == "pushed").all()
    stale = any(r.pushed_manifest_hash is not None
                and r.pushed_manifest_hash != man.manifest_hash for r in pushed_rows)
    logger.info("push_now: run=%s manifest=%s pushed_repos=%d stale=%s → %s",
                run_id, man.manifest_hash[:12], len(pushed_rows), stale,
                "reject (current already on git)" if pushed_rows and not stale
                else "dispatch re-push of stale branch" if stale else "dispatch first push")
    if pushed_rows and not stale:
        raise HTTPException(409, "already pushed — git already holds the current approved changes")
    if run.status == "active" and run.lease_owner is not None:
        raise HTTPException(409, "run is busy — try again shortly")
    if run.phase not in ("completed", "pushing", "rebase_reverify", "awaiting_human_approval"):
        raise HTTPException(409, f"run is not pushable from phase={run.phase}")
    run.status = "active"
    run.lease_owner = None
    run.lease_expires_at = None
    db.commit()
    from app.agents.agentic_events import emit_event
    emit_event(db, run.id, "push_requested", {"action": "⬆ Push to git requested"})
    db.commit()
    from app.services.celery_tasks import agentic_push_task
    agentic_push_task.delay(run_id)
    return {**_run_view(run), "push_dispatched": True}


def _apply_plan_supersession(db, run, pend: dict, current_user) -> None:
    """The human approved schemas that supersede the ratified reuse/extend approach
    (comply-first refine: they requested new schema, the agent applied it, and approval of
    the frozen manifest IS the approval of the plan update announced at review time).

    Ordering is deliberate: (1) the OPERATIVE artifact — the rewritten approach decision
    Phase B inherits — is committed FIRST, which also clears the pending flag so a retried
    approval can never double-roll the plan; (2) the plan-version roll is best-effort
    AFTER it — its fail-path rollback (plan_versioning) can then no longer erase anything
    (the manifest approval and the handoff rewrite are already durable)."""
    from app.models.base import utcnow
    from app.agents.agentic_events import emit_event
    # New whole files AND new message elements landed inside existing bundled schemas —
    # both are the approved contract the rewritten directive must name.
    files = ", ".join([p.rsplit("/", 1)[-1] for p in (pend.get("new_files") or [])]
                      + list(pend.get("new_messages") or []))
    prior_label = pend.get("prior_title") or pend.get("prior_approach") or "reuse"
    # One option record, used for BOTH the handoff and the plan changelog — the version
    # history and the directive Phase B inherits must describe the same decision.
    opt = {"id": "refine-supersession", "approach": "new",
           "title": f"New schema approved at XSD review (supersedes '{prior_label}')",
           "target_api": files,
           "how_it_fits": f"The human requested at XSD review: {pend.get('requested')}"}
    h = dict(run.handoff_json or {})
    ad = dict(h.get("approach_decision") or {})
    ad["superseded_option"] = ad.get("option")
    ad["option"] = opt
    ad["approach"] = "new"
    # The supersession only fires from a reuse/extend decision — i.e. the gate offered a "new
    # API" option and the human rejected it, so it is sitting in `rejected`. Phase B renders
    # BOTH the directive and the rejected list (_approach_block), so leaving it there tells the
    # code agent to implement the new API and, three lines later, never to implement it. Retire
    # the now-chosen path from the rejected list; keep the rest, and keep the original on record.
    _rej = [o for o in (ad.get("rejected") or []) if isinstance(o, dict)]
    if _rej:
        ad["superseded_rejected"] = _rej
        ad["rejected"] = [o for o in _rej if (o.get("approach") or "").lower() != "new"]
    # The gate evidence justified the SUPERSEDED choice; it no longer grounds this decision.
    if ad.get("evidence"):
        ad["superseded_evidence"] = ad.pop("evidence")
    ad["directive"] = (f"The human requested and APPROVED new schema at the XSD review, superseding the "
                       f"earlier '{pend.get('prior_approach')}' decision — implement against the approved "
                       "schemas, including the new API path; the old 'do not create a new API' rule no "
                       "longer applies.")
    ad["superseded_at_review"] = {"at": utcnow().isoformat(),
                                  "approved_by": getattr(current_user, "id", None)}
    h["approach_decision"] = ad
    h.pop("pending_plan_supersession", None)
    run.handoff_json = h
    db.commit()
    new_v = None
    try:
        from app.agents.plan_versioning import record_approach_decision_version
        new_v = record_approach_decision_version(
            db, change_request_id=run.change_request_id, run_id=run.id,
            chosen={**opt,
                    "divergence_note": (f"At the XSD review the human requested: {pend.get('requested')} "
                                        f"— applied and approved with the frozen manifest ({files}).")},
            decided_by=getattr(current_user, "id", None), kind="refine_supersession")
    except Exception as e:  # noqa: BLE001 — versioning is best-effort; the approval must stand
        logger.warning("refine-supersession plan version failed for %s: %s", run.change_request_id, e)
    emit_event(db, run.id, "plan_revised",
               {"version": new_v, "diverges": True,
                "action": (f"📝 Plan updated{f' to v{new_v}' if new_v else ''} — the approved XSDs "
                           f"supersede the earlier '{pend.get('prior_approach')}' approach: "
                           f"{files or 'new schema'} now part of the plan")})


@router.post("/agentic/runs/{run_id}/approve-xsd")
def approve_xsd(run_id: str, body: ApproveRequest, db: DbDep, current_user: AgenticUser):
    """Approve a Phase-A (XSD) run's frozen schema manifest — the schema-review gate.

    Unlike ``/approve`` this does NOT push: it completes Phase A (its workspace is
    retained for Phase B via the GC parent/child guard) so Phase B can generate code
    against the approved XSDs and raise the single combined MR. The change-flow stage
    advance is driven separately by the XSD page's existing 'Complete Stage' action."""
    from app.agents import manifest as M
    from app.models.agentic import AgenticStatus
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if getattr(run, "kind", "full") != "xsd":
        raise HTTPException(409, "not a Phase-A (XSD) run")
    if run.phase != "awaiting_xsd_approval":
        raise HTTPException(409, f"run is not awaiting XSD approval (phase={run.phase})")
    # Fail-closed on a schema that failed its authoritative build (JAXB generate/compile):
    # approving it hands Phase B a non-compiling contract. Override is an explicit,
    # reason-logged human act (mirrors the push gate's blocker override).
    if (run.handoff_json or {}).get("xsd_build_failed"):
        if not body.override_blockers:
            raise HTTPException(409, "the generated schema failed its authoritative build (JAXB "
                                "generate/compile) — fix it via request-xsd-changes, or re-approve "
                                "with override_blockers=true to accept a non-building schema.")
        reason = (body.override_reason or "").strip()
        if len(reason) < 8:
            raise HTTPException(400, "override_reason is required when override_blockers=true (min 8 "
                                "chars) — this is captured for compliance audit.")
        from app.agents.agentic_events import emit_event
        emit_event(db, run.id, "xsd_build_override",
                   {"by": getattr(current_user, "id", None), "reason": reason[:1000],
                    "action": "⚠ XSD BUILD OVERRIDE — approved a schema that does not build; reason recorded"})
    if not M.approve(db, run_id, body.manifest_hash, current_user.id):
        raise HTTPException(409, "manifest_hash does not match the frozen XSD manifest (stale or tampered)")
    # Make the human's approval DURABLE before any best-effort plan work below: M.approve
    # only flushes, and the plan roll's fail-path rollback (plan_versioning) must never be
    # able to erase the approval that was just made.
    db.commit()
    # Approval of the manifest is also the approval of a pending plan supersession (the
    # human was told the plan delta at review time via plan_supersession_pending).
    pend = (run.handoff_json or {}).get("pending_plan_supersession")
    if pend:
        _apply_plan_supersession(db, run, pend, current_user)
    S.mark_terminal(db, run, AgenticStatus.COMPLETED)
    db.commit()
    return {**_run_view(run), "approved": True, "phase_a_complete": True}


@router.post("/agentic/runs/{run_id}/decide-tsd-approval")
def decide_tsd_approval(run_id: str, db: DbDep, current_user: CurrentUser):
    """Resume a run parked at AWAITING_TSD_APPROVAL (ADR-0005 / SDLC review gap 4).

    Re-checks the change's latest TechSpec status: if it is now APPROVED (e.g. the
    TSD was regenerated, which auto-approves per `agentic_tsd_auto_approve_on_generate`,
    or a human explicitly approved it via a future TSD approval UI), the run's
    ``tsd_version_locked`` is set and it resumes at CODE_CHANGE. If the TSD is still
    not approved, returns 409 with the current status rather than silently no-op-ing,
    so the caller knows exactly what is blocking it."""
    from app.agents.agentic_events import emit_event
    from app.agents.agentic_orchestrator import _latest_tsd
    from app.models.research import ArtifactStatus
    from app.services.celery_tasks import agentic_drive_task
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if run.phase != "awaiting_tsd_approval":
        raise HTTPException(409, f"run is not at the TSD approval gate (phase={run.phase})")
    ts = _latest_tsd(db, run.change_request_id)
    if ts is None or ts.status != ArtifactStatus.APPROVED:
        raise HTTPException(
            409,
            f"the TSD is still not approved (status={ts.status.value if ts else 'none'}) — "
            "approve/regenerate the TSD, then retry this endpoint.",
        )
    # CAS — same double-click guard as decide-verify.
    claimed = db.query(AgenticRun).filter(
        AgenticRun.id == run_id,
        AgenticRun.phase == "awaiting_tsd_approval",
    ).update({
        AgenticRun.status: "active",
        AgenticRun.phase: "code_change",
        AgenticRun.tsd_version_locked: ts.version,
        AgenticRun.cancel_requested: False,
        AgenticRun.lease_owner: None,
        AgenticRun.lease_expires_at: None,
    }, synchronize_session=False)
    if not claimed:
        db.rollback()
        raise HTTPException(409, "TSD approval gate already decided")
    db.refresh(run)
    emit_event(db, run.id, "tsd_approved_resumed",
               {"tsd_version": ts.version,
                "action": f"✅ TSD v{ts.version} approved — resuming code generation"})
    emit_event(db, run.id, "phase_changed", {"from": "awaiting_tsd_approval", "to": "code_change"})
    db.commit()
    agentic_drive_task.delay(run.id, "")
    return {**_run_view(run), "resumed": True, "tsd_version_locked": ts.version}


class DecideVerifyRequest(BaseModel):
    action: str = "retry"             # "retry" | "skip"


@router.post("/agentic/runs/{run_id}/decide-verify")
def decide_verify(run_id: str, body: DecideVerifyRequest, db: DbDep, current_user: CurrentUser):
    """Human decision at the verification gate (reached after 3 failed auto-verifications):

      - ``retry``: run ONE more code-change → verify cycle; if it still fails the run
        re-parks here (so each manual continue is exactly one more attempt).
      - ``skip``:  accept the change UNVERIFIED and proceed to review → approval.

    Open to the planning roles (PM included) — the same people who drove the analysis can
    decide to retry or accept-unverified, not only admin/tech-lead.
    """
    from app.agents.agentic_events import emit_event
    from app.services.celery_tasks import agentic_drive_task
    run = _run_or_404(db, run_id)
    _authz_analysis(current_user)
    if run.phase != "awaiting_verify_decision":
        raise HTTPException(409, f"run is not at the verification gate (phase={run.phase})")
    action = (body.action or "retry").strip().lower()
    if action not in ("retry", "skip"):
        raise HTTPException(400, "action must be 'retry' or 'skip'")

    target_phase = "code_change" if action == "retry" else "review"
    # CAS — atomically claim the gate so a rapid double-click can't dispatch two drive
    # tasks: the conditional UPDATE flips phase only WHILE still at the gate, and the
    # loser (rowcount 0) is rejected. (The drive task's lease is the execution backstop;
    # this additionally prevents the duplicate dispatch + double phase_changed event.)
    claimed = db.query(AgenticRun).filter(
        AgenticRun.id == run_id,
        AgenticRun.phase == "awaiting_verify_decision",
    ).update({
        AgenticRun.status: "active",
        AgenticRun.phase: target_phase,
        AgenticRun.cancel_requested: False,
        AgenticRun.lease_owner: None,
        AgenticRun.lease_expires_at: None,
    }, synchronize_session=False)
    if not claimed:
        db.rollback()
        raise HTTPException(409, "verification gate already decided")
    db.refresh(run)
    if action == "retry":
        emit_event(db, run.id, "verify_retry", {"action": "🔁 Retrying verification once more"})
    else:
        h = dict(run.handoff_json or {})
        h["verify_skipped"] = True    # flag so the approval gate shows the change as UNVERIFIED
        h.pop("verified", None)       # a skip is NOT a pass — clear any stale verified flag
        run.handoff_json = h
        emit_event(db, run.id, "verify_skipped",
                   {"action": "⚠ Verification skipped by user — proceeding without a passing build"})
    # Emit the phase transition too, so the UI's event-driven phase updates immediately and
    # the verification gate closes (no duplicate-click window) even before the run refetches.
    emit_event(db, run.id, "phase_changed", {"from": "awaiting_verify_decision", "to": run.phase})
    db.commit()
    agentic_drive_task.delay(run.id, "")
    return {**_run_view(run), "decided": action}


# Phases at which a change has been generated + verified at least once, so an
# on-demand re-verify is meaningful AND no drive loop is mid-flight on the tree.
_REVERIFY_PHASES = frozenset({
    "review", "awaiting_human_approval", "awaiting_verify_decision",
    "rebase_reverify", "completed",
})
_REVERIFY_STALE_S = 3600  # a "running" re-verify older than this is treated as dead


@router.post("/agentic/runs/{run_id}/reverify")
def reverify_run_endpoint(run_id: str, db: DbDep, current_user: CurrentUser):
    """Re-run the build verification on an already-generated / approved change and
    report pass/fail — WITHOUT re-running code-gen or review. Read-only w.r.t.
    workflow state (phase/status unchanged); the result lands as ``verification`` +
    ``reverify_done`` events and a durable ``last_reverify`` field. The heavy mvn
    build runs in Celery; this route validates, marks in-progress, and dispatches."""
    from datetime import datetime
    from app.models.base import utcnow
    from app.agents.agentic_events import emit_event
    from app.agents import workspace_local
    from app.services.celery_tasks import agentic_reverify_task

    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if run.phase not in _REVERIFY_PHASES:
        raise HTTPException(409, f"re-verify is available once the change is generated (phase={run.phase})")

    # Don't rebuild a tree another non-terminal run (e.g. a Phase-B child) is editing.
    ws = getattr(run, "workspace_run_id", None) or run.id
    if workspace_local._has_active_dependent(db, ws):
        raise HTTPException(409, "another run is still working on this workspace — re-verify after it finishes")

    # One re-verify at a time per run. Lock the run row first so the
    # check-and-set on last_reverify is atomic against a concurrent POST —
    # without the lock two requests both read status!="running", both mark
    # "running", and dispatch two mvn builds racing on the same workspace.
    # (FOR UPDATE is a no-op on the SQLite test harness, which serializes
    # writers anyway.) A "running" marker older than the staleness window is
    # treated as dead (worker crashed mid-build) so the run never wedges.
    run = db.execute(
        select(AgenticRun).where(AgenticRun.id == run_id).with_for_update()
    ).scalar_one()
    h = dict(run.handoff_json or {})
    lr = h.get("last_reverify") or {}
    if lr.get("status") == "running":
        try:
            age = (utcnow() - datetime.fromisoformat(lr.get("at"))).total_seconds()
        except Exception:
            age = _REVERIFY_STALE_S + 1
        if age < _REVERIFY_STALE_S:
            raise HTTPException(409, "a re-verify is already in progress for this run")

    h["last_reverify"] = {"status": "running", "at": utcnow().isoformat()}
    run.handoff_json = h
    emit_event(db, run.id, "reverify_started", {"action": "🔁 Re-verifying the change…"})
    db.commit()
    agentic_reverify_task.delay(run.id)
    return {**_run_view(run), "reverify": "started"}


class DecideApproachRequest(BaseModel):
    selected_option_id: str | None = None
    custom_direction: str | None = None
    option: dict | None = None        # the full option object (so the agent gets all the detail)


def _resume_xsd_discovery(db, run, current_user, emit_kind: str, payload: dict) -> None:
    """Shared: re-drive a paused XSD run from xsd_discovery under a fresh lease."""
    from app.agents.agentic_events import emit_event
    from app.services.celery_tasks import agentic_drive_task
    from app.models.base import utcnow
    run.status, run.phase = "active", "xsd_discovery"
    run.cancel_requested = False
    run.lease_owner = None
    run.lease_expires_at = None
    emit_event(db, run.id, emit_kind, payload)
    db.commit()
    agentic_drive_task.delay(run.id, "")


@router.post("/agentic/runs/{run_id}/decide-approach")
def decide_approach(run_id: str, body: DecideApproachRequest, db: DbDep, current_user: AgenticUser):
    """Record the human's reuse-vs-new choice at the approach gate and re-drive the run
    in apply mode. The agent implements exactly the chosen approach (or the custom
    direction) before generating any schema."""
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if run.phase != "awaiting_approach_decision":
        raise HTTPException(409, f"run is not awaiting an approach decision (phase={run.phase})")
    if not (body.selected_option_id or body.custom_direction):
        raise HTTPException(400, "choose an option or provide a custom direction")
    from app.models.base import utcnow
    from app.models.agentic import AgenticEvent
    # Pull the proposal being decided so we can persist a RICH decision memory: the chosen
    # option, the REJECTED alternatives, and the grounding evidence. Phase A (apply) and
    # Phase B (code) both read this so they implement exactly the chosen path and never
    # drift back to a rejected one (e.g. silently spinning up a new API after reuse was picked).
    prop_ev = (db.query(AgenticEvent)
               .filter(AgenticEvent.run_id == run_id, AgenticEvent.kind == "approach_proposal")
               .order_by(AgenticEvent.seq.desc()).first())
    proposal = (prop_ev.payload or {}) if prop_ev else {}
    all_opts = [o for o in (proposal.get("options") or []) if isinstance(o, dict)]
    chosen = body.option or next((o for o in all_opts if o.get("id") == body.selected_option_id), None)
    rejected = [o for o in all_opts if o.get("id") != (body.selected_option_id or (chosen or {}).get("id"))]
    chosen_approach = (chosen or {}).get("approach")
    # A directive Phase B can't ignore: if reuse/extend was picked, no new API/service.
    directive = None
    if chosen_approach in ("reuse", "extend"):
        tgt = (chosen or {}).get("target_api") or "the existing flow"
        directive = (f"The human chose to {chosen_approach.upper()} {tgt}. Do NOT create a new "
                     "API/controller/service/state-machine — implement INSIDE that existing flow.")
    h = dict(run.handoff_json or {})
    h["approach_decision"] = {"selected_option_id": body.selected_option_id,
                              "custom_direction": body.custom_direction,
                              "option": chosen, "approach": chosen_approach,
                              "rejected": [{"id": o.get("id"), "title": o.get("title"),
                                            "approach": o.get("approach")} for o in rejected],
                              "evidence": proposal.get("evidence") or [],
                              "directive": directive, "chosen_at": utcnow().isoformat()}
    run.handoff_json = h
    db.commit()  # persist the decision durably BEFORE the best-effort plan-versioning below
    # If the human picked an option the agent flagged as diverging from the ratified plan,
    # roll the plan forward to v+1 recording the chosen approach + why — so the plan stops
    # contradicting what will be built (the gate's whole point).
    # Coerce to a real bool: a client-posted body.option may carry diverges_from_plan as the
    # agent's raw string ("no"/"false"), which is truthy in Python and would bump the plan on a
    # NON-diverging choice. Only a genuine truthy value rolls the plan to v+1.
    _diverges = (chosen or {}).get("diverges_from_plan")
    if isinstance(_diverges, str):
        _diverges = _diverges.strip().lower() in ("true", "yes", "1", "y")
    if chosen and _diverges:
        from app.agents.plan_versioning import record_approach_decision_version
        from app.agents.agentic_events import emit_event
        new_v = record_approach_decision_version(
            db, change_request_id=run.change_request_id, run_id=run.id,
            chosen=chosen, decided_by=getattr(current_user, "id", None))
        if new_v:
            emit_event(db, run.id, "plan_revised",
                       {"version": new_v, "diverges": True,
                        "action": f"📝 Plan updated to v{new_v} — chosen approach diverges from the "
                                  "original recommendation; the reason is recorded in the plan"})
    _resume_xsd_discovery(db, run, current_user, "approach_decided",
                          {"selected_option_id": body.selected_option_id,
                           "custom": bool(body.custom_direction),
                           "action": "✅ Approach chosen — generating the schema accordingly"})
    return {**_run_view(run), "decided": True}


# ── Change-Analysis gates (S2, kind='analysis') ──────────────────────────────

def _resume_analysis(db, run, emit_kind: str, payload: dict) -> None:
    """Re-drive a paused analysis run from the ANALYZING phase under a fresh lease."""
    from app.agents.agentic_events import emit_event
    from app.services.celery_tasks import agentic_drive_task
    run.status, run.phase = "active", "analyzing"
    run.cancel_requested = False
    run.lease_owner = None
    run.lease_expires_at = None
    emit_event(db, run.id, emit_kind, payload)
    db.commit()
    agentic_drive_task.delay(run.id, "")


def _latest_event_payload(db, run_id: str, kind: str) -> dict:
    from app.models.agentic import AgenticEvent
    ev = (db.query(AgenticEvent)
          .filter(AgenticEvent.run_id == run_id, AgenticEvent.kind == kind)
          .order_by(AgenticEvent.seq.desc()).first())
    return (ev.payload or {}) if ev else {}


class DecideClarificationsRequest(BaseModel):
    # For single-select / yes-no questions:
    #   [{question_id, chosen_option_id?, custom_answer?}]
    # For v3 multi_select questions (e.g. `scope_signal::parties_in_scope`):
    #   [{question_id, chosen_option_ids: list[str], custom_answer?}]
    # `_resolve` inspects q.kind and picks the right shape; we keep the outer
    # type as `list` (untyped) so a mixed payload of both shapes serializes.
    answers: list
    # Optional free-text plan rectification the PM adds alongside the answers — a binding
    # FUNCTIONAL choice (e.g. "create a new dedicated API instead of reusing X"). The agent
    # re-checks technical feasibility, applies it, and records the repercussions in the plan.
    plan_rectification: str | None = None


@router.post("/agentic/runs/{run_id}/decide-clarifications")
def decide_clarifications(run_id: str, body: DecideClarificationsRequest, db: DbDep, current_user: CurrentUser):
    """Record the PM's answers to the analysis clarification batch (each → a ledger
    entry) and re-drive the analysis so it produces the plan."""
    run = _run_or_404(db, run_id)
    _authz_analysis(current_user)
    if run.phase != "awaiting_clarifications":
        raise HTTPException(409, f"run is not awaiting clarifications (phase={run.phase})")
    if not body.answers:
        raise HTTPException(400, "answers are required")
    from app.services import decision_ledger as DL
    questions = _latest_event_payload(db, run_id, "clarifications_requested").get("questions") or []
    qmap = {q.get("id"): q for q in questions if isinstance(q, dict) and q.get("id")}
    answers_by_qid = {a.get("question_id"): a for a in body.answers
                      if isinstance(a, dict) and a.get("question_id")}

    def _resolve(q, ans):
        # v3 — multi_select: caller sends chosen_option_ids: list[str]. Resolve
        # each id to its label and JSON-serialize the sorted list into `chosen`
        # so the loader can parse it back. Falls through to custom_answer when
        # nothing was selected and to empty string when neither is present.
        if (q or {}).get("kind") == "multi_select":
            import json as _json
            ids = list((ans or {}).get("chosen_option_ids") or [])
            opts_by_id = {o.get("id"): o.get("label") for o in (q.get("options") or [])
                          if isinstance(o, dict) and o.get("id")}
            labels = sorted(opts_by_id[i] for i in ids if i in opts_by_id)
            if labels:
                return _json.dumps(labels, ensure_ascii=False)
            return ((ans or {}).get("custom_answer") or "").strip() or ""
        opt = next((o for o in (q.get("options") or [])
                    if isinstance(o, dict) and o.get("id") == (ans or {}).get("chosen_option_id")), None)
        return (opt or {}).get("label") or ((ans or {}).get("custom_answer") or "").strip() or ""

    # Backend completeness gate — NOT just the UI disable. A stale or bypassed client could
    # otherwise resume the analysis with unanswered questions silently dropped, producing a
    # plan built on missing PM decisions. Every asked question must have a real answer.
    missing = [qid for qid in qmap if not _resolve(qmap[qid], answers_by_qid.get(qid))]
    if qmap and missing:
        raise HTTPException(
            422, f"{len(missing)} of {len(qmap)} question(s) still need an answer before the plan can be drafted")

    lines = []
    for qid, q in qmap.items():
        ans = answers_by_qid.get(qid)
        chosen = _resolve(q, ans)
        qtext = q.get("text") or qid or "clarification"
        # Provenance — the anti-laundering record. A clicked agent-suggested option and a
        # PM-typed answer must stay distinguishable downstream: the incident case was an
        # LLM-invented value that a click converted into a "human-ratified" directive with
        # its origin erased. `occupancy` is the platform's server-side check attached by
        # ask_clarifications; build_decisions_block renders this so every later agent sees
        # the epistemic status, not just the value.
        opt = next((o for o in (q.get("options") or [])
                    if isinstance(o, dict) and o.get("id") == (ans or {}).get("chosen_option_id")), None)
        prov: dict = {"origin": "llm_option" if opt else "human_typed"}
        if opt:
            prov["was_recommended"] = q.get("recommended") == opt.get("id")
            if opt.get("proposed_value"):
                prov["proposed_value"] = str(opt.get("proposed_value"))[:80]
            occ = opt.get("occupancy")
            if isinstance(occ, dict):
                prov["occupancy"] = {"value": occ.get("value"), "hits": occ.get("hits"),
                                     "complete": occ.get("complete", True)}
        DL.append_entry(
            db, run.change_request_id, question_key=str(qid or qtext),
            kind="clarification", question=qtext, options=q.get("options"),
            chosen=chosen, directive=f"For '{qtext}': {chosen}.",
            decided_by=current_user.id, decided_against=prov,
        )
        lines.append(f"Q: {qtext}\nA: {chosen}")
    rect = (body.plan_rectification or "").strip()
    if rect:
        # The PM's rectification is a binding functional choice — ledger it like an answer
        # so every downstream phase receives it, and ride it into the resume prompt where
        # the rectification clause makes the agent feasibility-check + apply it.
        # Key parity with the decide_plan reopen path: the ledger supersedes by
        # question_key, and a reopen re-drives analysis back through this gate — a
        # constant key would let round 2's rectification silently delete round 1's
        # binding directive (and reopen pops clarification_answers, so the ledger is
        # the only durable carrier). Scope to the plan version being corrected
        # (v0 = the pre-plan first round).
        from app.models.change_analysis import ChangeAnalysis
        _ca = (db.query(ChangeAnalysis)
               .filter(ChangeAnalysis.change_request_id == run.change_request_id)
               .order_by(ChangeAnalysis.version.desc()).first())
        _pv = getattr(_ca, "version", 0) or 0
        DL.append_entry(
            db, run.change_request_id, question_key=f"plan_rectification:clarifications:v{_pv}",
            kind="clarification", question="Plan rectification (PM)",
            chosen=rect[:500],
            directive=f"PM plan rectification (binding functional choice): {rect[:400]}",
            decided_by=current_user.id,
        )
        lines.append("PLAN RECTIFICATION (binding functional choice — verify feasibility, "
                     f"apply, and record repercussions): {rect}")
    h = dict(run.handoff_json or {})
    h["clarification_answers"] = "\n\n".join(lines)
    run.handoff_json = h
    _resume_analysis(db, run, "clarifications_answered",
                     {"count": len(lines), "rectified": bool(rect),
                      "action": "✅ Answers recorded — drafting the plan"})
    return {**_run_view(run), "answered": len(lines)}


class DecidePlanRequest(BaseModel):
    action: str = "ratify"            # "ratify" | "reopen"
    side: str | None = None           # "functional" | "technical" (default by role)
    feedback: str | None = None       # required for reopen


@router.post("/agentic/runs/{run_id}/decide-plan")
def decide_plan(run_id: str, body: DecidePlanRequest, db: DbDep, current_user: CurrentUser):
    """Ratify (PM = functional, tech-lead = technical) or reopen the proposed plan.
    Both sides ratified → a binding plan_ratification ledger entry + the run completes."""
    run = _run_or_404(db, run_id)
    _authz_analysis(current_user)
    if run.phase != "awaiting_plan_approval":
        raise HTTPException(409, f"run is not awaiting plan ratification (phase={run.phase})")
    from app.models.change_analysis import ChangeAnalysis
    from app.models.base import utcnow
    ca = (db.query(ChangeAnalysis)
          .filter(ChangeAnalysis.change_request_id == run.change_request_id)
          .order_by(ChangeAnalysis.version.desc()).first())
    if ca is None:
        raise HTTPException(409, "no change-analysis plan to act on")

    if body.action == "reopen":
        h = dict(run.handoff_json or {})
        h["plan_feedback"] = (body.feedback or "Revise the plan.").strip()
        h.pop("clarification_answers", None)
        run.handoff_json = h
        ca.status = "draft"
        # Same weight as a clarification-stage rectification: the feedback is a binding
        # functional choice on the ledger, so downstream phases receive it even if the
        # revised plan under-applies it. The key is scoped to the plan VERSION being
        # reopened: the ledger supersedes by question_key, so a shared key would make each
        # reopen delete the previous rectification — a PM who asks for a new API here and
        # then reopens again over wording would silently lose the API directive. Per-version
        # keys accumulate distinct corrections while a re-reopen of the SAME version (a
        # genuine re-answer) still supersedes.
        if (body.feedback or "").strip():
            from app.services import decision_ledger as DL
            DL.append_entry(
                db, run.change_request_id,
                question_key=f"plan_rectification:ratify:v{getattr(ca, 'version', 0) or 0}",
                kind="clarification", question="Plan rectification (at ratification)",
                chosen=body.feedback.strip()[:500],
                directive=f"PM plan rectification (binding functional choice): {body.feedback.strip()[:400]}",
                decided_by=current_user.id,
            )
        db.commit()
        _resume_analysis(db, run, "plan_reopened",
                         {"feedback": h["plan_feedback"][:200], "action": "↩ Plan reopened — revising"})
        return {**_run_view(run), "reopened": True}

    # ratify — DETERMINISTIC critical-decision gate first. A plan whose money-movement /
    # atomicity / ordering decisions are missing or unsourced cannot be ratified: an
    # unsourced critical decision is exactly the assumption the generator later re-decides
    # (observed twice: 'per-participant credit' drifted to a consolidated double-credit).
    if getattr(settings, "agentic_require_critical_decisions", True):
        _cds = (ca.technical_analysis or {}).get("critical_decisions")
        _valid_sources = {"requirement", "code_verified", "human_decision"}
        _gaps: list[str] = []
        if not isinstance(_cds, list) or not _cds:
            _gaps.append("plan has no critical_decisions block — re-run the analysis (the "
                         "planner must decide settlement/money-legs/atomicity/ordering "
                         "explicitly, or ask a clarification)")
        else:
            for cd in _cds:
                if not isinstance(cd, dict):
                    continue
                dim = str(cd.get("dimension") or "?")
                if not (cd.get("decision") or cd.get("directive")):
                    _gaps.append(f"{dim}: no decision recorded")
                # Compound sources ("requirement|human_decision") are MORE evidence, not less —
                # accept when every component is a valid source.
                _parts = [x for x in __import__("re").split(r"[|/,+ ]+",
                          str(cd.get("source") or "").lower()) if x]
                if not _parts or any(x not in _valid_sources for x in _parts):
                    _gaps.append(f"{dim}: unsourced (must be requirement | code_verified | "
                                 "human_decision — never an assumption)")
        if _gaps:
            raise HTTPException(422, "Cannot ratify — unresolved critical decisions: "
                                + "; ".join(_gaps[:8]))

    side = (body.side or ("functional"
            if current_user.role in (UserRole.PRODUCT_MANAGER, UserRole.PRODUCT_OWNER)
            else "technical")).lower()
    if side == "functional" or current_user.role == UserRole.ADMIN:
        ca.pm_ratified_by, ca.pm_ratified_at = current_user.id, utcnow()
    if side == "technical" or current_user.role == UserRole.ADMIN:
        ca.tech_ratified_by, ca.tech_ratified_at = current_user.id, utcnow()
    db.commit()

    if ca.pm_ratified_at is not None and ca.tech_ratified_at is not None:
        ca.status = "ratified"
        from app.services import decision_ledger as DL
        fp = ca.functional_plan or {}
        DL.append_entry(
            db, run.change_request_id, question_key="plan_ratification",
            kind="plan_ratification", question="Implementation plan",
            chosen=(fp.get("overview") or "ratified")[:500],
            directive="Implement strictly within the ratified plan; do not introduce flows, "
                      "fields, or requirements not traceable to it.",
            decided_by=current_user.id,
            decided_against={"change_analysis_id": ca.id, "version": ca.version},
        )
        # Each critical decision becomes its OWN binding ledger directive — the same
        # can't-ignore mechanism the approach gate uses — so Phase B and the reviewer
        # receive them as numbered, individually-verifiable contract items.
        for cd in ((ca.technical_analysis or {}).get("critical_decisions") or []):
            if isinstance(cd, dict) and (cd.get("directive") or cd.get("decision")):
                DL.append_entry(
                    db, run.change_request_id,
                    question_key=f"critical:{cd.get('dimension', '?')}",
                    kind="critical_decision",
                    question=f"Critical decision — {cd.get('dimension', '?')}",
                    chosen=str(cd.get("decision") or cd.get("directive"))[:500],
                    directive=str(cd.get("directive") or cd.get("decision"))[:500],
                    decided_by=current_user.id,
                    decided_against={"source": cd.get("source"),
                                     "evidence": str(cd.get("evidence") or "")[:300]},
                )
        from app.agents.agentic_events import emit_event
        emit_event(db, run.id, "plan_ratified",
                   {"action": "✅ Plan ratified (PM + tech-lead) — analysis complete"})
        # The replay transcript now OUTLIVES ratification: the approach-proposal phase of the
        # next xsd/full run continues it (agentic_orchestrator._phase_propose, looked up by
        # change_request_id) instead of redoing the identical discovery sweep. The analysis
        # run is terminal here so the blob is never rewritten again — ~300KB of dormant JSON
        # on a terminal row is the price of not re-reading the codebase in the next phase.
        S.mark_terminal(db, run, S.AgenticStatus.COMPLETED)
        db.commit()
    return {**_run_view(run), "pm_ratified": ca.pm_ratified_at is not None,
            "tech_ratified": ca.tech_ratified_at is not None, "status": ca.status}


class ChallengePlanRequest(BaseModel):
    assumption: str        # the plan assumption that turned out wrong
    finding: str           # what the code actually shows


@router.post("/agentic/runs/{run_id}/challenge-plan")
def challenge_plan(run_id: str, body: ChallengePlanRequest, db: DbDep, current_user: AgenticUser):
    """S7 challenge→revalidation: Phase B (or a reviewer) flags that the ratified plan
    rests on a wrong assumption. The correction is appended to the Decision Ledger as a
    binding revalidation entry, so every subsequent generation honours the ACTUAL finding
    instead of the stale plan. (Reopening the analysis gate for a fresh plan is the PM's
    call via decide-plan reopen; this guarantees the correction is at least binding.)"""
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if not body.assumption.strip() or not body.finding.strip():
        raise HTTPException(400, "assumption and finding are required")
    from app.services import decision_ledger as DL
    DL.append_entry(
        db, run.change_request_id,
        question_key=f"challenge:{body.assumption.strip()[:80]}",
        kind="revalidation", question=body.assumption.strip(), chosen=body.finding.strip(),
        directive=(f"Plan assumption challenged during implementation — assumed: "
                   f"{body.assumption.strip()}; ACTUAL: {body.finding.strip()}. Honour the actual finding."),
        decided_by=current_user.id,
    )
    from app.agents.agentic_events import emit_event
    emit_event(db, run.id, "plan_challenged",
               {"assumption": body.assumption.strip()[:200],
                "action": "⚠ Plan assumption challenged — recorded as a binding correction"})
    db.commit()
    return {"recorded": True}


class DecideRevisionRequest(BaseModel):
    selected_option_id: str | None = None
    custom_direction: str | None = None
    option: dict | None = None        # the chosen safer-alternative object (full detail)
    proceed_anyway: bool = False      # ⛔ explicit risk acceptance for the ORIGINAL request


@router.post("/agentic/runs/{run_id}/decide-revision")
def decide_revision(run_id: str, body: DecideRevisionRequest, db: DbDep, current_user: AgenticUser):
    """Resolve the disruptive-revision conversation: the human picks one of the agent's
    safer alternatives (normal refine round), or explicitly proceeds with their original
    request — which is implemented but permanently recorded + displayed as ⛔ accepted risk."""
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if run.phase != "awaiting_approach_decision":
        raise HTTPException(409, f"run is not awaiting a revision decision (phase={run.phase})")
    from app.models.base import utcnow
    h = dict(run.handoff_json or {})
    original = (h.get("xsd_change_request") or {}).get("feedback", "")
    if body.proceed_anyway:
        if not original:
            raise HTTPException(409, "no pending change request to accept the risk for")
        h["xsd_change_request"] = {"feedback": original, "accepted_risk": True,
                                   "at": utcnow().isoformat()}
        h["accepted_risk"] = {"request": original, "by": getattr(current_user, "username", None) or current_user.id,
                              "at": utcnow().isoformat()}
        run.handoff_json = h
        _resume_xsd_discovery(db, run, current_user, "risk_accepted",
                              {"request": original[:300], "by": h["accepted_risk"]["by"],
                               "action": "⛔ Risk accepted — implementing the disruptive change as explicitly requested"})
        return {**_run_view(run), "decided": True, "accepted_risk": True}
    direction = (body.custom_direction or "").strip()
    if not direction and body.option:
        o = body.option
        direction = ". ".join(s for s in (o.get("title"), o.get("how_it_fits")) if s)
    if not direction:
        raise HTTPException(400, "choose a safer alternative, give a custom direction, or proceed_anyway")
    h["xsd_change_request"] = {"feedback": direction, "at": utcnow().isoformat(), "via_revision": True}
    run.handoff_json = h
    _resume_xsd_discovery(db, run, current_user, "revision_chosen",
                          {"selected_option_id": body.selected_option_id,
                           "action": "✅ Safer alternative chosen — applying it now"})
    return {**_run_view(run), "decided": True, "accepted_risk": False}


class DecideCodeDecisionRequest(BaseModel):
    answer: str                     # the human's decision, plain language
    chosen_option_id: str | None = None


@router.post("/agentic/runs/{run_id}/decide-code-decision")
def decide_code_decision(run_id: str, body: DecideCodeDecisionRequest, db: DbDep, current_user: AgenticUser):
    """A3 — answer the code agent's parked decision question. The answer is recorded as a
    BINDING decision-ledger directive (so this and every future run receives it) and the
    code phase resumes."""
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if run.phase != "awaiting_code_decision":
        raise HTTPException(409, f"run is not awaiting a code decision (phase={run.phase})")
    ans = (body.answer or "").strip()
    if not ans and not body.chosen_option_id:
        raise HTTPException(400, "answer (or chosen_option_id) is required")
    dq = (run.handoff_json or {}).get("code_decision_request") or {}
    if body.chosen_option_id and not ans:
        # Coerce the stored option id to str before matching: ask_decision's schema does not
        # force `id` to a string, so an LLM emitting `"id": 1` (int) would fail `1 == "1"` and
        # the human's chosen LABEL would be silently replaced by the raw id in the persisted
        # decision-ledger directive. chosen_option_id always arrives as a string from the UI.
        opt = next((o for o in (dq.get("options") or [])
                    if isinstance(o, dict) and str(o.get("id")) == body.chosen_option_id), None)
        ans = str((opt or {}).get("label") or body.chosen_option_id)
    from app.services import decision_ledger as DL
    q = str(dq.get("question") or "code decision")
    blocked = str(dq.get("blocked_item") or "")
    # Key on the BLOCKED ITEM, not the question prose. The agent rewords a question every
    # time it re-asks, so prose-keyed entries never superseded each other: one run recorded
    # seven answers to one question under seven keys and fed all seven to the model at once
    # as BINDING, which is exactly how it ended up flip-flopping for hours. Anchoring on the
    # directive/plan item collapses those into a single chain where the newest answer wins.
    # Semantic similarity is the backstop for a re-ask anchored on a differently-worded item.
    key, key_meta = DL.resolve_question_key(
        db, run.change_request_id, prefix="code_decision",
        anchor=blocked or q, question=q, kind="code_decision",
    )
    repeat = DL.repeat_state(db, run.change_request_id, key)
    DL.append_entry(
        db, run.change_request_id, question_key=key,
        kind="code_decision", question=q, options=dq.get("options"),
        chosen=ans[:500], directive=f"For '{q[:200]}': {ans[:300]}.",
        decided_by=current_user.id,
        decided_against={"blocked_item": dq.get("blocked_item"),
                         "key_match": key_meta.get("match"),
                         "key_similarity": key_meta.get("score"),
                         "related": key_meta.get("related") or [],
                         "answer_number": repeat["count"] + 1},
    )
    h = dict(run.handoff_json or {})
    h.pop("code_decision_request", None)
    run.handoff_json = h
    from app.models.base import utcnow  # noqa: F401 — parity with sibling endpoints
    from app.agents.agentic_events import emit_event
    emit_event(db, run.id, "code_decision_answered",
               {"answer": ans[:200], "question_key": key,
                "key_match": key_meta.get("match"), "answer_number": repeat["count"] + 1,
                "supersedes_prior": repeat["count"] > 0,
                "action": ("✅ Decision recorded — resuming code generation"
                           if not repeat["count"] else
                           f"✅ Decision recorded (answer #{repeat['count'] + 1} on this question — "
                           "it supersedes the earlier ones) — resuming code generation")})
    S.advance(db, run, S.P.CODE_CHANGE)
    db.commit()
    from app.services.celery_tasks import agentic_drive_task
    agentic_drive_task.delay(run.id, "")
    return {**_run_view(run), "answered": True}


class DecideSchemaAmendmentRequest(BaseModel):
    approve: bool
    reason: str | None = None       # required on reject — becomes the binding directive's rationale


def _record_amendment_decision(db, run, current_user, amendments: list[dict], *,
                               chosen: str, directive: str) -> None:
    """Record a schema-amendment ruling as a BINDING ledger entry.

    Anchored on the amended FILES (stable across rewording) so a re-ask lands on this same
    chain instead of opening a new one. Never raises: a ledger failure must not strand a run
    whose schema edit already landed on disk.
    """
    from app.services import decision_ledger as DL
    try:
        anchor = "schema_amendment:" + ",".join(
            sorted({str(a.get("path") or a.get("file") or "?") for a in amendments}))
        key, _ = DL.resolve_question_key(db, run.change_request_id, prefix="schema_amendment",
                                         anchor=anchor, question=None, use_similarity=False)
        DL.append_entry(db, run.change_request_id, question_key=key, kind="code_decision",
                        question="May the code phase amend the approved schema?",
                        chosen=chosen[:500], directive=directive[:1000],
                        decided_by=current_user.id,
                        decided_against={"blocked_item": anchor,
                                         "amendments": [{"path": a.get("path"),
                                                         "origin": a.get("origin")}
                                                        for a in amendments]})
    except Exception:  # noqa: BLE001 — the ledger write must not block the resume
        logger.exception("schema-amendment ledger append failed run=%s", run.id)


@router.post("/agentic/runs/{run_id}/decide-schema-amendment")
def decide_schema_amendment(run_id: str, body: DecideSchemaAmendmentRequest,
                            db: DbDep, current_user: AgenticUser):
    """Fix 2 — rule on the schema change the code phase staged but could not make itself.

    Approve applies the proposal to the workspace VERBATIM (no model re-does the edit, so
    what you saw is what lands) and resumes code generation against the amended schema.
    Reject records a binding directive to implement around it, so the agent does not simply
    re-propose the same edit next round — the loop this gate exists to break.
    """
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if run.phase != "awaiting_schema_amendment":
        raise HTTPException(409, f"run is not awaiting a schema amendment (phase={run.phase})")
    req = (run.handoff_json or {}).get("schema_amendment_request") or {}
    amendments = req.get("amendments") or []
    if not amendments:
        raise HTTPException(409, "no staged schema amendment found for this run")
    reason = (body.reason or "").strip()
    if not body.approve and not reason:
        raise HTTPException(400, "a reason is required when rejecting — it becomes the binding "
                                 "directive telling the agent what to do instead")

    from app.agents.agentic_events import emit_event
    from app.services import schema_amendment as SA

    h = dict(run.handoff_json or {})
    h.pop("schema_amendment_request", None)
    # Remember WHICH proposals were ruled on, keyed the same way the orchestrator filters them.
    # Per-proposal rather than a run-level flag: a re-stage of this exact edit must not re-park
    # the run (that is the loop this gate breaks), but a different schema problem found later
    # still gets its own gate.
    from app.agents.agentic_orchestrator import _amendment_key
    # NB: only proposals whose outcome is REAL are recorded as decided. An approval that did not
    # land on disk is not a decision the agent can build on, and burning its key here would stop
    # the gate from ever re-opening for it (review finding 1). Rejections are always real: the
    # schema is unchanged by design, which is exactly what the directive tells the agent.
    _decided_now = list(amendments)

    if body.approve:
        # Same workspace resolution the orchestrator uses (_ws_id): a Phase-B run edits its
        # Phase-A parent's tree, so applying under run.id would write to a nonexistent dir.
        result = SA.apply(run.id, (getattr(run, "workspace_run_id", None) or run.id), amendments)
        h["schema_amendment_applied"] = {
            "applied": [{"path": a.get("path"), "file": a.get("file")} for a in result["applied"]],
            "failed": [{"path": a.get("path"), "reason": a.get("reason")} for a in result["failed"]],
        }
        if result["failed"]:
            # An approval that did not fully land must NOT resume as if the schema were fixed.
            # The whole point of this gate is that the agent writes Java against a schema it can
            # trust; telling it "the schema now contains your text" when it does not is worse than
            # the deadlock we replaced, because it is a BINDING falsehood it cannot detect.
            # Re-park on the unapplied remainder so the human sees the fresh before/after.
            try:
                _failed = SA.describe(run.id, (getattr(run, "workspace_run_id", None) or run.id),
                                      (run.handoff_json or {}).get("repo_base_sha") or {},
                                      [{k: v for k, v in a.items() if k != "reason"}
                                       for a in result["failed"]])
            except Exception:  # noqa: BLE001 — provenance is advisory; re-park regardless
                logger.exception("schema-amendment re-describe failed run=%s", run.id)
                _failed = result["failed"]
            # Carry the reason the apply refused, so the UI explains WHY it is back.
            _reasons = {(a.get("repo_id"), a.get("path")): a.get("reason") for a in result["failed"]}
            for a in _failed:
                a.setdefault("apply_failed_reason", _reasons.get((a.get("repo_id"), a.get("path"))))
            h["schema_amendment_request"] = {"amendments": _failed,
                                             "disk_changes": req.get("disk_changes")}
            # Only the ones that genuinely landed are settled; the rest stay open for a re-ruling.
            h["schema_amendments_decided"] = sorted(
                set(h.get("schema_amendments_decided") or [])
                | {_amendment_key(a) for a in result["applied"]})
            run.handoff_json = h
            emit_event(db, run.id, "schema_amendment_partial", {
                **h["schema_amendment_applied"],
                "action": (f"⚠ Approved, but {len(result['failed'])} of {len(amendments)} "
                           f"amendment(s) could NOT be applied "
                           f"({len(result['applied'])} landed) — the file changed since the "
                           "proposal was staged, so the anchor no longer matches. Code generation "
                           "is NOT resuming against a schema that was not amended; review the "
                           "re-staged proposal(s) below.")})
            # Record what DID land, so an operator can see the partial outcome in the ledger.
            if result["applied"]:
                _record_amendment_decision(
                    db, run, current_user, result["applied"],
                    chosen="approved — partially applied",
                    directive=("Part of the schema amendment you staged was approved and applied: "
                               + ", ".join(sorted({str(a.get("file") or a.get("path"))
                                                   for a in result["applied"]}))
                               + ". The remainder could NOT be applied and is awaiting a fresh "
                                 "human ruling — do not assume it landed."))
            db.commit()
            raise HTTPException(409, (
                f"schema amendment could not be fully applied: {len(result['applied'])} of "
                f"{len(amendments)} landed. The run stays at the schema-amendment gate so the "
                "unapplied proposal(s) can be re-reviewed against the current file contents."))
        run.handoff_json = h
        emit_event(db, run.id, "schema_amendment_approved", {
            "applied": h["schema_amendment_applied"]["applied"],
            "action": (f"✅ Schema amendment approved and applied to "
                       f"{len(result['applied'])} file(s) — resuming code generation")})
        directive = ("The schema amendment you staged was APPROVED BY A HUMAN and has been applied "
                     "to the workspace. The schema now contains your proposed text — re-read the "
                     "affected schema file(s) before relying on them, and make the dependent Java "
                     "consistent with the AMENDED schema. Do not re-stage this edit.")
        chosen = "approved — applied verbatim"
    else:
        run.handoff_json = h
        emit_event(db, run.id, "schema_amendment_rejected", {
            "reason": reason[:400],
            "action": ("🚫 Schema amendment rejected — the schema is unchanged and the agent has "
                       "a binding directive to implement around it")})
        directive = SA.rejection_directive(amendments, reason)
        chosen = f"rejected — {reason[:200]}"

    # Every proposal reaching here has a REAL outcome (fully applied, or rejected with the
    # schema deliberately unchanged), so all of them are settled.
    h["schema_amendments_decided"] = sorted(
        set(h.get("schema_amendments_decided") or []) | {_amendment_key(a) for a in _decided_now})
    run.handoff_json = h

    _record_amendment_decision(db, run, current_user, amendments,
                               chosen=chosen, directive=directive)

    S.advance(db, run, S.P.CODE_CHANGE)
    db.commit()
    from app.services.celery_tasks import agentic_drive_task
    agentic_drive_task.delay(run.id, "")
    return {**_run_view(run), "approved": bool(body.approve)}


class RequestXsdChangesRequest(BaseModel):
    feedback: str


@router.post("/agentic/runs/{run_id}/request-xsd-changes")
def request_xsd_changes(run_id: str, body: RequestXsdChangesRequest, db: DbDep, current_user: AgenticUser):
    """Refine loop (full flow): the human asks for changes to the generated XSDs. The
    agent applies them (comply-first — an explicit request supersedes earlier gate
    decisions); only a genuinely BREAKING request pauses for confirmation, once. Then
    re-presents the updated diff for approval."""
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if getattr(run, "kind", "full") != "xsd":
        raise HTTPException(409, "not a Phase-A (XSD) run")
    if run.phase != "awaiting_xsd_approval":
        raise HTTPException(409, f"run is not awaiting XSD approval (phase={run.phase})")
    if not (body.feedback or "").strip():
        raise HTTPException(400, "feedback is required")
    from app.models.base import utcnow
    h = dict(run.handoff_json or {})
    h["xsd_change_request"] = {"feedback": body.feedback.strip(), "at": utcnow().isoformat()}
    run.handoff_json = h
    _resume_xsd_discovery(db, run, current_user, "xsd_changes_requested",
                          {"action": "📝 Applying your requested XSD changes (a breaking change will "
                                     "pause once for your confirmation)"})
    return {**_run_view(run), "refining": True}


@router.post("/agentic/runs/{run_id}/cancel")
def cancel_run(run_id: str, db: DbDep, current_user: AgenticUser, force: bool = False):
    """Cancel a run. A run PARKED at a human gate (or otherwise driver-less) is
    cancelled IMMEDIATELY — the cooperative flag would never fire there because no
    worker is driving it, and an 'active' zombie blocks starting a fresh run (the
    one-active-run-per-change constraint). A run a worker is actively driving gets
    the cooperative flag, honoured at the next phase boundary (§3).

    ``force=true`` (used by "Start over" when the plain cancel above didn't clear
    the block) skips the gate/lease-dead check and terminates immediately regardless
    of phase — for the rare case where a still-alive worker is genuinely mid-push, this
    does NOT kill the underlying subprocess; it only frees the DB row so a new run can
    start. The in-flight push may still complete or fail in the background."""
    from app.models.agentic import AgenticStatus
    from app.models.base import utcnow
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    _gates = {"awaiting_approach_decision", "awaiting_xsd_approval", "awaiting_human_approval",
              "awaiting_tsd_approval"}
    # A worker that died mid-phase (crash / stack restart) leaves lease_owner set
    # but lease_expires_at in the past. That run is just as driver-less as one with
    # no lease — the cooperative flag would never fire — so cancel it immediately.
    lease_dead = run.lease_owner is None or (
        run.lease_expires_at is not None and run.lease_expires_at < utcnow()
    )
    if run.status == "active" and (force or run.phase in _gates or lease_dead):
        try:
            # honour_cancel: CANCELLED normally; a re-opened deferred push (phase='completed')
            # closes back to COMPLETED — its only legal terminal.
            S.honour_cancel(db, run)
        except Exception:  # noqa: BLE001 — illegal transition from this phase: fall back to cooperative
            S.request_cancel(db, run)
    else:
        S.request_cancel(db, run)
    db.commit()
    return _run_view(run)


_TERMINAL_PHASES = {"completed", "failed", "cancelled", "gave_up"}


def _resumable_phase(db, run) -> str | None:
    """The phase to resume FROM. If the run is non-terminal, its current phase;
    if terminal, the last NON-terminal phase it reached (mark_terminal overwrote
    run.phase) — read from the phase_changed event history. If it died before ANY
    non-terminal checkpoint (an early clone/workspace failure), fall back to
    ``pending`` so the run re-drives from the start instead of dead-ending as
    non-resumable."""
    if run.phase not in _TERMINAL_PHASES:
        return run.phase
    from app.models.agentic import AgenticEvent
    rows = (db.query(AgenticEvent)
            .filter(AgenticEvent.run_id == run.id, AgenticEvent.kind == "phase_changed")
            .order_by(AgenticEvent.seq.desc()).all())
    for ev in rows:
        to = (ev.payload or {}).get("to")
        if to and to not in _TERMINAL_PHASES:
            return to
    # No non-terminal checkpoint was ever recorded — the run died during its very first
    # transition (e.g. the pending→workspace_ready clone failed before recording any
    # phase). Re-drive from the start: the clone is idempotent and no edits exist yet, so
    # resuming from `pending` is safe and lets an early failure (bad workspace path,
    # transient clone error) recover instead of dead-ending with "no resumable phase".
    return "pending"


@router.post("/agentic/runs/{run_id}/resume")
def resume_run(run_id: str, db: DbDep, current_user: CurrentUser):
    """Manually resume/continue a run that stalled, failed, or was paused — re-drive
    it from its last working phase under a fresh lease. Continues from the on-disk
    edits (the workspace persists); the goal is recovered from the change request."""
    run = _run_or_404(db, run_id)
    _authz_resume(run, current_user)
    if run.status == "completed":
        raise HTTPException(409, "run already completed")
    # A run parked at a human gate is driven by the gate UI, not Resume — re-driving
    # it just re-breaks at the gate (no progress), so reject with a clear message.
    if run.phase in ("awaiting_human_approval", "awaiting_xsd_approval", "awaiting_approach_decision",
                     "awaiting_code_decision", "awaiting_tsd_approval",
                     "awaiting_schema_amendment"):
        raise HTTPException(409, "run is awaiting your decision — use the gate controls, not Resume")
    phase = _resumable_phase(db, run)
    if not phase:
        raise HTTPException(409, "no resumable phase found for this run")
    # One-active-run invariant (partial-unique index uq_agentic_runs_active — at most one
    # active run per change). If a NEWER run is already active for this change — e.g. "Start
    # over" created one — flipping this stopped run back to active violates the index and
    # surfaces as an opaque "An internal error occurred". Detect it and reject clearly.
    from app.agents.agentic_state import _active_run
    other = _active_run(db, run.change_request_id)
    if other is not None and other.id != run.id:
        raise HTTPException(409, "a newer run for this change is already active — continue that "
                                 "one (or cancel it first) instead of resuming this stopped run")
    # Reset to active at the working phase + free any stale lease, then re-drive.
    run.status, run.phase = "active", phase
    run.cancel_requested = False
    run.lease_owner = None
    run.lease_expires_at = None
    db.commit()
    from app.agents.agentic_events import emit_event
    emit_event(db, run.id, "resume_requested", {"phase": phase, "action": f"▶ Manual resume → {phase}"})
    db.commit()
    # A run sitting in `pushing` (approval already given) resumes via the PUSH task,
    # not the drive loop — `_step` has no pushing handler.
    if phase in ("pushing", "rebase_reverify"):
        from app.services.celery_tasks import agentic_push_task
        agentic_push_task.delay(run_id)
    else:
        from app.services.celery_tasks import agentic_drive_task
        agentic_drive_task.delay(run_id, "")     # intent recovered from the change request in drive_run
    return {**_run_view(run), "resumed": True, "phase": phase}


# ── Change walkthrough — plain-language dev + tester flow (§17) ────────────────

def _walkthrough_diff(db, run) -> str:
    """Diff of the run's CHANGE-SET vs the recorded base, robust to run state: it diffs
    against the manifest's base SHA (so it captures changes even after they're committed at
    push time), and surfaces new files via an intent-to-add that is immediately undone.

    Scoped to the change-set paths — the manifest's operations (durable), else the
    filtered live change-set. An UNSCOPED diff here once described a completely different
    change than the panel: post-push (fallback diff vs HEAD = nothing) it saw only the
    verify build's ``target/**`` output and the ``.lease`` marker, and the walkthrough
    honestly reported "no application source code in this diff"."""
    from app.models.agentic import ChangeManifest
    from app.agents import workspace_local
    from app.agents.platform_adapter import adapter
    ws = run.workspace_run_id or run.id
    man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run.id)
           .order_by(ChangeManifest.created_at.desc()).first())
    bases = {pr["repo_id"]: pr.get("base_commit_sha") for pr in ((man.per_repo if man else []) or [])}
    man_paths: dict[str, list[str]] = {}
    for op in ((man.operations if man else None) or []):
        if op.get("repo_id") and op.get("path"):
            man_paths.setdefault(op["repo_id"], []).append(op["path"])
    parts: list[str] = []
    for rid in (run.selected_repo_ids or []):
        try:
            rd = workspace_local.repo_dir(ws, rid)
            paths = man_paths.get(rid) or [p for _op, p in workspace_local.changed_files(ws, rid)]
            if not paths:
                continue
            base = bases.get(rid) or workspace_local.recorded_base(ws, rid)
            adapter.run_command(rd, ["git", "add", "-A", "-N", "--", *paths])  # show new files as additions
            res = adapter.run_command(rd, ["git", "diff", base, "--", *paths])
            adapter.run_command(rd, ["git", "reset", "-q", "--", *paths])      # undo the intent-to-add
            d = (res.stdout or "").strip()
            logger.info("walkthrough_diff: run=%s repo=%s scope=%s paths=%d base=%s bytes=%d",
                        run.id, rid, "manifest-ops" if man_paths.get(rid) else "live-changed-files",
                        len(paths), base[:12], len(d))
            if d:
                parts.append(f"### repo {rid}\n{d[:200000]}")
        except Exception:  # noqa: BLE001 — best-effort; an unreadable repo just contributes nothing
            continue
    return "\n\n".join(parts)


@router.get("/agentic/runs/{run_id}/walkthrough")
def get_walkthrough(run_id: str, db: DbDep, current_user: CurrentUser):
    """Return the stored developer + tester walkthrough for a run (null until generated)."""
    run = _run_or_404(db, run_id)
    _authz_read(run, current_user)
    wt = (run.handoff_json or {}).get("walkthrough")
    return {"run_id": run_id, "generated": bool(wt), "walkthrough": wt or None}


@router.post("/agentic/runs/{run_id}/walkthrough")
async def generate_run_walkthrough(run_id: str, db: DbDep, current_user: AgenticUser):
    """On-demand: generate the plain-language developer + tester walkthrough for this run's
    change — what the API does now, the runtime flow, the decision/decline logic, and concrete
    tester scenarios. Grounded in the actual diff. Stored on the run; downloadable as a QA CSV."""
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    from app.agents.agentic_orchestrator import _analysis_plan_block
    from app.agents.change_walkthrough import generate_walkthrough
    from app.models.change_request import ChangeRequest
    diff_text = _walkthrough_diff(db, run)
    plan = _analysis_plan_block(db, run.change_request_id)
    cr = db.get(ChangeRequest, run.change_request_id)
    intent = (cr.initial_prompt if cr else "") or (cr.title if cr else "") or ""
    title = (cr.title if cr else None) or "Change walkthrough"
    wt = await generate_walkthrough(intent=intent, plan_text=plan, diff_text=diff_text, title=title)
    hj = dict(run.handoff_json or {})
    hj["walkthrough"] = wt
    run.handoff_json = hj
    db.commit()
    return {"run_id": run_id, "generated": True, "walkthrough": wt}


class StuckDecideRequest(BaseModel):
    """Apply a recovery action picked from /stuck-help (action_code) OR validate + apply a
    free-text direction (custom_direction). Exactly one MUST be set."""
    action_code: str | None = None
    custom_direction: str | None = None


def _recent_event_dicts(db, run_id: str, limit: int = 10) -> list[dict]:
    """Last few events for the stuck-helper's error context (kind + payload only)."""
    from app.models.agentic import AgenticEvent
    rows = (db.query(AgenticEvent).filter(AgenticEvent.run_id == run_id)
            .order_by(AgenticEvent.seq.desc()).limit(limit).all())
    return [{"kind": r.kind, "payload": r.payload or {}} for r in reversed(rows)]


def _eligible_for_recovery(run) -> bool:
    """Server-side gate (Codex P1 fix): the UI hides the 'Ask AI what to do' button on healthy
    runs, but the API must enforce its own state precondition — a recovery action against an
    actively-progressing run can corrupt the workspace or short-circuit a working flow. Eligible:
    terminal failure / cancelled / gave up, runs carrying an error_code, runs parked at any
    human-decision gate. Healthy active runs are NOT eligible — they aren't stuck."""
    status = (getattr(run, "status", None) or "").lower()
    phase = (getattr(run, "phase", None) or "").lower()
    if status in ("failed", "gave_up", "cancelled"):
        return True
    if getattr(run, "error_code", None):
        return True
    if "awaiting" in phase or phase == "rebase_reverify":
        return True
    return False


@router.post("/agentic/runs/{run_id}/stuck-help")
async def stuck_help(run_id: str, db: DbDep, current_user: AgenticUser):
    """Ask the LLM for recovery options for a stuck run. Returns 2-3 plain-language options +
    one recommended, drawn from a CLOSED action catalog. Fail-open: a generic rerun fallback
    is always returned on parse/LLM failure, so the UI never shows an empty card."""
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if not _eligible_for_recovery(run):
        raise HTTPException(409,
            f"recovery options are only available on a stuck/failed/awaiting run "
            f"(status={run.status}, phase={run.phase})")
    from app.agents.stuck_helper import propose_recovery
    proposal = await propose_recovery(run=run, recent_events=_recent_event_dicts(db, run_id))
    return {"run_id": run_id, **proposal}


_PUSHABLE_PHASES = ("completed", "pushing", "rebase_reverify", "awaiting_human_approval")


def _resurrect_for_push(db, run) -> bool:
    """A push-preflight failure marks the run terminal (phase=failed) — but the MANIFEST stays
    approved and the change is recoverable. Move the run back to ``awaiting_human_approval``
    (the normal pre-push phase) so push_run_now's phase guard accepts it. No-op when the run is
    already in a pushable phase. Returns True when a resurrection happened (for telemetry)."""
    if run.phase in _PUSHABLE_PHASES:
        return False
    from app.agents.agentic_events import emit_event
    run.status = "active"
    run.phase = "awaiting_human_approval"
    run.lease_owner = None
    run.lease_expires_at = None
    run.cancel_requested = False
    emit_event(db, run.id, "run_resurrected_for_push",
               {"from_phase": "failed", "action": "↩ Run resurrected for a push retry"})
    db.commit()
    return True


def _dispatch_recovery(db, run, action_code: str, current_user) -> dict:
    """Apply a stuck-helper action by delegating to the existing recovery endpoints. Each branch
    raises a clean HTTPException on a bad state — no silent no-ops, no novel side effects."""
    if action_code == "rerun_code_gen":
        body = StartAgenticRequest(repo_ids=run.selected_repo_ids or [], intent="")
        rerun_code(run.change_request_id, body, db, current_user)
        return {"applied": True, "action": "rerun_code_gen", "next": "code_change_restarted"}
    if action_code == "reset_and_retry_push":
        # The git-trick fast path for BASE_DRIFT: reset workspace HEAD to the manifest's recorded
        # base (keeps working-tree files), then re-dispatch the push. Push uses the GitLab API on
        # file content, so the local-HEAD reset is what clears the preflight.
        #
        # Workspace-race guard (Codex P0/P1 fix): the workspace is SHARED across attempts on a
        # change. If a NEWER run on the same change is currently active (the usual after a
        # 'Rerun code-gen'), resetting this stale failed run's workspace would corrupt the
        # newer run's work-in-progress. Refuse cleanly BEFORE the destructive reset, so the
        # database-level uniqueness rejection that would have caught this only after damage
        # never gets the chance.
        active = S._active_run(db, run.change_request_id)
        if active is not None and active.id != run.id:
            raise HTTPException(409,
                f"another run ({str(active.id)[:8]}) is currently active on this change — "
                "the workspace can't be reset while it's in use. Abandon or wait for that run first.")
        from app.agents.agentic_orchestrator import reset_workspace_to_recorded_base
        info = reset_workspace_to_recorded_base(db, run.id)
        resurrected = _resurrect_for_push(db, run)
        push_run_now(run.id, db, current_user)
        return {"applied": True, "action": "reset_and_retry_push",
                "next": "push_dispatched", "reset": info.get("reset", 0), "resurrected": resurrected}
    if action_code == "retry_push":
        resurrected = _resurrect_for_push(db, run)
        push_run_now(run.id, db, current_user)
        return {"applied": True, "action": "retry_push", "next": "push_dispatched",
                "resurrected": resurrected}
    if action_code == "abandon":
        cancel_run(run.id, db, current_user)
        return {"applied": True, "action": "abandon", "next": "cancelled"}
    if action_code == "resume_once_more":
        resume_run(run.id, db, current_user)
        return {"applied": True, "action": "resume_once_more", "next": "resumed"}
    raise HTTPException(400, f"unknown stuck-help action_code: {action_code}")


@router.post("/agentic/runs/{run_id}/stuck-decide")
async def stuck_decide(run_id: str, body: StuckDecideRequest, db: DbDep, current_user: AgenticUser):
    """Apply a recovery choice. Two paths:
    - ``action_code``: dispatches to the matching existing endpoint (rerun/retry-push/cancel/resume).
    - ``custom_direction``: runs a SECOND LLM pass classifying the text against the catalog. On
      SAFE_AND_CLEAR + a known mapping → apply. On UNCLEAR/UNSAFE → return ``options_only``
      (frontend re-renders the options card with the textbox hidden), so the human picks instead.
    """
    run = _run_or_404(db, run_id)
    _authz_write(run, current_user)
    if not _eligible_for_recovery(run):
        raise HTTPException(409,
            f"recovery actions are only available on a stuck/failed/awaiting run "
            f"(status={run.status}, phase={run.phase})")
    if not (body.action_code or (body.custom_direction or "").strip()):
        raise HTTPException(400, "action_code or custom_direction is required")
    from app.agents.stuck_helper import ACTION_CATALOG, validate_custom_direction, propose_recovery
    if body.action_code:
        if body.action_code not in ACTION_CATALOG:
            raise HTTPException(400, f"unknown action_code: {body.action_code}")
        return _dispatch_recovery(db, run, body.action_code, current_user)
    # Free-text path: validator first.
    rec = _recent_event_dicts(db, run_id)
    verdict = await validate_custom_direction(run=run, recent_events=rec,
                                              custom_direction=body.custom_direction or "")
    if verdict["verdict"] == "SAFE_AND_CLEAR" and verdict["maps_to"]:
        return {**_dispatch_recovery(db, run, verdict["maps_to"], current_user),
                "validated": True, "mapped_to": verdict["maps_to"], "why": verdict["why"]}
    # Unsafe / unclear — bounce back to the options card with the textbox HIDDEN. Generate a
    # fresh options proposal so the human always has a current set of choices to pick from.
    proposal = await propose_recovery(run=run, recent_events=rec)
    return {"applied": False, "options_only": True, "verdict": verdict["verdict"],
            "why": verdict["why"], **proposal}


@router.get("/agentic/runs/{run_id}/walkthrough.csv")
def walkthrough_csv(run_id: str, db: DbDep, current_user: CurrentUser):
    """Download the walkthrough's tester scenarios as a QA-sheet CSV."""
    from fastapi.responses import Response
    from app.agents.change_walkthrough import scenarios_to_csv
    run = _run_or_404(db, run_id)
    _authz_read(run, current_user)
    wt = (run.handoff_json or {}).get("walkthrough")
    if not wt:
        raise HTTPException(404, "no walkthrough generated yet — generate it first")
    return Response(content=scenarios_to_csv(wt), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="walkthrough-{run_id[:8]}.csv"'})


# ── WebSocket — agentic_events subscriber (§3) ────────────────────────────────

@router.websocket("/ws/agentic/runs/{run_id}")
async def ws_agentic_events(run_id: str, websocket: WebSocket, token: str | None = None):
    """Replay all events, then stream new ones as they land. agentic_events is the
    source of truth, so a reconnect (with last seq) loses nothing — the WS is a
    pure subscriber that never owns workflow state.

    Auth: the token arrives in the FIRST JSON MESSAGE (preferred) — ``{"token":
    ..., "after_seq": N}``. A ``?token=`` query param is still accepted for
    backwards compatibility with older clients.

    Prefer the message frame. An earlier version of this docstring had it
    backwards, claiming the query param avoided "logging the JWT in message
    frames" — nothing logs frame bodies, but nginx's default ``combined`` format
    logs the entire request line, so a query-string token is written to the
    access log on every connect (and to intermediate proxy logs and browser
    history). That is a live, admin-scoped credential landing in files with
    weaker access control than the application. All in-repo callers now send the
    frame; the query param should be removed once no external client relies on
    it."""
    await websocket.accept()
    db: Session = SessionLocal()
    try:
        if token:
            after_seq = -1
        else:
            auth = json.loads(await websocket.receive_text())
            token = auth.get("token", "")
            after_seq = auth.get("after_seq", -1)
        ws_user = authenticate_ws(websocket, db, token)
        # Agentic codegen is admin + tech-lead only — gate the live event stream too.
        if ws_user is None or ws_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD):
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            await websocket.close()
            return
        if db.get(AgenticRun, run_id) is None:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Run not found"}))
            await websocket.close()
            return

        last_seq = int(after_seq)
        terminal = {"completed", "failed", "cancelled", "gave_up"}
        while True:
            for ev in events_for(db, run_id, after_seq=last_seq):
                await websocket.send_text(json.dumps({"type": "event", **ev}))
                last_seq = ev["seq"]
            run = db.get(AgenticRun, run_id)
            db.refresh(run)
            if run.status in terminal:
                await websocket.send_text(json.dumps({"type": "end", "status": run.status}))
                await websocket.close()
                return
            await asyncio.sleep(1.0)        # poll the durable feed
    except Exception as e:                  # noqa: BLE001 — client disconnects land here
        logger.info("agentic WS closed for run=%s: %s", run_id, e)
    finally:
        db.close()
