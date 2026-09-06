# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin Code Indexing API — manage GitLab repos and index Java source code into pgvector."""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select, func, text

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.deps import DbDep, AdminUser, CurrentUser
from app.models.code_repo import CodeRepo
from app.models.document_chunk import DocumentChunk, DocCategory, CODE_SOURCE_CATEGORIES
from app.models.module_context import ModuleContext
from app.models.base import generate_uuid, utcnow
from app.rag.code_ingestion import (
    LANGUAGE_EXTENSIONS,
    ingest_polyglot_repo,
    ingest_polyglot_repo_incremental,
    ingest_repo,
)
# R-6 — durable agent-job registry for REST-triggered async indexing.
from app.services import job_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/code-indexing", tags=["code-indexing"])


# ── R-6 — background runner for code indexing ───────────────────────────────
#
# The indexing call is multi-hour for large repos. Instead of blocking the
# HTTP request, we register a job in `agent_jobs`, kick the ingest off as a
# BackgroundTask, and return `{job_id}` immediately. The frontend polls
# `GET /api/jobs/{job_id}` every 5 s to surface progress and the result on
# the existing CodeIndexing admin page.
#
# Progress at the boundaries is reported by `_run_index_job`. For mid-ingest
# fine-grained progress (per-file, per-stage), instrument `ingest_repo` with
# an `on_progress` callback — that's a follow-up sub-slice; R-6 v1 reports:
#   - "Cloning repo + ingesting" at start
#   - "Persisting chunks" mid-way (after the synchronous ingest returns,
#     before the repo-row stats are committed)
#   - terminal stage "Indexed N files / M chunks" on success
#
# Each background runner opens its OWN db session — the request-scoped one
# from DbDep is closed by FastAPI as soon as the endpoint returns.


