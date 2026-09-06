# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Sub-slice 19a-scheduler — Celery periodic incremental re-ingest.

Runs every `settings.scheduler_ingest_interval_minutes` minutes (when
`USE_SCHEDULED_INGEST=True`):
  1. Iterate every registered `CodeRepo` row.
  2. For each repo, call `ingest_polyglot_repo_incremental` with the
     configured language set.
  3. (Optionally) call `ingest_from_db` to refresh the AGE knowledge
     graph projection so cross-file CALLS / DESCRIBES edges stay current.

Each repo is independent — a per-repo failure is logged and skipped, the
remaining repos still process. The Celery beat scheduler config lives
in `celery_tasks.py`; this module just defines the task body and a pure
orchestrator that's testable without Celery.

Design decisions:
  - Pure orchestrator `run_scheduled_ingest_once(db, repos, *, languages,
    do_kg_projection, ingest_fn, kg_ingest_fn)` is fully DI: callers
    inject the ingest + KG-projection callables, so tests can verify
    the iteration / per-repo failure / flag-gating logic without
    hitting GitLab or the AGE graph.
  - The Celery task wrapper just opens a SessionLocal, queries CodeRepo
    rows, and delegates to the orchestrator with the real callables.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Report shape
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScheduledIngestReport:
    """Per-tick summary returned by the orchestrator + Celery task."""
    repos_processed:    int = 0
    repos_succeeded:    int = 0
    repos_failed:       int = 0
    files_added:        int = 0
    files_modified:     int = 0
    files_deleted:      int = 0
    chunks_inserted:    int = 0
    chunks_deleted:     int = 0
    kg_projection_ran:  bool = False
    kg_successes:       int = 0
    kg_failures:        int = 0
    failures:           list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repos_processed":   self.repos_processed,
            "repos_succeeded":   self.repos_succeeded,
            "repos_failed":      self.repos_failed,
            "files_added":       self.files_added,
            "files_modified":    self.files_modified,
            "files_deleted":     self.files_deleted,
            "chunks_inserted":   self.chunks_inserted,
            "chunks_deleted":    self.chunks_deleted,
            "kg_projection_ran": self.kg_projection_ran,
            "kg_successes":      self.kg_successes,
            "kg_failures":       self.kg_failures,
            "failures":          self.failures,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Pure orchestrator (DI-friendly)
# ──────────────────────────────────────────────────────────────────────────────

