# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase B API — Design to Build.

REST endpoints for Phase B run management and code iteration history.
WebSocket endpoint for streaming code generation.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import DbDep, CurrentUser, AgenticUser, authenticate_ws
from app.core.database import SessionLocal
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.phase_b import (
    PhaseBRun, PhaseBRunStatus, PhaseBStep,
    CodeIteration, IterationTrigger,
    CodeReviewResult, ISReviewResult, ReviewStatus,
    GitEvent, GitEventStatus,
    BuildRun, BuildRunStatus, DeploymentRun,
    UATTestCase, UATTestRun, TestRunStatus,
    PhaseBRunRepo, PhaseBTriageReport,
)
from app.models.tech_spec import TechSpec
from app.models.brd import BRD, BRDStatus
from app.models.base import generate_uuid, utcnow
from app.agents.code_change import (
    stream_code_change_turn, parse_files_from_output, build_parser_context,
)
from app.agents.code_review import run_code_review, stream_code_review
from app.agents.is_review import run_is_review, stream_is_review
from app.rag.code_ingestion import ingest_codebase
from app.services.git_integrator import content_hash, push_to_gitlab, push_to_gitlab_multi
# R-7 — durable agent-job registry for Phase B long-running step calls.
from app.services import job_registry

logger = logging.getLogger(__name__)
router = APIRouter(tags=["phase-b"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_change_or_404(change_id: str, db: Session) -> ChangeRequest:
    cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    return cr


def _get_run_or_404(change_id: str, db: Session) -> PhaseBRun:
    run = (
        db.query(PhaseBRun)
        .filter(PhaseBRun.change_request_id == change_id)
        .order_by(PhaseBRun.started_at.desc())
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="Phase B run not started yet")
    return run


def _load_tech_spec(change_id: str, db: Session) -> str:
    row = (
        db.query(TechSpec)
        .filter(TechSpec.change_request_id == change_id)
        .order_by(TechSpec.version.desc())
        .first()
    )
    return row.content if row and row.content else ""


def _load_brd(change_id: str, db: Session) -> str:
    row = (
        db.query(BRD)
        .filter(BRD.change_request_id == change_id)
        .order_by(BRD.version.desc())
        .first()
    )
    return row.content if row and row.content else ""


def _iteration_history_to_messages(iterations: list[CodeIteration]) -> list[dict]:
    """Convert stored code iterations into Claude conversation turns."""
    messages: list[dict] = []
    for it in iterations:
        # Reconstruct: user turn is the feedback (or initial trigger), assistant is generated output
        user_text = it.user_feedback or ("start" if it.trigger == IterationTrigger.INITIAL else "")
        if user_text:
            messages.append({"role": "user", "content": user_text})
        if it.generated_output:
            messages.append({"role": "assistant", "content": it.generated_output})
    return messages


# ── REST — Phase B run management ─────────────────────────────────────────────

class StartPhaseBRequest(BaseModel):
    """Phase B start payload.

    Multi-repo support (M-2):
      - `repo_ids`: preferred path. List of CodeRepo.id values; one
        phase_b_run_repos row is created per id, all sharing one branch
        name (`branch_override` if provided, else auto-generated).
      - Legacy `gitlab_repo` + `gitlab_branch`: still accepted for back-
        compat. If `repo_ids` is empty/absent, we resolve `gitlab_repo`
        to a CodeRepo and store it as the single repo for the run.

    `branch_override` lets operators pin a specific branch name (same name
    applied across all repos in the run). When omitted, the run continues the
    agentic run's provisioned feature branch for this change (one combined MR
    with the Phase-A XSD work), else we generate `change-<short>/iter-1`.
    The run NEVER defaults to a repo's base branch — `gitlab_branch` is the
    legacy BASE-branch binding, not the branch the run pushes to.
    """
    gitlab_repo: str | None = None
    gitlab_branch: str | None = None
    repo_ids: list[str] | None = None
    branch_override: str | None = None


def _agentic_feature_branch(db: Session, change_id: str) -> str | None:
    """The feature branch this change's agentic run(s) provisioned their workspace
    on (``handoff_json['feature_branch']``) — Phase B continues the SAME branch so
    the XSD work and the code land as one combined MR. Newest run wins; None when
    no agentic run recorded a branch (runs predating branch-at-provisioning, or a
    change with no agentic phase)."""
    from app.models.agentic import AgenticRun
    rows = (db.query(AgenticRun)
            .filter(AgenticRun.change_request_id == change_id)
            .order_by(AgenticRun.created_at.desc()).limit(20).all())
    for r in rows:
        fb = ((r.handoff_json or {}).get("feature_branch") or "").strip()
        if fb:
            return fb
    return None


@router.post("/changes/{change_id}/phase-b/start")
def start_phase_b(change_id: str, body: StartPhaseBRequest, db: DbDep, current_user: CurrentUser):
    """Initialise a Phase B run for a change request that has completed Phase A."""
    from app.models.code_repo import CodeRepo

    cr = _get_change_or_404(change_id, db)
    if cr.status != ChangeStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Phase A must be completed before starting Phase B")

    # Downstream gate: any unresolved uploaded-doc reconciliation (BRD or TSD) blocks codegen.
    from app.agents.upload_reconciler import has_unresolved_reconciliation
    if has_unresolved_reconciliation(db, change_id):
        raise HTTPException(status_code=409,
                            detail="Resolve the uploaded-document reconciliation conflicts before generating code.")

    # Idempotent — return existing run if already started
    existing = db.query(PhaseBRun).filter(PhaseBRun.change_request_id == change_id).first()
    if existing:
        logger.info("Phase B already exists: change=%s run=%s step=%s", change_id, existing.id, existing.current_step)
        # Surface the per-repo summary even on the idempotent return so the UI
        # can render the multi-repo card immediately on a re-mount.
        repos = (
            db.query(PhaseBRunRepo, CodeRepo)
            .join(CodeRepo, CodeRepo.id == PhaseBRunRepo.repo_id)
            .filter(PhaseBRunRepo.run_id == existing.id)
            .all()
        )
        return {
            "run_id":       existing.id,
            "current_step": existing.current_step,
            "status":       existing.status,
            "repos": [
                {"repo_id": r.repo_id, "label": cr_obj.label,
                 "gitlab_repo": cr_obj.gitlab_repo, "branch": r.branch}
                for r, cr_obj in repos
            ],
        }

    # ── Resolve repos ─────────────────────────────────────────────────────────
    # New path: repo_ids → list of CodeRepo rows.
    # Legacy path: gitlab_repo string → CodeRepo lookup.
    code_repos: list[CodeRepo] = []
    if body.repo_ids:
        # De-dup while preserving order (operators sometimes pass duplicates).
        seen_ids: set[str] = set()
        for rid in body.repo_ids:
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            cr_obj = db.get(CodeRepo, rid)
            if cr_obj is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown repo_id: {rid}",
                )
            code_repos.append(cr_obj)
    elif body.gitlab_repo:
        cr_obj = (
            db.query(CodeRepo)
            .filter(CodeRepo.gitlab_repo == body.gitlab_repo)
            .first()
        )
        if cr_obj is None:
            # Tolerate legacy callers that pass an unregistered repo string.
            # The run still starts, but no phase_b_run_repos row is created
            # and the multi-repo features won't activate for this run.
            logger.warning(
                "Phase B start: gitlab_repo=%s not found in code_repos — falling back to legacy single-repo run",
                body.gitlab_repo,
            )
        else:
            code_repos.append(cr_obj)
    else:
        # No repo specified → fall through to legacy single-repo run with
        # null gitlab_repo. Code-gen will see all indexed repos in retrieval
        # but git push will fail until a repo is configured.
        logger.info("Phase B start: no repos specified for change=%s", change_id)

    # Compute the shared FEATURE branch. Never default to a repo's base branch:
    # that made the git step commit the run's work directly onto main whenever no
    # override was given ("branch already exists" was swallowed downstream and the
    # commit went straight through). Precedence: operator override > the agentic
    # run's provisioned branch (continue the branch the Phase-A XSD work already
    # lives on — one combined MR) > the documented generated fallback.
    primary_repo = code_repos[0] if code_repos else None
    base_branches = {cr.gitlab_branch or "main" for cr in code_repos} | {"main", "master"}
    override = (body.branch_override or "").strip()
    if override and override in base_branches:
        raise HTTPException(
            status_code=400,
            detail=(f"branch_override '{override}' is a base branch — "
                    "pass a feature branch name instead."))
    branch_name = (
        override
        or _agentic_feature_branch(db, change_id)
        or f"change-{change_id[:8]}/iter-1"
    )

    run = PhaseBRun(
        id=generate_uuid(),
        change_request_id=change_id,
        status=PhaseBRunStatus.IN_PROGRESS,
        current_step=PhaseBStep.CODE_CHANGE,
        iteration_count=0,
        # Singular columns retained for back-compat: store the FIRST repo
        # so legacy UI / single-repo `git/push` callers still work.
        gitlab_repo=(primary_repo.gitlab_repo if primary_repo else body.gitlab_repo),
        gitlab_branch=branch_name,
        started_at=utcnow(),
    )
    db.add(run)
    db.flush()  # need run.id before inserting phase_b_run_repos rows

    # Insert one phase_b_run_repos row per resolved CodeRepo. All rows
    # share the same `branch` value so cross-repo MRs are easy to find.
    for cr_obj in code_repos:
        db.add(PhaseBRunRepo(
            id=generate_uuid(),
            run_id=run.id,
            repo_id=cr_obj.id,
            branch=branch_name,
        ))

    db.commit()
    db.refresh(run)
    logger.info(
        "Phase B started: change=%s run=%s repos=%d branch=%s",
        change_id, run.id, len(code_repos), branch_name,
    )
    return {
        "run_id":       run.id,
        "current_step": run.current_step,
        "status":       run.status,
        "repos": [
            {"repo_id": r.id, "label": r.label,
             "gitlab_repo": r.gitlab_repo, "branch": branch_name}
            for r in code_repos
        ],
    }


@router.get("/changes/{change_id}/phase-b")
def get_phase_b(change_id: str, db: DbDep, current_user: CurrentUser):
    """Get the current Phase B run summary for a change request."""
    from app.models.code_repo import CodeRepo

    run = _get_run_or_404(change_id, db)
    iterations = (
        db.query(CodeIteration)
        .filter(CodeIteration.phase_b_run_id == run.id)
        .order_by(CodeIteration.iteration_number)
        .all()
    )
    # M-2 — multi-repo summary. Empty list for legacy runs that didn't go
    # through the multi-repo start path; UI continues to fall back to the
    # singular gitlab_repo / gitlab_branch fields when this is empty.
    repo_rows = (
        db.query(PhaseBRunRepo, CodeRepo)
        .join(CodeRepo, CodeRepo.id == PhaseBRunRepo.repo_id)
        .filter(PhaseBRunRepo.run_id == run.id)
        .all()
    )
    return {
        "run_id": run.id,
        "status": run.status,
        "current_step": run.current_step,
        "iteration_count": run.iteration_count,
        "gitlab_repo": run.gitlab_repo,
        "gitlab_branch": run.gitlab_branch,
        "started_at": run.started_at.isoformat(),
        "repos": [
            {
                "repo_id":     pbrr.repo_id,
                "label":       cr.label,
                "gitlab_repo": cr.gitlab_repo,
                "role":        getattr(cr, "role", None) or "app",
                "branch":      pbrr.branch,
                "mr_url":      pbrr.mr_url,
                "mr_iid":      pbrr.mr_iid,
                "mr_state":    pbrr.mr_state,
            }
            for pbrr, cr in repo_rows
        ],
        "iterations": [
            {
                "iteration_number": it.iteration_number,
                "trigger": it.trigger,
                "approved": it.approved,
                "files_count": len(it.files_changed or []),
                "created_at": it.created_at.isoformat(),
            }
            for it in iterations
        ],
    }


@router.delete("/changes/{change_id}/phase-b")
def reset_phase_b(change_id: str, db: DbDep, current_user: CurrentUser):
    """Delete all Phase B data for a change request so it can be restarted fresh."""
    run = (
        db.query(PhaseBRun)
        .filter(PhaseBRun.change_request_id == change_id)
        .order_by(PhaseBRun.started_at.desc())
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="No Phase B run found")

    run_id = run.id

    # Delete child tables (order matters for FK constraints).
    # Triage reports reference the run AND build/uat rows — they go first.
    db.query(PhaseBTriageReport).filter(PhaseBTriageReport.phase_b_run_id == run_id).delete()
    from app.models.phase_b import UATTestResult as _UATTestResult
    _run_ids = db.query(UATTestRun.id).filter(UATTestRun.phase_b_run_id == run_id)
    db.query(_UATTestResult).filter(_UATTestResult.test_run_id.in_(_run_ids.subquery())) \
        .delete(synchronize_session=False)
    db.query(GitEvent).filter(GitEvent.phase_b_run_id == run_id).delete()
    db.query(DeploymentRun).filter(DeploymentRun.phase_b_run_id == run_id).delete()
    db.query(BuildRun).filter(BuildRun.phase_b_run_id == run_id).delete()
    db.query(UATTestCase).filter(UATTestCase.phase_b_run_id == run_id).delete()
    db.query(UATTestRun).filter(UATTestRun.phase_b_run_id == run_id).delete()

    # Delete review results via code iterations
    iteration_ids = [
        it.id for it in
        db.query(CodeIteration).filter(CodeIteration.phase_b_run_id == run_id).all()
    ]
    if iteration_ids:
        db.query(ISReviewResult).filter(ISReviewResult.code_iteration_id.in_(iteration_ids)).delete(synchronize_session=False)
        db.query(CodeReviewResult).filter(CodeReviewResult.code_iteration_id.in_(iteration_ids)).delete(synchronize_session=False)

    db.query(CodeIteration).filter(CodeIteration.phase_b_run_id == run_id).delete()
    db.query(PhaseBRun).filter(PhaseBRun.id == run_id).delete()
    db.commit()

    logger.info("Phase B reset: change=%s run=%s", change_id, run_id)
    return {"deleted": True, "run_id": run_id}


@router.get("/changes/{change_id}/phase-b/code/iterations")
def list_code_iterations(change_id: str, db: DbDep, current_user: CurrentUser):
    """List all code iterations with summary (no full content)."""
    run = _get_run_or_404(change_id, db)
    iterations = (
        db.query(CodeIteration)
        .filter(CodeIteration.phase_b_run_id == run.id)
        .order_by(CodeIteration.iteration_number)
        .all()
    )
    return [
        {
            "id": it.id,
            "iteration_number": it.iteration_number,
            "trigger": it.trigger,
            "approved": it.approved,
            "files_changed": it.files_changed or [],
            "user_feedback": it.user_feedback,
            "created_at": it.created_at.isoformat(),
        }
        for it in iterations
    ]


@router.get("/changes/{change_id}/phase-b/code/iterations/{iteration_number}")
def get_code_iteration(change_id: str, iteration_number: int, db: DbDep, current_user: CurrentUser):
    """Get a specific code iteration with full generated output."""
    run = _get_run_or_404(change_id, db)
    it = (
        db.query(CodeIteration)
        .filter(
            CodeIteration.phase_b_run_id == run.id,
            CodeIteration.iteration_number == iteration_number,
        )
        .first()
    )
    if not it:
        raise HTTPException(status_code=404, detail=f"Iteration {iteration_number} not found")
    return {
        "id": it.id,
        "iteration_number": it.iteration_number,
        "generated_output": it.generated_output,
        "files_changed": it.files_changed or [],
        "user_feedback": it.user_feedback,
        "trigger": it.trigger,
        "approved": it.approved,
        "created_at": it.created_at.isoformat(),
    }


@router.post("/changes/{change_id}/phase-b/code/iterations/{iteration_number}/approve")
def approve_code_iteration(change_id: str, iteration_number: int, db: DbDep, current_user: CurrentUser):
    """Mark a code iteration as approved, advancing the run to Code Review."""
    run = _get_run_or_404(change_id, db)
    it = (
        db.query(CodeIteration)
        .filter(
            CodeIteration.phase_b_run_id == run.id,
            CodeIteration.iteration_number == iteration_number,
        )
        .first()
    )
    if not it:
        raise HTTPException(status_code=404, detail=f"Iteration {iteration_number} not found")

    it.approved = True
    run.current_step = PhaseBStep.CODE_REVIEW
    db.commit()
    logger.info("Code iteration approved: change=%s iteration=%d → step=code_review", change_id, iteration_number)
    return {"approved": True, "next_step": PhaseBStep.CODE_REVIEW}


# ── REST — Codebase ingestion ──────────────────────────────────────────────────

class IngestCodebaseRequest(BaseModel):
    gitlab_repo: str | None = None
    gitlab_branch: str | None = None


@router.post("/changes/{change_id}/phase-b/ingest-codebase")
def ingest_codebase_endpoint(change_id: str, body: IngestCodebaseRequest, db: DbDep, current_user: CurrentUser):
    """Fetch Java source files from GitLab and store in Code RAG (pgvector)."""
    _get_change_or_404(change_id, db)
    try:
        result = ingest_codebase(
            db=db,
            change_request_id=change_id,
            repo=body.gitlab_repo,
            branch=body.gitlab_branch,
        )
        return result
    except ValueError as e:
        # SCR finding #6 — do not echo the raw exception text to the caller
        # (CWE-209). Log the real message server-side only.
        logger.warning("Code RAG ingestion rejected: %s", e)
        raise HTTPException(status_code=400, detail="Codebase ingestion request was invalid")
    except Exception as e:
        logger.exception("Code RAG ingestion failed: %s", e)
        raise HTTPException(status_code=500, detail="Ingestion failed")


# ── REST — Code Review ─────────────────────────────────────────────────────────

def _get_latest_approved_files(run: PhaseBRun, db: Session) -> tuple[CodeIteration | None, list[dict]]:
    """Return the latest approved iteration and its files_changed list."""
    it = (
        db.query(CodeIteration)
        .filter(
            CodeIteration.phase_b_run_id == run.id,
            CodeIteration.approved == True,
        )
        .order_by(CodeIteration.iteration_number.desc())
        .first()
    )
    if not it:
        return None, []
    return it, it.files_changed or []


@router.post("/changes/{change_id}/phase-b/code-review")
async def trigger_code_review(change_id: str, db: DbDep, current_user: CurrentUser):
    """Run AI code review on the latest approved iteration's files."""
    run = _get_run_or_404(change_id, db)
    if run.current_step not in (PhaseBStep.CODE_REVIEW, PhaseBStep.CODE_CHANGE):
        raise HTTPException(status_code=400, detail=f"Cannot run code review at step '{run.current_step}'")

    iteration, files = _get_latest_approved_files(run, db)
    if not iteration:
        raise HTTPException(status_code=400, detail="No approved code iteration found. Approve code first.")

    # Gather previous review issues (if this is a re-review after loop-back)
    previous_issues = []
    prev_review = (
        db.query(CodeReviewResult)
        .join(CodeIteration, CodeReviewResult.code_iteration_id == CodeIteration.id)
        .filter(CodeIteration.phase_b_run_id == run.id)
        .order_by(CodeReviewResult.created_at.desc())
        .first()
    )
    if prev_review and prev_review.issues:
        previous_issues = prev_review.issues

    # R-7 — track as a durable job so the sidebar tray + audit log
    # surface this in-flight step.
    with job_registry.tracked_step(
        db,
        change_request_id=change_id,
        module="phase_b",
        subtype="code_review",
        user_id=current_user.id,
        initial_stage=f"Reviewing {len(files)} file(s)",
        metadata={"iteration_id": iteration.id, "files_count": len(files)},
    ) as tracker_id:
        logger.info("Code review started: change=%s files=%d job=%s", change_id, len(files), tracker_id)
        result = await run_code_review(files, previous_issues=previous_issues)

        # Persist — store full result (issues + stats + rules_checked) in the JSON column
        status = ReviewStatus.CLEAN if result.get("status") == "clean" else ReviewStatus.ISSUES_FOUND
        review_data = {
            "issues": result.get("issues", []),
            "rules_checked": result.get("rules_checked", {}),
            "stats": result.get("stats", {}),
            "summary": result.get("summary", ""),
        }
        review = CodeReviewResult(
            id=generate_uuid(),
            code_iteration_id=iteration.id,
            status=status,
            issues=review_data,
            created_at=utcnow(),
        )
        db.add(review)

        stats = result.get("stats", {})
        issue_count = len(result.get("issues", []))
        sq_count = stats.get("sonarqube_issues", 0)
        pmd_count = stats.get("pmd_issues", 0)
        logger.info("Code review done: change=%s status=%s issues=%d sonarqube=%d pmd=%d", change_id, status, issue_count, sq_count, pmd_count)

        # Advance step if clean, stay if issues found
        # IS Review is disabled — skip directly to GIT
        if status == ReviewStatus.CLEAN:
            run.current_step = PhaseBStep.GIT
        db.commit()

    return {
        "status": result.get("status"),
        "summary": result.get("summary", ""),
        "rules_checked": result.get("rules_checked", {}),
        "stats": result.get("stats", {}),
        "issues": result.get("issues", []),
        "next_step": run.current_step,
        "job_id": tracker_id,
    }


@router.get("/changes/{change_id}/phase-b/code-review/latest")
def get_latest_code_review(change_id: str, db: DbDep, current_user: CurrentUser):
    """Get the latest code review result."""
    run = _get_run_or_404(change_id, db)
    review = (
        db.query(CodeReviewResult)
        .join(CodeIteration, CodeReviewResult.code_iteration_id == CodeIteration.id)
        .filter(CodeIteration.phase_b_run_id == run.id)
        .order_by(CodeReviewResult.created_at.desc())
        .first()
    )
    if not review:
        raise HTTPException(status_code=404, detail="No code review found")
    raw = review.issues or {}
    # Handle both old format (list of issues) and new format (dict with issues, stats, rules_checked)
    if isinstance(raw, list):
        issues = raw
        rules_checked = {}
        stats = {}
        summary = ""
    else:
        issues = raw.get("issues", [])
        rules_checked = raw.get("rules_checked", {})
        stats = raw.get("stats", {})
        summary = raw.get("summary", "")

    return {
        "id": review.id,
        "status": review.status,
        "summary": summary,
        "rules_checked": rules_checked,
        "stats": stats,
        "issues": issues,
        "iteration_id": review.code_iteration_id,
        "created_at": review.created_at.isoformat(),
    }


@router.post("/changes/{change_id}/phase-b/code-review/loop-back")
async def code_review_loop_back(change_id: str, db: DbDep, current_user: CurrentUser):
    """Route back to Code Change Agent with code review issues as context."""
    run = _get_run_or_404(change_id, db)
    review = (
        db.query(CodeReviewResult)
        .join(CodeIteration, CodeReviewResult.code_iteration_id == CodeIteration.id)
        .filter(CodeIteration.phase_b_run_id == run.id)
        .order_by(CodeReviewResult.created_at.desc())
        .first()
    )
    if not review or review.status != ReviewStatus.ISSUES_FOUND:
        raise HTTPException(status_code=400, detail="No code review issues to loop back")

    run.current_step = PhaseBStep.CODE_CHANGE
    db.commit()
    logger.info("Code review loop-back: change=%s", change_id)
    return {"looped_back": True, "current_step": PhaseBStep.CODE_CHANGE}


# ── REST — IS Review ───────────────────────────────────────────────────────────

@router.post("/changes/{change_id}/phase-b/is-review")
async def trigger_is_review(change_id: str, db: DbDep, current_user: CurrentUser):
    """Run AI IS (security) review on the latest approved iteration's files."""
    run = _get_run_or_404(change_id, db)
    if run.current_step not in (PhaseBStep.IS_REVIEW, PhaseBStep.CODE_REVIEW):
        raise HTTPException(status_code=400, detail=f"Cannot run IS review at step '{run.current_step}'")

    iteration, files = _get_latest_approved_files(run, db)
    if not iteration:
        raise HTTPException(status_code=400, detail="No approved code iteration found.")

    # R-7 — track as a durable job
    with job_registry.tracked_step(
        db,
        change_request_id=change_id,
        module="phase_b",
        subtype="is_review",
        user_id=current_user.id,
        initial_stage=f"Reviewing security on {len(files)} file(s)",
        metadata={"iteration_id": iteration.id, "files_count": len(files)},
    ) as tracker_id:
        logger.info("IS review started: change=%s files=%d job=%s", change_id, len(files), tracker_id)
        result = await run_is_review(files)

        status = ReviewStatus.CLEAN if result.get("status") == "clean" else ReviewStatus.ISSUES_FOUND
        review = ISReviewResult(
            id=generate_uuid(),
            code_iteration_id=iteration.id,
            status=status,
            findings=result.get("findings", []),
            created_at=utcnow(),
        )
        db.add(review)

        findings_count = len(result.get("findings", []))
        logger.info("IS review done: change=%s status=%s findings=%d", change_id, status, findings_count)

        if status == ReviewStatus.CLEAN:
            run.current_step = PhaseBStep.GIT
        else:
            # Advance to IS_REVIEW so the frontend shows the findings panel
            run.current_step = PhaseBStep.IS_REVIEW
        db.commit()

    return {
        "status": result.get("status"),
        "summary": result.get("summary", ""),
        "findings": result.get("findings", []),
        "next_step": run.current_step,
        "job_id": tracker_id,
    }


@router.get("/changes/{change_id}/phase-b/is-review/latest")
def get_latest_is_review(change_id: str, db: DbDep, current_user: CurrentUser):
    """Get the latest IS review result."""
    run = _get_run_or_404(change_id, db)
    review = (
        db.query(ISReviewResult)
        .join(CodeIteration, ISReviewResult.code_iteration_id == CodeIteration.id)
        .filter(CodeIteration.phase_b_run_id == run.id)
        .order_by(ISReviewResult.created_at.desc())
        .first()
    )
    if not review:
        raise HTTPException(status_code=404, detail="No IS review found")
    return {
        "id": review.id,
        "status": review.status,
        "findings": review.findings or [],
        "iteration_id": review.code_iteration_id,
        "created_at": review.created_at.isoformat(),
    }


@router.post("/changes/{change_id}/phase-b/is-review/loop-back")
async def is_review_loop_back(change_id: str, db: DbDep, current_user: CurrentUser):
    """Route back to Code Change Agent with IS review findings as context."""
    run = _get_run_or_404(change_id, db)
    review = (
        db.query(ISReviewResult)
        .join(CodeIteration, ISReviewResult.code_iteration_id == CodeIteration.id)
        .filter(CodeIteration.phase_b_run_id == run.id)
        .order_by(ISReviewResult.created_at.desc())
        .first()
    )
    if not review or review.status != ReviewStatus.ISSUES_FOUND:
        raise HTTPException(status_code=400, detail="No IS review findings to loop back")

    run.current_step = PhaseBStep.CODE_CHANGE
    db.commit()
    logger.info("IS review loop-back: change=%s", change_id)
    return {"looped_back": True, "current_step": PhaseBStep.CODE_CHANGE}


# ── REST — Advance to next step ──────────────────────────────────────────────

# Steps that can be advanced via this generic endpoint.
#
# Session 23 — the unified build+deploy flow rolls clone+build+deploy+
# service-startup into the BUILD step. The standalone DEPLOY panel is gone
# from the UI, so a manual skip from BUILD now jumps straight to TEST_GEN.
# The DEPLOY → TEST_GEN row is kept only so legacy in-flight runs already
# parked at DEPLOY can still advance.
_ADVANCEABLE_STEPS = {
    PhaseBStep.CODE_REVIEW: PhaseBStep.GIT,    # skip code review (IS review disabled)
    PhaseBStep.GIT:         PhaseBStep.BUILD,
    PhaseBStep.BUILD:       PhaseBStep.TEST_GEN,
    PhaseBStep.DEPLOY:      PhaseBStep.TEST_GEN,
    # UAT is ONE script-based step now (gen + exec combined), so a manual skip
    # from it jumps straight to TRIAGE. The TEST_EXEC row is kept only so
    # legacy in-flight runs already parked there can still advance.
    PhaseBStep.TEST_GEN:    PhaseBStep.TRIAGE,
    PhaseBStep.TEST_EXEC:   PhaseBStep.TRIAGE,
}


def _assert_governance_passed(db, change_id: str) -> None:
    """The governance gate for EVERY transition into BUILD — the agentic bridge,
    the manual advance-step, and the build trigger itself all call this, so no
    entry point (including the legacy pipeline's GIT step) reaches Build while a
    pre-build review stage (EA → InfoSec) is pending. Inert with the flag off."""
    from app.core.config import settings as _settings
    if not getattr(_settings, "governance_reviews_enabled", False):
        return
    from app.agents.governance_orchestrator import governance_status
    _gs = governance_status(db, change_id)
    if not _gs["all_passed"]:
        _pend = [s["label"] for s in (_gs["ea"], _gs["infosec"]) if not s["passed"]]
        raise HTTPException(status_code=409,
                            detail="governance reviews must pass before Build — pending: "
                                   + ", ".join(_pend)
                                   + ". Start/finish them from the Phase B page.")


@router.post("/changes/{change_id}/phase-b/advance-step")
def advance_phase_b_step(change_id: str, db: DbDep, current_user: CurrentUser):
    """Advance the Phase B run to the next pipeline step."""
    run = _get_run_or_404(change_id, db)

    if run.current_step == PhaseBStep.TRIAGE:
        # Final step — mark run as completed
        run.status = PhaseBRunStatus.COMPLETED
        run.completed_at = utcnow()
        db.commit()
        logger.info("Phase B completed: change=%s", change_id)
        return {"current_step": "completed", "status": "completed"}

    next_step = _ADVANCEABLE_STEPS.get(run.current_step)
    if not next_step:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot advance from step '{run.current_step}'. Use the specific endpoint for this step.",
        )
    if next_step == PhaseBStep.BUILD:
        _assert_governance_passed(db, change_id)

    logger.info("Step advanced: change=%s %s → %s", change_id, run.current_step, next_step)
    run.current_step = next_step
    db.commit()
    return {"current_step": next_step.value}