def _generate_module_context_parallel(
    repo_id: str, gitlab_repo: str, gitlab_branch: str,
    gitlab_url: str | None, result_holder: dict,
) -> None:
    """Build module-wise context (`module_context` rows) CONCURRENTLY with the
    RAG ingest. Module context needs a checked-out tree (the ingest fetches via
    the GitLab API and never clones), so this does its own shallow clone into a
    temp dir, on its own DB session, then cleans up.

    Fully fail-soft and gated: flag off / clone error / parse error → 0 rows and
    the ingest job is never affected. Runs in a daemon thread started by
    `_run_index_job` so both halves progress at the same time."""
    from app.core.config import settings
    if not settings.use_module_context_generation:
        return
    import tempfile, shutil, subprocess
    from app.agents.workspace_local import build_clone_url, validate_git_ref
    from app.agents import module_context_generator

    tmp = tempfile.mkdtemp(prefix="modctx_")
    dest = f"{tmp}/repo"
    mc_db = SessionLocal()
    try:
        # Validate before the value reaches an option slot — a branch starting
        # with '-' is parsed by git as a flag, and --upload-pack=<cmd> on a
        # clone runs that command locally. Fail soft: module context is an
        # optional enrichment and must never break the ingest.
        try:
            safe_branch = validate_git_ref(gitlab_branch)
        except ValueError as exc:
            logger.warning("[modctx %s] skipped: %s", repo_id, exc)
            return
        url = build_clone_url(gitlab_url or settings.gitlab_url, gitlab_repo, settings.gitlab_token)
        proc = subprocess.run(
            # `--` ends option parsing so url/dest can never be read as flags.
            ["git", "clone", "--depth", "1", "--branch", safe_branch, "--", url, dest],
            capture_output=True, text=True, timeout=settings.agentic_command_timeout_s,
        )
        if proc.returncode != 0:
            import re
            safe_stderr = re.sub(r"://[^@]+@", "://***@", proc.stderr or "")
            logger.warning("[modctx %s] shallow clone failed (exit %s): %s", repo_id, proc.returncode, safe_stderr)
            return
        base = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or None
        n = module_context_generator.maybe_generate_module_context(mc_db, repo_id, dest, base)
        mc_db.commit()
        result_holder["modules"] = n
        logger.info("[modctx %s] wrote %d module_context rows", repo_id, n)
        # Flow map (reuse-first §) — reads the module entry points just written.
        from app.agents import flow_context_generator
        fc = flow_context_generator.maybe_generate_flow_context(mc_db, repo_id, base)
        mc_db.commit()
        result_holder["flows"] = fc
        logger.info("[flowctx %s] wrote %d flow_context row(s)", repo_id, fc)
    except Exception as exc:  # noqa: BLE001 — never break the ingest
        logger.warning("[modctx %s] skipped: %s", repo_id, exc)
    finally:
        mc_db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _run_index_job(
    *,
    job_id: str,
    repo_id: str,
    mode: str,           # 'java' | 'polyglot' | 'polyglot-incremental'
    languages: list[str] | None = None,
) -> None:
    """Execute the ingest synchronously inside a fresh DB session, updating
    the agent_jobs row at boundaries. Runs in a FastAPI BackgroundTask, so
    the HTTP response has already been sent by the time this starts.

    Failures are caught + recorded on the job row; nothing propagates.
    """
    worker_db = SessionLocal()
    try:
        repo = worker_db.get(CodeRepo, repo_id)
        if not repo:
            job_registry.fail_job(
                worker_db, job_id,
                error=f"Repo {repo_id} not found at job-start time",
                final_stage="Repo not found",
            )
            return

        job_registry.update_job(
            worker_db, job_id,
            current_stage=f"Cloning {repo.gitlab_repo} ({repo.gitlab_branch})",
        )
        logger.info(
            "[index-job %s] starting mode=%s repo=%s branch=%s langs=%s",
            job_id, mode, repo.gitlab_repo, repo.gitlab_branch, languages,
        )

        # Kick off module-wise context generation IN PARALLEL with the ingest
        # below — both fetch the repo independently and run at the same time.
        import threading
        mc_result: dict = {}
        mc_thread = threading.Thread(
            target=_generate_module_context_parallel,
            args=(repo_id, repo.gitlab_repo, repo.gitlab_branch, repo.gitlab_url, mc_result),
            daemon=True,
        )
        mc_thread.start()

        if mode == "java":
            result = ingest_repo(
                db=worker_db, repo_id=repo_id,
                repo=repo.gitlab_repo, branch=repo.gitlab_branch,
                gitlab_url=repo.gitlab_url,
            )
            files_fetched = result["files_fetched"]
            chunks_stored = result["chunks_stored"]
            extra: dict = {}
        elif mode == "polyglot":
            result = ingest_polyglot_repo(
                db=worker_db, repo_id=repo_id,
                repo=repo.gitlab_repo, branch=repo.gitlab_branch,
                languages=languages or [],
                gitlab_url=repo.gitlab_url,
            )
            files_fetched = result["files_fetched"]
            chunks_stored = result["chunks_stored"]
            extra = {
                "by_language":       result.get("by_language"),
                "files_by_language": result.get("files_by_language"),
            }
        elif mode == "polyglot-incremental":
            result = ingest_polyglot_repo_incremental(
                db=worker_db, repo_id=repo_id,
                repo=repo.gitlab_repo, branch=repo.gitlab_branch,
                languages=languages or [],
                gitlab_url=repo.gitlab_url,
            )
            files_fetched = result["files_fetched"]
            # Incremental returns inserts/deletes — the post-state chunks count
            # is repo.chunks_count + inserted - deleted (approximated below).
            chunks_stored = result.get("chunks_inserted", 0)
            extra = {
                "files_unchanged": result.get("files_unchanged"),
                "files_modified":  result.get("files_modified"),
                "files_added":     result.get("files_added"),
                "files_deleted":   result.get("files_deleted"),
                "chunks_inserted": result.get("chunks_inserted"),
                "chunks_deleted":  result.get("chunks_deleted"),
                "by_language":     result.get("by_language"),
            }
        else:
            job_registry.fail_job(
                worker_db, job_id,
                error=f"Unknown index mode {mode!r}",
                final_stage="Internal error",
            )
            return

        # Let the parallel module-context build finish (it ran during the ingest)
        # so the result reflects both halves; bounded so a hung clone can't wedge
        # the job.
        mc_thread.join(timeout=settings.agentic_command_timeout_s + 30)
        extra["module_contexts"] = mc_result.get("modules", 0)

        job_registry.update_job(
            worker_db, job_id,
            current_stage=f"Persisting stats — {files_fetched} files, {chunks_stored} chunks",
        )

        # Update repo aggregate stats — same logic as the original
        # synchronous endpoints did.
        repo.last_indexed_at = utcnow()
        repo.files_count = files_fetched
        if mode == "polyglot-incremental":
            repo.chunks_count = (repo.chunks_count or 0) + (extra.get("chunks_inserted") or 0) - (extra.get("chunks_deleted") or 0)
        else:
            repo.chunks_count = chunks_stored
        worker_db.commit()

        job_registry.complete_job(
            worker_db, job_id,
            result={
                "repo_id":       repo_id,
                "mode":          mode,
                "files_fetched": files_fetched,
                "chunks_stored": chunks_stored,
                **extra,
            },
            final_stage=f"Indexed {files_fetched} files, {chunks_stored} chunks",
        )
        logger.info(
            "[index-job %s] complete files=%d chunks=%d",
            job_id, files_fetched, chunks_stored,
        )
    except Exception as exc:
        logger.exception("[index-job %s] failed", job_id)
        try:
            job_registry.fail_job(
                worker_db, job_id,
                error=str(exc), final_stage="Indexing failed",
            )
        except Exception:
            pass
    finally:
        worker_db.close()