def run_scheduled_ingest_once(
    db: Session,
    repos: list,
    *,
    languages: list[str],
    do_kg_projection: bool,
    ingest_fn: Callable[..., dict],
    kg_ingest_fn: Callable[[Session], Any] | None = None,
) -> ScheduledIngestReport:
    """Execute one scheduler tick. Pure-ish — DB is the only side effect.

    Args:
        db: SQLAlchemy session.
        repos: list of CodeRepo rows (or duck-typed objects with
               id / gitlab_repo / gitlab_branch / gitlab_url attrs).
        languages: which languages to feed each `ingest_fn` call.
        do_kg_projection: when True, run `kg_ingest_fn` after all repos
                          finish (regardless of per-repo failures —
                          stale graph is better than no graph).
        ingest_fn: injectable; signature
                   `(db, repo_id, repo, branch, *, languages, gitlab_url)`
                   → dict matching `ingest_polyglot_repo_incremental`'s
                   return shape. Real prod callers pass that function.
        kg_ingest_fn: injectable; signature `(db) → ingest_report`. None
                      disables KG projection regardless of `do_kg_projection`.

    Returns:
        ScheduledIngestReport with per-bucket counts + per-repo failure list.
    """
    report = ScheduledIngestReport()

    for repo in repos:
        report.repos_processed += 1
        try:
            result = ingest_fn(
                db=db,
                repo_id=repo.id,
                repo=repo.gitlab_repo,
                branch=repo.gitlab_branch,
                languages=languages,
                gitlab_url=repo.gitlab_url,
            )
        except Exception as e:
            report.repos_failed += 1
            report.failures.append({
                "repo_id":     getattr(repo, "id", "?"),
                "gitlab_repo": getattr(repo, "gitlab_repo", "?"),
                "stage":       "incremental_ingest",
                "error":       str(e),
            })
            logger.warning(
                "scheduled ingest: repo %s failed: %s",
                getattr(repo, "gitlab_repo", "?"), e,
            )
            continue

        report.repos_succeeded += 1
        report.files_added     += int(result.get("files_added", 0) or 0)
        report.files_modified  += int(result.get("files_modified", 0) or 0)
        report.files_deleted   += int(result.get("files_deleted", 0) or 0)
        report.chunks_inserted += int(result.get("chunks_inserted", 0) or 0)
        report.chunks_deleted  += int(result.get("chunks_deleted", 0) or 0)

    # KG projection — run once after all per-repo work, even when some
    # repos failed (the unaffected ones may still have new chunks worth
    # projecting). Skipped when caller disabled or kg_ingest_fn is None.
    if do_kg_projection and kg_ingest_fn is not None:
        report.kg_projection_ran = True
        try:
            kg_report = kg_ingest_fn(db)
            # `ingest_from_db` returns IngestReport with total_successes/failures.
            successes = getattr(kg_report, "total_successes", lambda: 0)()
            failures  = getattr(kg_report, "total_failures",  lambda: 0)()
            report.kg_successes = successes
            report.kg_failures  = failures
        except Exception as e:
            report.kg_failures += 1
            report.failures.append({
                "stage": "kg_projection",
                "error": str(e),
            })
            logger.warning("scheduled ingest: KG projection failed: %s", e)

    logger.info(
        "scheduled ingest tick: repos=%d/%d files=+%d/~%d/-%d chunks=+%d/-%d kg=%s",
        report.repos_succeeded, report.repos_processed,
        report.files_added, report.files_modified, report.files_deleted,
        report.chunks_inserted, report.chunks_deleted,
        "yes" if report.kg_projection_ran else "no",
    )
    return report


# ──────────────────────────────────────────────────────────────────────────────
# Celery task wrapper (real prod entry — registered against beat_schedule)
# ──────────────────────────────────────────────────────────────────────────────

def _list_active_repos(db: Session) -> list:
    from app.models.code_repo import CodeRepo
    return list(db.query(CodeRepo).all())


def _real_incremental_ingest(**kwargs):
    """Lazy-imported wrapper so this module stays importable when the
    ingest dependency tree (gitlab, embeddings) hasn't been initialised."""
    from app.rag.code_ingestion import ingest_polyglot_repo_incremental
    return ingest_polyglot_repo_incremental(**kwargs)


def _real_kg_ingest(db: Session):
    from app.kg.ingest_from_rag import ingest_from_db
    return ingest_from_db(db)


# Late-import the Celery app so a missing celery dep doesn't break unit
# tests that only need the orchestrator.
def _register_with_celery():
    from app.services.celery_tasks import celery_app

    @celery_app.task(name="scheduler.scheduled_polyglot_reingest")
    def scheduled_polyglot_reingest():
        """Periodic task — one tick. Skipped when flag off."""
        if not settings.use_scheduled_ingest:
            logger.debug("scheduled ingest skipped (flag off)")
            return {"skipped": True, "reason": "use_scheduled_ingest=False"}

        from app.core.database import SessionLocal
        db = SessionLocal()
        try:
            repos = _list_active_repos(db)
            if not repos:
                logger.info("scheduled ingest: no repos registered, skipping")
                return {"skipped": True, "reason": "no_repos"}

            report = run_scheduled_ingest_once(
                db, repos,
                languages=list(settings.scheduler_ingest_languages),
                do_kg_projection=settings.scheduler_kg_projection,
                ingest_fn=_real_incremental_ingest,
                kg_ingest_fn=_real_kg_ingest,
            )
            return report.to_dict()
        finally:
            db.close()

    return scheduled_polyglot_reingest


# Register when the module is imported. Wrapped so test imports of this
# module don't fail when Celery isn't installed in some test env.
try:
    scheduled_polyglot_reingest = _register_with_celery()
except Exception as e:  # pragma: no cover
    logger.warning("could not register scheduled_polyglot_reingest: %s", e)
    scheduled_polyglot_reingest = None  # type: ignore[assignment]