@router.post("/changes/{change_id}/phase-b/agentic-complete")
def agentic_complete(change_id: str, db: DbDep, current_user: AgenticUser):
    """Hand the pipeline over from the agentic run to the remaining legacy steps.

    The agentic run covers CODE_CHANGE + CODE_REVIEW + GIT (it writes the code,
    reviews it, and raises the MR on approval), so once its manifest is human-
    approved AND the run is completed (pushed or push-deferred) this jumps the
    Phase B pipeline straight to BUILD — creating the run row when the legacy
    pipeline was never started (the agentic path bypasses it entirely).

    Gated to admin/tech_lead (AgenticUser); the handoff must be authorised by the
    agentic run's author or an admin, mirroring the per-run rule on /approve."""
    from app.models.agentic import AgenticRun, ChangeManifest, AgenticRunRepo
    from app.models.user import UserRole

    aruns = (db.query(AgenticRun)
             .filter(AgenticRun.change_request_id == change_id,
                     AgenticRun.kind.in_(("code", "full")))
             .order_by(AgenticRun.created_at.desc()).all())
    approved = None
    for r in aruns:
        man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == r.id)
               .order_by(ChangeManifest.created_at.desc()).first())
        if man is not None and man.approved_at is not None:
            approved = r
            break
    if approved is None:
        raise HTTPException(status_code=409, detail="no approved agentic code change for this change — approve it first")
    # Per-run authz: only the run's author or an admin can hand off (matches /approve).
    owner = getattr(approved, "created_by", None)
    if owner is not None and current_user.role != UserRole.ADMIN and current_user.id != owner:
        raise HTTPException(status_code=403, detail="only the run's author or an admin can advance this change")
    # The agentic run must actually be finished — completed AND either pushed to git
    # or explicitly push-deferred. Otherwise the change would move to Build while the
    # code is still being generated or was never landed.
    pushed = db.query(AgenticRunRepo.run_id).filter(
        AgenticRunRepo.run_id == approved.id, AgenticRunRepo.push_state == "pushed").first() is not None
    deferred = bool((getattr(approved, "handoff_json", None) or {}).get("push_deferred"))
    if approved.status != "completed" or not (pushed or deferred):
        raise HTTPException(status_code=409,
                            detail="agentic run is not finished — approve & push (or push later) before advancing")
    # Governance gate: Build unlocks only when BOTH pre-build review stages
    # (EA → InfoSec) passed — clean, fixes approved, or explicitly overridden with
    # an audited reason. Shared with advance-step and the build trigger so every
    # path into BUILD is gated, not just this bridge.
    _assert_governance_passed(db, change_id)

    run = (
        db.query(PhaseBRun)
        .filter(PhaseBRun.change_request_id == change_id)
        .order_by(PhaseBRun.started_at.desc())
        .first()
    )
    if run is None:
        run = PhaseBRun(change_request_id=change_id)
        db.add(run)
    run.current_step = PhaseBStep.BUILD
    db.commit()
    logger.info("Agentic handover: change=%s → BUILD (agentic run %s approved)", change_id, approved.id)
    return {"current_step": PhaseBStep.BUILD.value}