# ── Schemas ───────────────────────────────────────────────────────────────────

class AddRepoRequest(BaseModel):
    label: str
    gitlab_repo: str
    gitlab_branch: str = "main"
    gitlab_url: str | None = None

    @field_validator("gitlab_branch")
    @classmethod
    def _check_branch(cls, v: str) -> str:
        """Reject refs that git would read as options.

        The stored value later lands in a `git clone --branch <ref>` argv, where a
        ref beginning with '-' is parsed as a flag (`--upload-pack=<cmd>` runs a
        local command). Validating at the write boundary stops the bad value from
        being persisted at all, so the second-order read path is clean too. Normal
        refs — `main`, `release/2.0`, `feature/NET-123` — are unaffected.
        """
        from app.agents.workspace_local import validate_git_ref
        try:
            return validate_git_ref(v, field="gitlab_branch")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    # Build-order role for multi-repo changes (network-core → network-2.0): "core" repos
    # are built (mvn install) FIRST so their artifacts are in the shared ~/.m2
    # before dependent "app" repos compile. Default "app".
    role: str | None = None


class UpdateRepoRequest(BaseModel):
    role: str | None = None


# ── Repo CRUD ─────────────────────────────────────────────────────────────────

def _normalize_repo_path(value: str) -> str:
    """Accept either a project PATH (``group/repo``) or a full clone URL
    (``https://gitlab.com/group/repo.git``) and return the path the GitLab API +
    clone expect (``group/repo``). Operators paste either; the API needs the path,
    so normalise once at the boundary rather than 404-ing later."""
    v = (value or "").strip()
    if "://" in v:                                  # full URL → strip scheme + host
        from urllib.parse import urlparse
        v = urlparse(v).path
    v = v.strip("/")
    if v.endswith(".git"):
        v = v[:-4]
    return v


@router.get("/repos")
def list_repos(db: DbDep, _: AdminUser):
    """List all registered code repos.

    R-6 — each row also carries `active_job` (or null) so the UI can show
    a per-row "Indexing in progress…" banner without an extra round-trip.
    Pulls from JobRegistry filtered to module='code_indexing' AND
    subtype=<repo_id>.
    """
    repos = db.scalars(select(CodeRepo).order_by(CodeRepo.created_at.desc())).all()

    # Bulk-fetch active code-indexing jobs once, then index by subtype (repo_id).
    active_jobs = job_registry.get_active_jobs(db, module="code_indexing")
    active_by_repo: dict[str, dict] = {}
    for j in active_jobs:
        rid = j.get("subtype")
        if rid and rid not in active_by_repo:
            active_by_repo[rid] = j   # take the most-recent (get_active_jobs orders DESC)

    return [
        {
            "id": r.id,
            "label": r.label,
            "gitlab_url": r.gitlab_url,
            "gitlab_repo": r.gitlab_repo,
            "gitlab_branch": r.gitlab_branch,
            "role": r.role,
            "last_indexed_at": r.last_indexed_at.isoformat() if r.last_indexed_at else None,
            "files_count": r.files_count,
            "chunks_count": r.chunks_count,
            "active_job": active_by_repo.get(r.id),     # null when idle
        }
        for r in repos
    ]


@router.get("/repos/{repo_id}/context")
def get_repo_context(repo_id: str, db: DbDep, _: AdminUser):
    """Everything the indexer generated for a repo, for the Code Indexing UI:
    the module-wise context tree (`module_context` rows) plus a summary of the
    indexed code chunks/symbols grouped by file."""
    repo = db.get(CodeRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    mods = db.scalars(
        select(ModuleContext)
        .where(ModuleContext.repo_id == repo_id)
        .order_by(ModuleContext.depth, ModuleContext.module_path)
    ).all()
    modules = [{
        "module_path": m.module_path or ".",
        "parent_module_path": m.parent_module_path,
        "depth": m.depth,
        "java_version": m.java_version,
        "depends_on": m.depends_on or [],
        "summary": m.summary,
        "key_types": m.key_types or [],
        "entry_points": m.entry_points or [],
        "functional_flow": m.functional_flow,
        "generated_at": m.generated_at.isoformat() if m.generated_at else None,
    } for m in mods]

    # Code chunks/symbols indexed for this repo (tagged via metadata.repo_id).
    chunks = db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.metadata_["repo_id"].as_string() == repo_id)
        .where(DocumentChunk.doc_category.in_(CODE_SOURCE_CATEGORIES))
    ).all()
    by_language: dict[str, int] = {}
    files: dict[str, dict] = {}
    for c in chunks:
        lang = c.language or "unknown"
        by_language[lang] = by_language.get(lang, 0) + 1
        f = files.setdefault(c.source_file, {"language": lang, "chunks": 0, "symbols": []})
        f["chunks"] += 1
        if c.symbol_name:
            f["symbols"].append({"kind": c.symbol_kind, "name": c.symbol_name})

    # The reuse-first flow map (one row per repo): which API carries the transaction leg.
    from app.models.flow_context import FlowContext
    fc = db.scalars(select(FlowContext).where(FlowContext.repo_id == repo_id)).first()
    flow = None
    if fc:
        flow = {
            "summary": fc.summary,
            "transaction_apis": fc.transaction_apis or [],
            "meta_apis": fc.meta_apis or [],
            "flows": fc.flows or [],
            "generated_at": fc.generated_at.isoformat() if fc.generated_at else None,
        }

    return {
        "repo_id": repo_id,
        "label": repo.label,
        "modules": modules,
        "flow": flow,
        "chunks": {
            "total": len(chunks),
            "by_language": by_language,
            "files": [{"path": p, **v} for p, v in sorted(files.items())],
        },
    }