# ── REST — UAT tests (combined gen + exec, script-based) ─────────────────────
# One operator script produces AND executes the suite; its streamed log is the
# artefact (shown live in the UI, read by AI triage). The legacy mock
# endpoints (test-gen/trigger + test-exec/trigger, uat_mock.py) are gone; the
# two GET routes stay so historical mock rows still render.

def _serialise_test_case(c: UATTestCase) -> dict:
    return {
        "id":               c.id,
        "test_id":          c.test_id,
        "suite_version":    c.suite_version,
        "category":         c.category.value if c.category else None,
        "title":            c.title,
        "description":      c.description,
        "preconditions":    c.preconditions,
        "http_method":      c.http_method,
        "endpoint":         c.endpoint,
        "request_headers":  c.request_headers,
        "request_payload":  c.request_payload,
        "expected_status":  c.expected_status,
        "expected_response": c.expected_response,
        "pass_criteria":    c.pass_criteria,
    }


def _serialise_test_run(r: UATTestRun) -> dict:
    return {
        "id":             r.id,
        "suite_version":  r.suite_version,
        "iteration_number": r.iteration_number,
        "base_url":       r.base_url,
        "script_path":    r.script_path,
        "log":            r.log,
        "total":          r.total,
        "passed":         r.passed,
        "failed":         r.failed,
        "skipped":        r.skipped,
        "status":         r.status.value if r.status else None,
        "started_at":     r.started_at.isoformat() if r.started_at else None,
        "completed_at":   r.completed_at.isoformat() if r.completed_at else None,
    }