@router.post("/repos")
def add_repo(body: AddRepoRequest, db: DbDep, _: AdminUser):
    """Register a new GitLab repo for code indexing."""
    if body.role and body.role not in ("core", "app", "legacy"):
        raise HTTPException(status_code=422, detail="role must be core, app or legacy")
    repo = CodeRepo(
        id=generate_uuid(),
        label=body.label,
        gitlab_url=body.gitlab_url,
        gitlab_repo=_normalize_repo_path(body.gitlab_repo),   # path, even if a URL was pasted
        gitlab_branch=body.gitlab_branch,
        role=body.role,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    logger.info("Code repo added: id=%s label='%s' repo=%s branch=%s role=%s",
                repo.id, repo.label, repo.gitlab_repo, repo.gitlab_branch, repo.role)
    return {
        "id": repo.id,
        "label": repo.label,
        "gitlab_repo": repo.gitlab_repo,
        "gitlab_branch": repo.gitlab_branch,
        "role": repo.role,
    }


@router.patch("/repos/{repo_id}")
def update_repo(repo_id: str, body: UpdateRepoRequest, db: DbDep, _: AdminUser):
    """Update repo settings — currently the build-order role (core builds first)."""
    repo = db.get(CodeRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")
    if body.role is not None:
        if body.role not in ("core", "app", "legacy"):
            raise HTTPException(status_code=422, detail="role must be core, app or legacy")
        repo.role = body.role
    # Self-heal a previously-stored full URL → path (so an already-added repo indexes).
    norm = _normalize_repo_path(repo.gitlab_repo)
    if norm != repo.gitlab_repo:
        repo.gitlab_repo = norm
    db.commit()
    return {"id": repo.id, "role": repo.role, "gitlab_repo": repo.gitlab_repo}


# Child tables keyed by `repo_id` that must be cleared before a repo row can go.
# The seven FK-constrained ones (agentic_run_repos, phase_b_run_repos,
# module_context, repo_path_context, flow_context, xsd_schema_nodes,
# xsd_java_links) declare NO `ON DELETE` action, so deleting the repo while any
# child row survives raises an IntegrityError. The last two (code_repo_state,
# code_repo_file_state) are soft links with no FK — cleared so no index/dedup
# state is orphaned. Order is leaf-first (xsd_schema_edges cascades from
# xsd_schema_nodes; xsd_java_links → nodes is SET NULL, so it must go first).
_REPO_CHILD_TABLES = (
    "xsd_java_links",
    "xsd_schema_nodes",
    "flow_context",
    "repo_path_context",
    "module_context",
    "agentic_run_repos",
    "phase_b_run_repos",
    "code_repo_file_state",
    "code_repo_state",
)


@router.delete("/repos/{repo_id}")
def remove_repo(repo_id: str, db: DbDep, _: AdminUser):
    """Remove a repo, its indexed code chunks, and every derived index / run-link
    row that references it. Atomic — all in one transaction."""
    repo = db.get(CodeRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    # Delete this repo's code chunks (any language, not just Java).
    chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.doc_category.in_(CODE_SOURCE_CATEGORIES))
        .filter(DocumentChunk.metadata_["repo_id"].as_string() == repo_id)
        .all()
    )
    for c in chunks:
        db.delete(c)

    # Clear child rows before the repo so the FK constraints don't block it.
    children_deleted: dict[str, int] = {}
    for tbl in _REPO_CHILD_TABLES:
        res = db.execute(text(f"DELETE FROM {tbl} WHERE repo_id = :rid"), {"rid": repo_id})
        if res.rowcount:
            children_deleted[tbl] = res.rowcount

    db.delete(repo)
    db.commit()
    logger.info("Code repo removed: id=%s label='%s' chunks_deleted=%d children=%s",
                repo_id, repo.label, len(chunks), children_deleted)
    return {"deleted": True, "chunks_deleted": len(chunks), "children_deleted": children_deleted}


# ── Indexing ──────────────────────────────────────────────────────────────────

@router.post("/repos/{repo_id}/index", status_code=202)
def index_repo(
    repo_id: str,
    background_tasks: BackgroundTasks,
    db: DbDep,
    user: AdminUser,
):
    """Trigger code indexing for a specific repo.

    R-6 — runs as a background task so the HTTP request returns immediately.
    Returns `{job_id, status}` for polling via `GET /api/jobs/{job_id}`.
    The frontend's JobsContext picks up the job_id and shows progress on
    the Code Indexing admin page + the sidebar tray.
    """
    repo = db.get(CodeRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    job_id = job_registry.create_job(
        db,
        change_request_id=None,            # admin-only — visibility=X (original user + admins)
        module="code_indexing",
        subtype=repo_id,
        started_by_user_id=user.id,
        metadata={
            "repo_id":       repo_id,
            "repo_label":    repo.label,
            "gitlab_repo":   repo.gitlab_repo,
            "gitlab_branch": repo.gitlab_branch,
            "mode":          "java",
        },
    )
    logger.info(
        "Code indexing scheduled: repo_id=%s repo=%s branch=%s job_id=%s",
        repo_id, repo.gitlab_repo, repo.gitlab_branch, job_id,
    )

    background_tasks.add_task(_run_index_job,
        job_id=job_id, repo_id=repo_id, mode="java",
    )
    return {
        "job_id":     job_id,
        "status":     "running",
        "module":     "code_indexing",
        "subtype":    repo_id,
        "repo_id":    repo_id,
    }


# ── Polyglot indexing (Slice 22c) ─────────────────────────────────────────────

class PolyglotIndexRequest(BaseModel):
    languages: list[str]


@router.post("/repos/{repo_id}/index-polyglot", status_code=202)
def index_repo_polyglot(
    repo_id: str,
    body: PolyglotIndexRequest,
    background_tasks: BackgroundTasks,
    db: DbDep,
    user: AdminUser,
):
    """Trigger code indexing for a repo across multiple languages.

    Body: `{"languages": ["java", "python"]}` — only languages with a known
    extension mapping in `LANGUAGE_EXTENSIONS` are honoured. Unknown names
    are silently dropped (validated below — empty after drop → 400).

    R-6 — runs as a background task; returns `{job_id}` for polling.
    """
    repo = db.get(CodeRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    known_languages = {lang.lower() for lang in LANGUAGE_EXTENSIONS.values()}
    requested = [l.lower() for l in body.languages or []]
    accepted = [l for l in requested if l in known_languages]
    if not accepted:
        raise HTTPException(
            status_code=400,
            detail=(
                f"no recognised languages in {body.languages}. "
                f"Known: {sorted(known_languages)}"
            ),
        )

    job_id = job_registry.create_job(
        db,
        change_request_id=None,
        module="code_indexing",
        subtype=repo_id,
        started_by_user_id=user.id,
        metadata={
            "repo_id":       repo_id,
            "repo_label":    repo.label,
            "gitlab_repo":   repo.gitlab_repo,
            "gitlab_branch": repo.gitlab_branch,
            "languages":     accepted,
            "mode":          "polyglot",
        },
    )
    logger.info(
        "Polyglot code indexing scheduled: repo_id=%s repo=%s languages=%s job_id=%s",
        repo_id, repo.gitlab_repo, accepted, job_id,
    )

    background_tasks.add_task(_run_index_job,
        job_id=job_id, repo_id=repo_id, mode="polyglot", languages=accepted,
    )
    return {
        "job_id":     job_id,
        "status":     "running",
        "module":     "code_indexing",
        "subtype":    repo_id,
        "repo_id":    repo_id,
        "languages":  accepted,
    }


# ── Incremental polyglot indexing (Slice 26) ──────────────────────────────────

@router.post("/repos/{repo_id}/index-polyglot-incremental", status_code=202)
def index_repo_polyglot_incremental(
    repo_id: str,
    body: PolyglotIndexRequest,
    background_tasks: BackgroundTasks,
    db: DbDep,
    user: AdminUser,
):
    """Trigger incremental indexing — re-processes only files whose
    content changed since the last ingest. Same body shape as
    `/index-polyglot`. Initial run on a fresh repo is equivalent to a
    full ingest (every file is "added"). Subsequent runs only process
    the delta.

    R-6 — runs as a background task; returns `{job_id}` for polling.
    """
    repo = db.get(CodeRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    known_languages = {lang.lower() for lang in LANGUAGE_EXTENSIONS.values()}
    requested = [l.lower() for l in body.languages or []]
    accepted = [l for l in requested if l in known_languages]
    if not accepted:
        raise HTTPException(
            status_code=400,
            detail=f"no recognised languages in {body.languages}. "
                   f"Known: {sorted(known_languages)}",
        )

    job_id = job_registry.create_job(
        db,
        change_request_id=None,
        module="code_indexing",
        subtype=repo_id,
        started_by_user_id=user.id,
        metadata={
            "repo_id":       repo_id,
            "repo_label":    repo.label,
            "gitlab_repo":   repo.gitlab_repo,
            "gitlab_branch": repo.gitlab_branch,
            "languages":     accepted,
            "mode":          "polyglot-incremental",
        },
    )
    logger.info(
        "Incremental polyglot indexing scheduled: repo_id=%s repo=%s languages=%s job_id=%s",
        repo_id, repo.gitlab_repo, accepted, job_id,
    )

    background_tasks.add_task(_run_index_job,
        job_id=job_id, repo_id=repo_id, mode="polyglot-incremental",
        languages=accepted,
    )
    return {
        "job_id":     job_id,
        "status":     "running",
        "module":     "code_indexing",
        "subtype":    repo_id,
        "repo_id":    repo_id,
        "languages":  accepted,
    }


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def indexing_status(db: DbDep, _: AdminUser):
    """Get overall code indexing statistics."""
    total_chunks = db.scalar(
        select(func.count(DocumentChunk.id))
        .where(DocumentChunk.doc_category == DocCategory.JAVA_SOURCE)
    ) or 0

    total_files = db.scalar(
        select(func.count(func.distinct(DocumentChunk.source_file)))
        .where(DocumentChunk.doc_category == DocCategory.JAVA_SOURCE)
    ) or 0

    repos = db.scalars(select(CodeRepo).order_by(CodeRepo.created_at)).all()

    return {
        "total_chunks": total_chunks,
        "total_files": total_files,
        "repos_count": len(repos),
        "repos": [
            {
                "id": r.id,
                "label": r.label,
                "gitlab_repo": r.gitlab_repo,
                "files_count": r.files_count,
                "chunks_count": r.chunks_count,
                "last_indexed_at": r.last_indexed_at.isoformat() if r.last_indexed_at else None,
            }
            for r in repos
        ],
    }