class UatTestTriggerRequest(BaseModel):
    """Payload for the combined UAT step. `script_path` names WHICH test
    script runs (validated against PHASE_B_SCRIPT_ROOT, admin/tech_lead,
    local runner mode only); omitted = the operator-configured
    PHASE_B_TEST_SCRIPT default, which works in any mode and for any
    authenticated operator — the elevation guards CHOOSING a script, not
    running the configured one. `base_url` is the deployment under test,
    passed to the script as $1."""
    script_path: str | None = None
    base_url: str | None = None


def _base_url_or_400(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    from urllib.parse import urlparse
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or len(raw) > 500 \
            or any(c.isspace() for c in raw):
        raise HTTPException(status_code=400,
                            detail="base_url must be a plain http(s) URL")
    # Target-address guard (same levers as partner endpoint URLs): the script
    # fetches this URL and its RESPONSE lands in a log any authenticated user
    # can read, so loopback/link-local/metadata targets are refused. Legit
    # internal UAT hosts go through the operator's ssrf allowlist /
    # allow-private settings, exactly like api/partners.py.
    from app.core.ssrf_guard import SsrfBlocked, check_outbound_url, parse_allowlist
    try:
        check_outbound_url(
            raw,
            mode=settings.ssrf_guard_mode,
            allowlist=parse_allowlist(settings.ssrf_allowed_internal_hosts),
            allow_private=settings.ssrf_allow_private_networks,
            context="uat base_url",
            block_on_resolution_failure=settings.ssrf_block_on_resolution_failure,
        )
    except SsrfBlocked as exc:
        raise HTTPException(
            status_code=400,
            detail=f"base_url refused: {exc.reason}. If this is a legitimate "
                   "internal UAT target, add its host to SSRF_ALLOWED_INTERNAL_HOSTS.")
    return raw


def _uat_script_or_400(body: "UatTestTriggerRequest", current_user) -> str:
    """The script the combined UAT step will run.

    Request-supplied path → full allowlist validation + role gate (an operator
    choosing WHICH script runs is the new execution surface). Omitted → the
    operator-configured PHASE_B_TEST_SCRIPT (same trust as PHASE_B_BUILD_SCRIPT),
    runnable by any authenticated operator in any runner mode; only its
    existence is checked so a misconfigured path fails loud here, not as a
    one-line failed run."""
    from pathlib import Path

    if body.script_path and body.script_path.strip():
        return _resolve_script_or_400(body.script_path, current_user, feature="UAT test")
    configured = (settings.phase_b_test_script or "").strip()
    if not configured:
        raise HTTPException(
            status_code=400,
            detail="no UAT test script is configured — set PHASE_B_TEST_SCRIPT, "
                   "or supply script_path (local runner mode with PHASE_B_SCRIPT_ROOT)")
    if not Path(configured).is_file():
        logger.error("PHASE_B_TEST_SCRIPT does not exist: %r", configured)
        raise HTTPException(status_code=400,
                            detail="the configured UAT test script is missing on this host — "
                                   "check PHASE_B_TEST_SCRIPT")
    return configured


@router.post("/changes/{change_id}/phase-b/test/trigger")
async def trigger_uat_tests(
    change_id: str,
    body: UatTestTriggerRequest,
    db: DbDep,
    current_user: CurrentUser,
):
    """Run the combined UAT step (gen + exec in one operator script).

    Returns the RUNNING UATTestRun immediately; the script continues in the
    background, its output streaming onto the row (poll GET /test-runs/latest).
    Once the script has run — pass or fail — the pipeline advances to TRIAGE,
    which is the step that looks at failures."""
    from app.services.uat_script import run_uat_script

    run = _get_run_or_404(change_id, db)
    # TEST_EXEC accepted for legacy in-flight runs parked there by the old
    # two-step flow — the combined step subsumes both.
    if run.current_step not in (PhaseBStep.TEST_GEN, PhaseBStep.TEST_EXEC):
        raise HTTPException(status_code=400,
                            detail=f"Cannot run UAT tests at step '{run.current_step}'")

    script_path = _uat_script_or_400(body, current_user)
    base_url = _base_url_or_400(body.base_url)

    latest = (db.query(UATTestRun)
              .filter(UATTestRun.phase_b_run_id == run.id)
              .order_by(UATTestRun.started_at.desc())
              .first())
    if latest and latest.status == TestRunStatus.RUNNING and latest.started_at and \
            (utcnow() - latest.started_at).total_seconds() < _stale_after_s():
        raise HTTPException(status_code=409,
                            detail="a UAT run is already in progress for this change — "
                                   "its log is streaming on the UAT panel")

    iteration = (db.query(UATTestRun)
                 .filter(UATTestRun.phase_b_run_id == run.id).count()) + 1
    test_run = UATTestRun(
        id=generate_uuid(),
        phase_b_run_id=run.id,
        suite_version=0,          # script-based: no generated case suite behind it
        iteration_number=iteration,
        base_url=base_url,
        script_path=script_path,
        status=TestRunStatus.RUNNING,
        started_at=utcnow(),
    )
    db.add(test_run)
    db.commit()
    db.refresh(test_run)

    run_id, test_run_id, user_id = run.id, test_run.id, current_user.id
    logger.info("UAT tests queued: change=%s script=%s test_run=%s",
                change_id, script_path, test_run_id)

    async def _drive() -> None:
        bg = SessionLocal()
        try:
            bg_run = bg.get(PhaseBRun, run_id)
            bg_test = bg.get(UATTestRun, test_run_id)
            if bg_run is None or bg_test is None:      # deleted under us (reset)
                return
            with job_registry.tracked_step(
                bg,
                change_request_id=change_id,
                module="phase_b",
                subtype="uat_tests",
                user_id=user_id,
                initial_stage="Running the UAT test script",
                metadata={"script_path": script_path, "base_url": base_url},
            ):
                await run_uat_script(bg_run, bg, test_run=bg_test,
                                     script_path=script_path, base_url=base_url)
        except Exception:  # noqa: BLE001 — background task: record, never raise
            logger.exception("UAT background task failed: change=%s", change_id)
            try:
                bg.rollback()
                row = bg.get(UATTestRun, test_run_id)
                if row is not None and row.status == TestRunStatus.RUNNING:
                    row.status = TestRunStatus.COMPLETED
                    row.completed_at = utcnow()
                    row.failed = row.failed or 1
                    row.total = row.total or 1
                    row.log = ((row.log or "")
                               + "\n[backend] UAT task failed — see server logs").strip()
                    # Same rule as the happy path: a recorded failure flows
                    # FORWARD to triage; leaving the step at TEST_GEN would
                    # record a failed run that triage can never look at.
                    crashed_run = bg.get(PhaseBRun, run_id)
                    if crashed_run is not None and crashed_run.current_step in (
                            PhaseBStep.TEST_GEN, PhaseBStep.TEST_EXEC):
                        crashed_run.current_step = PhaseBStep.TRIAGE
                    bg.commit()
            except Exception:  # noqa: BLE001
                logger.exception("Could not mark uat_test_run %s failed", test_run_id)
        finally:
            bg.close()

    _spawn_bg(_drive())
    return {"current_step": run.current_step.value, "test_run": _serialise_test_run(test_run)}


@router.get("/changes/{change_id}/phase-b/test-cases")
def list_test_cases(change_id: str, db: DbDep, current_user: CurrentUser):
    """List active UAT cases of the latest suite version. Used to rehydrate
    the panel on reload — returns an empty list if test gen hasn't run."""
    run = _get_run_or_404(change_id, db)
    latest_v = (
        db.query(UATTestCase.suite_version)
        .filter(UATTestCase.phase_b_run_id == run.id)
        .order_by(UATTestCase.suite_version.desc())
        .first()
    )
    if not latest_v:
        return {"suite_version": 0, "total": 0, "test_cases": []}

    rows = (
        db.query(UATTestCase)
        .filter(
            UATTestCase.phase_b_run_id == run.id,
            UATTestCase.suite_version == latest_v[0],
            UATTestCase.is_active.is_(True),
        )
        .order_by(UATTestCase.test_id.asc())
        .all()
    )
    return {
        "suite_version": latest_v[0],
        "total":         len(rows),
        "test_cases":    [_serialise_test_case(r) for r in rows],
    }


@router.get("/changes/{change_id}/phase-b/test-runs/latest")
def get_latest_test_run(change_id: str, db: DbDep, current_user: CurrentUser):
    """Latest UAT test run for this change + its per-case results. Returns
    404 if no run exists yet."""
    run = _get_run_or_404(change_id, db)
    from app.models.phase_b import UATTestResult as _UATTestResult

    test_run = (
        db.query(UATTestRun)
        .filter(UATTestRun.phase_b_run_id == run.id)
        .order_by(UATTestRun.started_at.desc())
        .first()
    )
    if not test_run:
        raise HTTPException(status_code=404, detail="No UAT test run yet")

    results = (
        db.query(_UATTestResult)
        .filter(_UATTestResult.test_run_id == test_run.id)
        .all()
    )
    return {
        "test_run": _serialise_test_run(test_run),
        "results": [
            {
                "id":              r.id,
                "test_case_id":    r.test_case_id,
                "status":          r.status.value if r.status else None,
                "actual_status":   r.actual_status,
                "actual_response": r.actual_response,
                "latency_ms":      r.latency_ms,
                "error_message":   r.error_message,
                "executed_at":     r.executed_at.isoformat() if r.executed_at else None,
            }
            for r in results
        ],
    }


# ── REST — Git / MR ──────────────────────────────────────────────────────────

class GitPushRequest(BaseModel):
    """Push payload.

    Multi-repo (M-4): when omitted, push fans out across all repos
    registered in `phase_b_run_repos` for this run. Files are routed to
    each repo by their `repo_id` (set by the M-3 parser).

    Legacy: `repo_id` lets the caller force a single-repo push (rebinds
    the run's primary `gitlab_repo` field and skips the multi-repo fan-out
    by overriding the route table). Useful for hotfix flows.

    `branch` lets the operator (re)name the feature branch at push time —
    required when the run has no feature branch (the endpoint 422s rather
    than committing to a base branch).
    """
    repo_id: str | None = None
    branch: str | None = None


@router.post("/changes/{change_id}/phase-b/git/push")
async def trigger_git_push(change_id: str, body: GitPushRequest, db: DbDep, current_user: CurrentUser):
    """Push approved code to GitLab: create feature branch(es), commit, raise MR(s).

    Returns a per-repo list of MR results so the UI can render one row per
    repo. The legacy fields (`branch_name`, `commit_sha`, `mr_url`, `mr_iid`)
    are populated from the FIRST successful repo result for back-compat.
    """
    from app.models.code_repo import CodeRepo

    run = _get_run_or_404(change_id, db)
    # A git push (incl. a deferred "push later") is valid once code is APPROVED — at
    # the GIT step OR any later step (build/test/triage/completed). Only the pre-GIT
    # steps have nothing approved to push yet, so reject just those. (Previously this
    # hard-required current_step==GIT, so a push after build/deploy/test 400'd with
    # "Cannot push at step 'completed'".)
    if run.current_step in (PhaseBStep.CODE_CHANGE, PhaseBStep.CODE_REVIEW, PhaseBStep.IS_REVIEW):
        raise HTTPException(status_code=400,
                            detail=f"Cannot push yet — code isn't approved (step '{run.current_step.value}').")

    # Operator-supplied feature branch (the 422 below tells them when it's needed).
    # A rename invalidates prior push state — the old branch's push no longer counts.
    if body.branch and body.branch.strip():
        nb = body.branch.strip()
        for row in db.query(PhaseBRunRepo).filter(PhaseBRunRepo.run_id == run.id).all():
            if row.branch != nb:
                row.pushed_content_hash = None
                row.branch = nb
        run.gitlab_branch = nb
        db.flush()

    iteration, files = _get_latest_approved_files(run, db)
    if not iteration:
        raise HTTPException(status_code=400, detail="No approved code iteration found.")

    # Legacy single-repo override: if caller forces a specific repo, route
    # ALL files to that repo (overriding any `repo_id` set by the M-3 parser).
    # NB: only the REPO is rebound — the branch must stay the run's feature
    # branch (this used to reset it to the repo's BASE branch, which routed
    # the commit straight onto main).
    if body.repo_id:
        code_repo = db.get(CodeRepo, body.repo_id)
        if not code_repo:
            raise HTTPException(status_code=404, detail="Registered repo not found")
        run.gitlab_repo = code_repo.gitlab_repo
        db.flush()
        for f in files:
            f["repo_id"] = code_repo.id
        logger.info(
            "Git push: legacy single-repo override id=%s repo=%s branch=%s",
            body.repo_id, code_repo.gitlab_repo, run.gitlab_branch,
        )

    # Refuse to commit to a base branch — ask the operator for a feature branch
    # instead. (Runs without phase_b_run_repos rows keep the legacy auto-convert
    # in git_integrator: a non-feature name becomes feature/change-<id>.)
    repo_rows = (
        db.query(PhaseBRunRepo, CodeRepo)
        .join(CodeRepo, CodeRepo.id == PhaseBRunRepo.repo_id)
        .filter(PhaseBRunRepo.run_id == run.id)
        .order_by(CodeRepo.label)
        .all()
    )
    if repo_rows:
        push_branch = repo_rows[0][0].branch or ""
        base_branches = {cr.gitlab_branch or "main" for _, cr in repo_rows} | {"main", "master"}
        if not push_branch or push_branch in base_branches:
            raise HTTPException(
                status_code=422,
                detail=(f"No feature branch is set for this run (current: "
                        f"'{push_branch or '—'}' is a base branch). Pass 'branch' in the "
                        "request body with the feature branch to push — refusing to commit "
                        "directly to the base branch."))

        # Idempotent — bound to CONTENT (mirrors the agentic pushed_manifest_hash):
        # "an MR exists" is not "the current content is pushed". Fix rounds after an
        # early push stay pushable; an unchanged payload 409s as before. NULL hash =
        # legacy row (unknown) → treated as pushed, never surprise-re-pushed.
        primary_repo_id = repo_rows[0][1].id
        by_repo: dict[str, list[dict]] = {}
        for f in files:
            by_repo.setdefault(f.get("repo_id") or primary_repo_id, []).append(f)
        rows_with_files = [(p, cr) for p, cr in repo_rows if by_repo.get(cr.id)]

        def _row_unchanged(pbrr, cr) -> bool:
            if not pbrr.mr_url:
                return False
            if pbrr.pushed_content_hash is None:
                return True
            return pbrr.pushed_content_hash == content_hash(by_repo.get(cr.id) or [])

        if rows_with_files and all(_row_unchanged(p, cr) for p, cr in rows_with_files):
            raise HTTPException(
                status_code=409,
                detail=("Already pushed — the approved content is unchanged since the last "
                        "push. Approve a new iteration to push again."))
    else:
        # Legacy run without repo rows: keep the original blanket idempotency.
        already_pushed = db.query(GitEvent.id).filter(
            GitEvent.phase_b_run_id == run.id, GitEvent.mr_iid.isnot(None)).first()
        if already_pushed:
            raise HTTPException(status_code=409,
                                detail="Already pushed — an MR was already raised for this change.")

    # R-7 — track git push as a durable job
    with job_registry.tracked_step(
        db,
        change_request_id=change_id,
        module="phase_b",
        subtype="git_push",
        user_id=current_user.id,
        initial_stage=f"Pushing {len(files)} file(s) to GitLab",
        metadata={"files_count": len(files), "gitlab_repo": run.gitlab_repo, "gitlab_branch": run.gitlab_branch},
    ) as tracker_id:
        logger.info("Git push started: change=%s files=%d job=%s", change_id, len(files), tracker_id)
        push_result = await push_to_gitlab_multi(run, files, db)
        results = push_result["results"]
        branch = push_result["branch"]

        # Find the first repo with a successful MR for legacy display.
        first_success = next(
            (r for r in results if r.get("mr_iid") is not None),
            results[0] if results else None,
        )

        logger.info(
            "Git push done: change=%s branch=%s repos=%d summary=%s",
            change_id, branch, len(results), push_result["summary"],
        )

    response = {
        "branch_name": branch,
        "files_count": len(files),
        "repos":       results,           # multi-repo per-repo list
        "summary":     push_result["summary"],
        "job_id":      tracker_id,
    }
    if first_success:
        response.update({
            # Legacy single-MR fields — populated from the first successful repo.
            "commit_sha": first_success.get("commit_sha"),
            "mr_url":     first_success.get("mr_url"),
            "mr_iid":     first_success.get("mr_iid"),
            "status":     first_success.get("status"),
        })
    return response


@router.get("/changes/{change_id}/phase-b/git/repos")
def list_git_repos(change_id: str, db: DbDep, current_user: CurrentUser):
    """List registered repos available for git push."""
    from app.models.code_repo import CodeRepo
    from sqlalchemy import select
    repos = db.scalars(select(CodeRepo).order_by(CodeRepo.created_at)).all()
    return [
        {"id": r.id, "label": r.label, "gitlab_repo": r.gitlab_repo, "gitlab_branch": r.gitlab_branch,
         "role": getattr(r, "role", None) or "app"}
        for r in repos
    ]


@router.get("/changes/{change_id}/phase-b/git/latest")
def get_latest_git_event(change_id: str, db: DbDep, current_user: CurrentUser):
    """Get the latest git push state for this Phase B run.

    M-4: returns a per-repo `repos` array for multi-repo runs (read from
    `phase_b_run_repos`) plus the legacy single-event fields for back-compat.
    The legacy fields are populated from the most recent GitEvent row, which
    matches one of the repo entries in the multi-repo array.
    """
    from app.models.code_repo import CodeRepo

    run = _get_run_or_404(change_id, db)
    git_event = (
        db.query(GitEvent)
        .filter(GitEvent.phase_b_run_id == run.id)
        .order_by(GitEvent.created_at.desc())
        .first()
    )
    if not git_event:
        logger.warning("Git event not found: change=%s", change_id)
        raise HTTPException(status_code=404, detail="No git event found")

    # M-4 — multi-repo summary read from phase_b_run_repos (preferred).
    repo_rows = (
        db.query(PhaseBRunRepo, CodeRepo)
        .join(CodeRepo, CodeRepo.id == PhaseBRunRepo.repo_id)
        .filter(PhaseBRunRepo.run_id == run.id)
        .all()
    )
    repos = [
        {
            "repo_id":     pbrr.repo_id,
            "label":       cr.label,
            "gitlab_repo": cr.gitlab_repo,
            "branch":      pbrr.branch,
            "mr_url":      pbrr.mr_url,
            "mr_iid":      pbrr.mr_iid,
            "mr_state":    pbrr.mr_state,
        }
        for pbrr, cr in repo_rows
    ]

    return {
        "id":          git_event.id,
        "branch_name": git_event.branch_name,
        "commit_sha":  git_event.commit_sha,
        "mr_url":      git_event.mr_url,
        "mr_iid":      git_event.mr_iid,
        "status":      git_event.status.value,
        "created_at":  git_event.created_at.isoformat(),
        # M-4 — per-repo list. Empty for legacy runs without phase_b_run_repos.
        "repos":       repos,
    }


# ── REST — Build ─────────────────────────────────────────────────────────────

class BuildTriggerRequest(BaseModel):
    """Payload for the unified build+deploy trigger.

    Branches default to "master" — matching the host script's own default
    when the parameters are omitted. The UI surfaces both as editable text
    fields so PMs can target a feature branch when needed.

    `script_path` names WHICH build+deploy script runs — absolute or relative
    to PHASE_B_SCRIPT_ROOT, validated (symlinks resolved, containment, *.sh)
    before any subprocess exists. Only honoured in `local` runner mode and
    only for admin/tech_lead; omitted = the fixed PHASE_B_BUILD_SCRIPT.
    """
    core_branch: str | None = None
    app_branch: str | None = None
    script_path: str | None = None


# Background script tasks are held here so the event loop's weak reference is
# not the only thing keeping them alive — per the asyncio docs a bare
# create_task() result can be garbage-collected mid-run. Entries remove
# themselves on completion.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _stale_after_s() -> int:
    """How old a QUEUED/RUNNING row must be before it stops blocking a
    re-trigger. Derived from the script ceiling (+30 min grace for flush and
    bookkeeping) rather than a fixed constant, so raising
    PHASE_B_SCRIPT_TIMEOUT_SECONDS past the old 2h constant cannot let a
    still-live run age out of the guard and admit a concurrent one. Belt and
    suspenders: the startup sweep (services/phase_b_recovery.py) already fails
    rows orphaned by a restart — this window only covers a run whose process
    is still up but wedged past its kill ceiling."""
    return max(60, int(settings.phase_b_script_timeout_seconds)) + 30 * 60


def _build_run_to_dict(build_run: BuildRun) -> dict:
    """Shape the BuildRun row for the frontend (build + deploy + startup)."""
    return {
        "id":                  build_run.id,
        "status":              build_run.status.value,
        "build_log":           build_run.build_log,
        "deploy_log":          build_run.deploy_log,
        "startup_log":         build_run.startup_log,
        "artifact_path":       build_run.artifact_path,
        "artifact_name":       build_run.jenkins_job_name,  # legacy reuse
        "deployed_artifacts":  build_run.deployed_artifacts or [],
        "services_started":    build_run.services_started or [],
        "core_branch":         build_run.core_branch,
        "app_branch":         build_run.app_branch,
        "host":                build_run.host,
        "script_path":         build_run.script_path,
        "triggered_at":        build_run.triggered_at.isoformat(),
        "completed_at":        build_run.completed_at.isoformat() if build_run.completed_at else None,
    }


def _resolve_script_or_400(raw: str, current_user, *, feature: str) -> str:
    """Validate a request-supplied script path (allowlist root, symlink-safe
    containment, *.sh) and gate it to admin/tech_lead. 400/403 with a safe
    message on rejection — the raw path is logged server-side only."""
    from app.models.user import UserRole
    from app.services.script_paths import ScriptPathError, resolve_operator_script

    if current_user.role not in (UserRole.ADMIN, UserRole.TECH_LEAD):
        raise HTTPException(status_code=403,
                            detail=f"choosing the {feature} script requires admin or tech_lead")
    mode = (settings.phase_b_runner_mode or "ssh").strip().lower()
    if mode != "local":
        raise HTTPException(
            status_code=400,
            detail=f"script_path is only honoured in PHASE_B_RUNNER_MODE=local "
                   f"(current mode: {mode}) — the script must run where it can be validated")
    try:
        resolved = resolve_operator_script(raw)
    except ScriptPathError as e:
        logger.warning("%s script rejected for user %s: %s (raw=%r)",
                       feature, current_user.id, e, raw[:200])
        raise HTTPException(status_code=400, detail=f"script_path rejected: {e}")
    return str(resolved)


def _latest_build_run(db, run_id: str) -> BuildRun | None:
    return (db.query(BuildRun)
            .filter(BuildRun.phase_b_run_id == run_id)
            .order_by(BuildRun.triggered_at.desc())
            .first())


@router.post("/changes/{change_id}/phase-b/build/trigger")
async def trigger_build(
    change_id: str,
    db: DbDep,
    current_user: CurrentUser,
    body: BuildTriggerRequest | None = None,
):
    """Start the build + deploy and return the QUEUED BuildRun immediately.

    The run continues in the background; the UI polls GET /build/latest, whose
    row the runner updates with the streaming script log every ~2s (ssh/local
    modes), so the operator watches the script's output live. Behaviour still
    depends on PHASE_B_RUNNER_MODE (ssh/local = host script — `script_path`
    may name which one; build = local clone+mvn; demo = canned simulated run).
    See app.services.build_runner."""
    from app.services.build_runner import run_build_and_deploy

    run = _get_run_or_404(change_id, db)
    if run.current_step != PhaseBStep.BUILD:
        raise HTTPException(status_code=400, detail=f"Cannot build at step '{run.current_step}'")
    # Defense in depth: current_step=BUILD can be reached by writers that predate
    # the governance gate (git_integrator's post-push auto-advance) — the trigger
    # is the last line, so the gate holds regardless of how the step was set.
    _assert_governance_passed(db, change_id)

    body = body or BuildTriggerRequest()
    script_path = None
    if body.script_path and body.script_path.strip():
        script_path = _resolve_script_or_400(body.script_path, current_user,
                                             feature="build+deploy")

    # One build at a time per run: a QUEUED/RUNNING row younger than the stale
    # ceiling blocks a re-trigger (double-click, second tab). Older ones are
    # restart orphans and stop counting.
    latest = _latest_build_run(db, run.id)
    if latest and latest.status in (BuildRunStatus.QUEUED, BuildRunStatus.RUNNING) \
            and latest.triggered_at and \
            (utcnow() - latest.triggered_at).total_seconds() < _stale_after_s():
        raise HTTPException(status_code=409,
                            detail="a build is already running for this change — "
                                   "wait for it to finish (its log is streaming on the Build panel)")

    core_branch = (body.core_branch or "master").strip() or "master"
    app_branch = (body.app_branch or "master").strip() or "master"
    mode = (settings.phase_b_runner_mode or "ssh").strip().lower()

    # Pre-create the row the UI will poll; the runner adopts it (sets the
    # mode's host label, flips it RUNNING, streams the log into it).
    build_run = BuildRun(
        id=generate_uuid(),
        phase_b_run_id=run.id,
        iteration_number=run.iteration_count,
        status=BuildRunStatus.QUEUED,
        triggered_at=utcnow(),
        core_branch=core_branch,
        app_branch=app_branch,
        host=mode,
        script_path=script_path,
    )
    db.add(build_run)
    db.commit()
    db.refresh(build_run)

    run_id, build_run_id, user_id = run.id, build_run.id, current_user.id
    logger.info("Build+deploy queued: change=%s mode=%s core=%s upi2=%s script=%s build_run=%s",
                change_id, mode, core_branch, app_branch,
                script_path or "(configured default)", build_run_id)

    async def _drive() -> None:
        # The request's session dies with the response — the background task
        # owns a fresh one and re-fetches both rows inside it.
        bg = SessionLocal()
        try:
            bg_run = bg.get(PhaseBRun, run_id)
            bg_build = bg.get(BuildRun, build_run_id)
            if bg_run is None or bg_build is None:      # deleted under us (reset)
                return
            with job_registry.tracked_step(
                bg,
                change_request_id=change_id,
                module="phase_b",
                subtype="build",
                user_id=user_id,
                initial_stage="Running the build + deploy script",
                metadata={"core_branch": core_branch, "app_branch": app_branch,
                          "script_path": script_path},
            ):
                await run_build_and_deploy(
                    bg_run, bg,
                    core_branch=core_branch,
                    app_branch=app_branch,
                    script_path=script_path,
                    build_run=bg_build,
                )
        except Exception:  # noqa: BLE001 — background task: record, never raise
            logger.exception("Build+deploy background task failed: change=%s", change_id)
            try:
                bg.rollback()
                row = bg.get(BuildRun, build_run_id)
                if row is not None and row.status in (BuildRunStatus.QUEUED, BuildRunStatus.RUNNING):
                    row.status = BuildRunStatus.FAILURE
                    row.completed_at = utcnow()
                    row.build_log = ((row.build_log or "")
                                     + "\n[backend] build task failed — see server logs").strip()
                    bg.commit()
            except Exception:  # noqa: BLE001
                logger.exception("Could not mark build_run %s failed", build_run_id)
        finally:
            bg.close()

    _spawn_bg(_drive())
    return _build_run_to_dict(build_run)


@router.get("/changes/{change_id}/phase-b/build/latest")
def get_latest_build(change_id: str, db: DbDep, current_user: CurrentUser):
    """Get the latest build+deploy run for this Phase B run."""
    run = _get_run_or_404(change_id, db)
    build_run = (
        db.query(BuildRun)
        .filter(BuildRun.phase_b_run_id == run.id)
        .order_by(BuildRun.triggered_at.desc())
        .first()
    )
    if not build_run:
        raise HTTPException(status_code=404, detail="No build found")

    return _build_run_to_dict(build_run)


# ── REST — Triage (AI over the build + UAT logs, plus the walkthrough) ────────

def _serialise_triage(row: PhaseBTriageReport) -> dict:
    return {
        "id":              row.id,
        "build_run_id":    row.build_run_id,
        "uat_test_run_id": row.uat_test_run_id,
        "report":          row.report,
        "walkthrough":     row.walkthrough,
        "created_at":      row.created_at.isoformat() if row.created_at else None,
    }


async def _walkthrough_for_change(db: Session, change_id: str,
                                  run: PhaseBRun | None = None) -> dict | None:
    """The dev + tester walkthrough shown on the Triage panel.

    Prefer the approved agentic run's STORED walkthrough; else generate one
    from that run's diff (persisting it back onto the run, like the agentic
    endpoint does); else, for the legacy pipeline, ground it in the approved
    iteration's generated files. Best-effort — None when there is nothing to
    ground a walkthrough in (the panel says so instead of inventing one)."""
    from app.agents.change_walkthrough import generate_walkthrough
    from app.services.build_runner import _approved_agentic_run

    cr = db.get(ChangeRequest, change_id)
    intent = (cr.initial_prompt if cr else "") or (cr.title if cr else "") or ""
    title = (cr.title if cr else None) or "Change walkthrough"

    approved = _approved_agentic_run(db, change_id)
    if approved is not None:
        stored = (approved.handoff_json or {}).get("walkthrough")
        if stored:
            return stored
        try:
            from app.api.agentic import _walkthrough_diff
            diff_text = _walkthrough_diff(db, approved)
        except Exception:  # noqa: BLE001 — workspace GC'd / unreadable ⇒ no diff
            diff_text = ""
        if diff_text.strip():
            wt = await generate_walkthrough(intent=intent, diff_text=diff_text, title=title)
            hj = dict(approved.handoff_json or {})
            hj["walkthrough"] = wt
            approved.handoff_json = hj
            db.commit()
            return wt
        return None

    # Legacy pipeline: no agentic run — ground in the approved iteration's files.
    if run is None:
        return None
    iteration, files = _get_latest_approved_files(run, db)
    if not iteration or not files:
        return None
    pseudo_diff = "\n\n".join(
        f"=== {f.get('path')} (full file as changed) ===\n{(f.get('content') or '')[:20000]}"
        for f in files[:30] if f.get("path"))
    if not pseudo_diff.strip():
        return None
    return await generate_walkthrough(intent=intent, diff_text=pseudo_diff, title=title)


@router.post("/changes/{change_id}/phase-b/triage/run")
async def run_phase_b_triage(change_id: str, db: DbDep, current_user: CurrentUser):
    """AI triage of the change's pipeline evidence, at the TRIAGE step.

    Reads the latest build+deploy log and UAT script log, classifies every
    visible failure (code bug vs test-case issue vs environment issue) with
    quoted evidence, and pairs the result with the plain-language developer +
    tester walkthrough of the change. Persists a PhaseBTriageReport; the panel
    rehydrates from GET /triage/latest. Completing the step stays a human
    action (advance-step)."""
    from app.agents.uat_triage import triage_from_logs

    run = _get_run_or_404(change_id, db)
    # Available AT the triage step and after (re-runs on a completed pipeline
    # are legitimate — e.g. after re-executing UAT).
    if run.current_step not in (PhaseBStep.TRIAGE, PhaseBStep.COMPLETED) \
            and run.status != PhaseBRunStatus.COMPLETED:
        raise HTTPException(status_code=400,
                            detail=f"Cannot run triage at step '{run.current_step}'")

    build = _latest_build_run(db, run.id)
    test = (db.query(UATTestRun)
            .filter(UATTestRun.phase_b_run_id == run.id)
            .order_by(UATTestRun.started_at.desc())
            .first())
    if build is None and test is None:
        raise HTTPException(status_code=400,
                            detail="nothing to triage — run Build and the UAT tests first")

    counts = {
        "total":   test.total if test else None,
        "passed":  test.passed if test else None,
        "failed":  test.failed if test else None,
        "skipped": test.skipped if test else None,
    }
    build_failed = bool(build and build.status == BuildRunStatus.FAILURE)
    # ssh/local builds store build_log = unified_log(), which already embeds
    # the deploy/startup sections verbatim — joining all three would feed the
    # model the same evidence twice. Include a section only when the unified
    # log does not already contain it (demo/build modes store disjoint logs).
    build_log_parts: list[str] = []
    for part in ((build.build_log if build else None),
                 (build.deploy_log if build else None),
                 (build.startup_log if build else None)):
        if part and not any(part in prev for prev in build_log_parts):
            build_log_parts.append(part)

    cr = db.get(ChangeRequest, change_id)
    with job_registry.tracked_step(
        db,
        change_request_id=change_id,
        module="phase_b",
        subtype="triage",
        user_id=current_user.id,
        initial_stage="AI-triaging build + UAT logs",
    ):
        report = await triage_from_logs(
            change_title=(cr.title if cr else "") or "",
            build_log="\n\n".join(build_log_parts) or None,
            build_failed=build_failed,
            test_log=(test.log if test else None),
            counts=counts,
        )
        walkthrough = await _walkthrough_for_change(db, change_id, run)

    row = PhaseBTriageReport(
        phase_b_run_id=run.id,
        build_run_id=build.id if build else None,
        uat_test_run_id=test.id if test else None,
        report=report,
        walkthrough=walkthrough,
        created_by=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("Phase B triage complete: change=%s overall=%s findings=%d walkthrough=%s",
                change_id, report.get("overall"), len(report.get("findings") or []),
                bool(walkthrough))
    return _serialise_triage(row)


@router.get("/changes/{change_id}/phase-b/triage/latest")
def get_latest_triage(change_id: str, db: DbDep, current_user: CurrentUser):
    """Latest triage report for this change's Phase B run (404 until one runs)."""
    run = _get_run_or_404(change_id, db)
    row = (db.query(PhaseBTriageReport)
           .filter(PhaseBTriageReport.phase_b_run_id == run.id)
           .order_by(PhaseBTriageReport.created_at.desc())
           .first())
    if row is None:
        raise HTTPException(status_code=404, detail="No triage report yet")
    return _serialise_triage(row)


# ── WebSocket — Code Change streaming ─────────────────────────────────────────

@router.websocket("/ws/changes/{change_id}/phase-b/code")
async def ws_code_change(change_id: str, websocket: WebSocket):
    """
    WebSocket for streaming code generation.

    Protocol (matches existing useAgentWS hook):
      C→S: {"token": "<jwt>"}                          — auth (first message)
      S→C: {"type": "history", "messages": [...]}      — prior iterations as conversation turns
      C→S: {"message": "start"}                        — trigger initial generation
      C→S: {"message": "<feedback text>"}              — request a revision
      S→C: {"type": "chunk", "text": "..."}            — streaming token
      S→C: {"type": "done", "full": "...",
             "iteration": N, "files": [...]}           — turn complete
      S→C: {"type": "error", "detail": "..."}          — error
    """
    await websocket.accept()
    logger.info("WS code-change connected: change=%s", change_id)
    db: Session = SessionLocal()

    try:
        # ── Auth ──────────────────────────────────────────────────────────────
        auth_msg = await websocket.receive_text()
        auth_data = json.loads(auth_msg)
        user = authenticate_ws(websocket, db, auth_data.get("token", ""))
        if not user:
            logger.warning("WS code-change auth failed: change=%s", change_id)
            await websocket.send_text(json.dumps({"type": "error", "detail": "Unauthorized"}))
            await websocket.close()
            return
        logger.info("WS code-change auth ok: change=%s user=%s", change_id, user.username)

        # ── Validate change + run ─────────────────────────────────────────────
        cr = db.query(ChangeRequest).filter(ChangeRequest.id == change_id).first()
        if not cr:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Change not found"}))
            return

        run = db.query(PhaseBRun).filter(PhaseBRun.change_request_id == change_id).order_by(PhaseBRun.started_at.desc()).first()
        if not run:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Phase B not started. Call POST /phase-b/start first."}))
            return

        # ── Load prior iterations as conversation history ──────────────────────
        iterations = (
            db.query(CodeIteration)
            .filter(CodeIteration.phase_b_run_id == run.id)
            .order_by(CodeIteration.iteration_number)
            .all()
        )
        history = _iteration_history_to_messages(iterations)

        # Send history to client so it can re-render prior turns
        history_for_client = []
        for it in iterations:
            if it.generated_output:
                history_for_client.append({"role": "assistant", "content": it.generated_output, "iteration": it.iteration_number})
            if it.user_feedback and it.trigger != IterationTrigger.INITIAL:
                history_for_client.append({"role": "user", "content": it.user_feedback})

        await websocket.send_text(json.dumps({"type": "history", "messages": history_for_client}))
        logger.info("WS code-change history sent: change=%s messages=%d", change_id, len(history_for_client))

        # R-7 — surface any active code-change job for this change
        active = job_registry.get_active_jobs(
            db, change_request_id=change_id, module="phase_b",
        )
        active_cc = [j for j in active if j.get("subtype") == "code_change"]
        if active_cc:
            await websocket.send_text(json.dumps({"type": "active_jobs", "jobs": active_cc}))

        # Load Tech Spec and BRD
        tech_spec = _load_tech_spec(change_id, db)
        brd       = _load_brd(change_id, db)

        # ── Main message loop ──────────────────────────────────────────────────
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            # R-7 — replay protocol
            if data.get("type") == "replay_request":
                rep_job_id = (data.get("job_id") or "").strip()
                rep_since  = int(data.get("since_seq") or 0)
                if not rep_job_id:
                    continue
                chunks = job_registry.get_chunks_since(rep_job_id, since_seq=rep_since)
                await websocket.send_text(json.dumps({
                    "type": "replay", "job_id": rep_job_id, "since_seq": rep_since,
                    "chunks": [{"seq": s, "text": t} for (s, t) in chunks],
                    "count": len(chunks),
                }))
                continue

            user_message = (data.get("message") or "").strip()
            if not user_message:
                continue

            # Determine trigger type — detect loop-back from review
            # Refresh run state from DB to catch loop-backs done via REST
            db.refresh(run)

            if not iterations:
                trigger = IterationTrigger.INITIAL
            else:
                # Check if there are recent review findings to inject
                latest_cr = (
                    db.query(CodeReviewResult)
                    .join(CodeIteration)
                    .filter(CodeIteration.phase_b_run_id == run.id)
                    .order_by(CodeReviewResult.created_at.desc())
                    .first()
                )
                latest_isr = (
                    db.query(ISReviewResult)
                    .join(CodeIteration)
                    .filter(CodeIteration.phase_b_run_id == run.id)
                    .order_by(ISReviewResult.created_at.desc())
                    .first()
                )

                if latest_isr and latest_isr.status == ReviewStatus.ISSUES_FOUND and latest_isr.findings:
                    trigger = IterationTrigger.IS_REVIEW_FEEDBACK
                elif latest_cr and latest_cr.status == ReviewStatus.ISSUES_FOUND and latest_cr.issues:
                    trigger = IterationTrigger.CODE_REVIEW_FEEDBACK
                else:
                    trigger = IterationTrigger.USER_FEEDBACK

            next_iteration_number = (run.iteration_count or 0) + 1
            logger.info("WS code-change user message: change=%s trigger=%s len=%d", change_id, trigger, len(user_message))

            # R-7 — durable job for this code-change turn
            registry_job_id = job_registry.create_job(
                db,
                change_request_id=change_id,
                module="phase_b",
                subtype="code_change",
                started_by_user_id=user.id,
                metadata={
                    "iteration_number": next_iteration_number,
                    "trigger":          str(trigger),
                },
            )
            await websocket.send_text(json.dumps({
                "type": "job_id", "job_id": registry_job_id,
                "module": "phase_b", "subtype": "code_change",
            }))
            job_registry.update_job(
                db, registry_job_id,
                current_stage=f"Generating iteration #{next_iteration_number}",
            )

            # Stream the response
            full_text = ""
            try:
                async for chunk in stream_code_change_turn(
                    db=db,
                    change_request_id=change_id,
                    tech_spec=tech_spec,
                    brd=brd,
                    conversation_history=history,
                    new_user_message=user_message,
                    phase_b_run_id=run.id,
                ):
                    full_text += chunk
                    await job_registry.ws_send_chunk(websocket, registry_job_id, chunk)
            except Exception as exc:
                logger.exception("Code Change Agent streaming error: %s", exc)
                job_registry.fail_job(db, registry_job_id, error=str(exc))
                await websocket.send_text(json.dumps({
                    "type": "error", "detail": "An internal error occurred", "job_id": registry_job_id,
                }))
                continue

            # Parse generated files from the output, with multi-repo
            # context so each file dict carries `repo_id` for downstream
            # routing at git-push time (M-4).
            path_to_repo, repo_label_to_id, primary_repo_id = build_parser_context(
                db, phase_b_run_id=run.id,
            )
            files = parse_files_from_output(
                full_text,
                path_to_repo=path_to_repo,
                repo_label_to_id=repo_label_to_id,
                primary_repo_id=primary_repo_id,
            )

            # Persist the iteration
            iteration = CodeIteration(
                id=generate_uuid(),
                phase_b_run_id=run.id,
                iteration_number=next_iteration_number,
                generated_output=full_text,
                files_changed=files,
                user_feedback=user_message if trigger != IterationTrigger.INITIAL else None,
                trigger=trigger,
                approved=False,
                created_at=utcnow(),
            )
            db.add(iteration)
            run.iteration_count = next_iteration_number
            db.commit()

            logger.info("WS code-change generation done: change=%s iteration=%d files=%d response_len=%d", change_id, next_iteration_number, len(files), len(full_text))

            job_registry.complete_job(
                db, registry_job_id,
                result={
                    "iteration_id":     iteration.id,
                    "iteration_number": next_iteration_number,
                    "files_count":      len(files),
                    "markdown_chars":   len(full_text),
                },
                final_stage=f"Iteration #{next_iteration_number} ready ({len(files)} files)",
            )

            # Update history for subsequent turns in this session
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": full_text})
            iterations = iterations + [iteration]  # extend local list

            await websocket.send_text(json.dumps({
                "type":      "done",
                "full":      full_text,
                "iteration": next_iteration_number,
                "files":     files,
                "job_id":    registry_job_id,
            }))

    except WebSocketDisconnect:
        logger.info("WS code change disconnected: change=%s", change_id)
    except Exception as exc:
        logger.exception("WS code change unhandled error: %s", exc)
    finally:
        db.close()
