# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import logging
import os

from celery import Celery
from app.core.config import settings
from app.core import log_buffer, diag

logger = logging.getLogger(__name__)

# The code-gen pipeline runs HERE (the worker), not in the FastAPI process.
# Without this, all of its rich logging (LLM turns, tool calls, phase timing)
# only reached the worker's stdout and never any file. Install the same file
# logging the API process uses so codegen.log / commands.log / build logs and
# app.jsonl all get written from the worker too. Both calls are fail-open +
# idempotent.
log_buffer.install(level=logging.DEBUG)
diag.install()

# Silent-acceptance sweep cadence. Default 15 min; lower it (e.g. 60s) in dev
# via NEGOTIATION_SWEEP_SECONDS so a shortened round window (minutes) actually
# gets swept promptly instead of waiting up to 15 min.
_SWEEP_SECONDS = float(os.environ.get("NEGOTIATION_SWEEP_SECONDS", str(15 * 60)))

celery_app = Celery(
    "npci_cm",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    # Don't let Celery rip our handlers off the root logger — otherwise the
    # log_buffer (app.jsonl) handler installed above is removed on worker boot
    # and the worker's general logs vanish from the file again.
    worker_hijack_root_logger=False,
    # ── A15 (architecture review Critical #15, "No Celery Worker Concurrency
    # Caps") ──────────────────────────────────────────────────────────────
    # worker_concurrency: bounds concurrent tasks per worker process so DB
    # connection checkouts (pool_size + max_overflow, see A13/core/database.py)
    # cannot be exceeded by concurrent long-running agentic tasks.
    worker_concurrency=settings.celery_worker_concurrency,
    # task_acks_late: a task is only ack'd (removed from the broker queue)
    # AFTER it completes, not when the worker picks it up — so a worker crash
    # mid-agentic-run redelivers the task instead of silently losing it. Safe
    # here because the agentic pipeline's own lease/recovery mechanism
    # (agentic.recover) already handles idempotent resume of a redelivered
    # drive; a naive redelivery-without-resume-awareness task would need to be
    # idempotent for this to be safe, which the codegen loop already is by
    # design (crash-safe resumption via `.lease` files, see architecture.md).
    task_acks_late=settings.celery_task_acks_late,
    # worker_prefetch_multiplier=1: with `task_acks_late`, a worker otherwise
    # prefetches `concurrency * prefetch_multiplier` tasks ahead of finishing
    # its current ones — with long agentic tasks that means one slow worker
    # can hoard several tasks from the queue while sitting idle on all but
    # one, starving other worker processes of work. 1 = fair dispatch (a
    # worker only prefetches the NEXT task once it has capacity).
    worker_prefetch_multiplier=settings.celery_worker_prefetch_multiplier,
    # Route long-running agentic tasks to a dedicated queue so they cannot
    # starve short periodic sweeps (retry, orphan-job, workspace GC) that
    # share the default `celery` queue. Deploying a SEPARATE worker process
    # bound to each queue (`celery worker -Q agentic` / `-Q celery`) is what
    # makes this isolation effective in production — see
    # docs/ARCHITECTURE_REVIEW_REMEDIATION.md §A15 for the compose changes.
    task_routes={
        "agentic.drive":         {"queue": settings.celery_agentic_queue},
        "agentic.reverify":      {"queue": settings.celery_agentic_queue},
        "agentic.push":          {"queue": settings.celery_agentic_queue},
        "agentic.recover":       {"queue": settings.celery_agentic_queue},
    },
    task_default_queue=settings.celery_default_queue,
)


# Apply DB-owned config (app_configs) onto `settings` in EACH worker process.
# The codegen pipeline runs here, not in the FastAPI process, so without this the
# worker would only see .env — stale after any Admin → Configuration change, and
# broken once operator config is removed from .env. Fires per forked worker
# (fresh DB session per process). Fail-open (load_db_overrides swallows errors).
from celery.signals import worker_process_init


@worker_process_init.connect
def _apply_db_config_overrides(**_kwargs):
    from app.core.app_config_sync import load_db_overrides
    applied = load_db_overrides()
    logger.info("Celery worker: applied %d DB config override(s)", len(applied))


# Sub-slice 19a-scheduler — periodic incremental re-ingest. Beat schedule
# only fires when `USE_SCHEDULED_INGEST=True` (the task body itself
# checks the flag and short-circuits). Interval is operator-configurable
# via `SCHEDULER_INGEST_INTERVAL_MINUTES` env var; default 30 minutes.
# Registering the schedule unconditionally is safe — Celery beat is a
# separate process and only runs when started by docker-compose; the
# task body's flag check makes "schedule registered, flag off" a no-op.
celery_app.conf.beat_schedule = {
    "scheduled-polyglot-reingest": {
        "task":     "scheduler.scheduled_polyglot_reingest",
        "schedule": float(settings.scheduler_ingest_interval_minutes) * 60.0,
    },
    # R-9 — orphan-job sweeper: every 30 minutes, mark `running` jobs that
    # haven't had an `updated_at` bump in the idle threshold (default 90 min,
    # see sweep_orphan_jobs_task) as failed. Catches cases where a backend
    # worker crashed mid-ingest, a BackgroundTask was killed by a redeploy,
    # or the WS handler died without emitting the `done`/`error` chunks.
    # Idle threshold raised from 30→90min after code_indexing jobs on
    # CPU-only/low-RAM boxes were observed sitting silent for 40+ min mid
    # LLM-summarization batch (no per-file checkpoint) before the sweeper
    # killed an otherwise-healthy job. Idempotent — safe to run more
    # frequently if operators want tighter recovery.
    "agent-jobs-orphan-sweeper": {
        "task":     "jobs.sweep_orphan_jobs",
        "schedule": 30.0 * 60.0,                  # 30 min
    },
    # Outbound A2A delivery retry: every 2 minutes, re-send messages to partner banks whose
    # delivery failed and whose backoff has elapsed. Without this, one transient network
    # blip meant the bank never received its Product Kit and nobody was told. Backoff
    # schedule and attempt cap live in app/services/a2a_client.py.
    "a2a-delivery-retry": {
        "task":     "a2a.retry_failed_deliveries",
        "schedule": 2.0 * 60.0,                   # 2 min
    },
    # ITA-7 — the suite-join deadline sweep: finalize cert runs whose partner-
    # reported cases never arrived inside `cert_suite_deadline_s`. This is
    # ALSO the restart story: everything is re-derived from cert_runs rows, so
    # a process that died mid-wait needs no recovery beyond this tick.
    "cert-suite-join-sweep": {
        "task":     "cert.sweep_suite_joins",
        "schedule": 60.0,                         # 1 min — deadlines are minutes-scale
    },
    # Slice 9 of A2A security hardening — daily key-age scan. Logs a
    # WARN for each partner whose api_key/jwt_signing_secret/signing_secret
    # was last rotated more than `partner_secret_max_age_days` ago.
    # Operators triage the warnings and call the rotate-* endpoints
    # during a maintenance window. Doesn't auto-rotate — that would
    # break in-flight partner connections without an OOB heads-up.
    "a2a-partner-secret-age-scan": {
        "task":     "a2a.scan_partner_secret_ages",
        "schedule": 24.0 * 60.0 * 60.0,           # daily
    },
    # Partner negotiation — silent-acceptance sweep: every 15 minutes,
    # mark OPEN negotiation rounds past their deadline as silently_accepted
    # and auto-accept the partner's open counter-proposals (no response in
    # the round window = acceptance). Idempotent — only touches overdue
    # open rounds, so running it more often just tightens the deadline
    # granularity (the round window itself is 24h by default).
    "negotiation-silent-acceptance-sweep": {
        "task":     "negotiation.sweep_silent_acceptances",
        "schedule": _SWEEP_SECONDS,               # 15 min default; NEGOTIATION_SWEEP_SECONDS overrides
    },
    # Agentic codegen (THE BOOK §3/§14) — recovery sweep. Reclaims agentic
    # runs whose worker lease expired (crash / redeploy mid-phase) so the
    # orchestrator can resume them from their persisted phase. Cadence is
    # operator-configurable; the task body no-ops when nothing is stale.
    "agentic-recover": {
        "task":     "agentic.recover",
        "schedule": float(settings.agentic_recover_interval_seconds),
    },
    # Agentic codegen (§6) — workspace GC. Clones are 200 MB-2 GB, so GC is
    # mandatory. Removes only terminal + past-TTL + lease-free workspaces;
    # never touches an active or leased one. Hourly is plenty.
    "agentic-gc-workspaces": {
        "task":     "agentic.gc_workspaces",
        "schedule": 60.0 * 60.0,                  # hourly
    },
    # Finding #8 — data tiering sweep. No-ops when artifact_tiering_enabled
    # is False (the default), so registering this unconditionally is safe —
    # exactly the same "schedule registered, flag off" pattern already used
    # for scheduled-polyglot-reingest. Daily is plenty since eligibility is
    # gated on a 30-day minimum age; running more often just tightens when
    # a row crosses that threshold gets picked up, not what gets compressed.
    "artifact-tiering-sweep": {
        "task":     "artifact.tiering_sweep",
        "schedule": 24.0 * 60.0 * 60.0,           # daily
    },
}

# Importing the scheduled-ingest module registers the task with celery_app.
# Wrapped because some test environments may import celery_tasks without
# the full ingestion dependency tree.
try:
    from app.services import scheduled_ingest  # noqa: F401
except Exception:
    pass


# ── ITA-7: the suite-join deadline sweep ───────────────────────────────────
@celery_app.task(bind=True, name="cert.sweep_suite_joins")
def sweep_suite_joins_task(self):
    """Finalize cert runs past their suite deadline (or fully reported but
    missed by the report hook). Harness-agnostic — delegates entirely to
    `services/cert_join.py`, which derives everything from persisted rows."""
    import asyncio

    from app.services.cert_join import sweep_expired

    try:
        counts = asyncio.run(sweep_expired())
        if counts.get("finalized"):
            logger.info("cert.sweep_suite_joins: %s", counts)
        return counts
    except Exception:  # noqa: BLE001 — a sweep crash must not kill the beat
        logger.exception("cert.sweep_suite_joins failed")
        return {"error": True}


# ── A2A outbound delivery retry sweeper ────────────────────────────────────
@celery_app.task(bind=True, name="a2a.retry_failed_deliveries")
def retry_failed_deliveries_task(self, limit: int = 50):
    """Re-send outbound A2A messages whose delivery failed and whose backoff has elapsed.

    Before this existed, an outbound send was one-shot: a transient blip meant the partner
    bank never received the message, nothing retried, and the only trace was a log line.
    Rows are scheduled by `_record_attempt` in app/services/a2a_client.py (exponential
    backoff, capped at MAX_DELIVERY_ATTEMPTS, non-retryable 4xx excluded).

    Picks up rows where `next_retry_at <= now`, oldest first. Each resend re-attempts the
    SAME row, so attempts accumulate and the audit trail stays one record per send.
    """
    import asyncio
    from datetime import timedelta
    from app.core.database import SessionLocal
    from app.models.base import utcnow
    from app.models.phase_c import A2ADirection, A2AMessage
    from app.services.a2a_client import resend_message

    # Short in-flight lease. A network resend takes seconds and the batch runs sequentially, so
    # while a row is being sent we push its next_retry_at out — an OVERLAPPING sweep (or the manual
    # /resend endpoint) then can't pick the SAME row and double-deliver. resend_message overwrites
    # next_retry_at with the real outcome (delivered→None, failed→backoff) when it finishes, so the
    # lease only governs the in-flight window; a row whose sweep dies mid-batch just becomes eligible
    # again after it elapses.
    _CLAIM_LEASE = timedelta(minutes=10)

    db = SessionLocal()
    try:
        # Claim the batch atomically. On Postgres, SELECT … FOR UPDATE SKIP LOCKED means two
        # concurrent sweeps never grab the same rows; we then stamp the lease and COMMIT to
        # release the row locks BEFORE the slow sends. A bare FOR UPDATE would be defeated the
        # moment resend_message commits per message and drops every lock — hence the lease.
        # (SQLite has no row locking, so the lock is skipped there; single-threaded tests don't race.)
        # ITA-5: tunnelled exchanges are excluded — a replayed tunnelled POST
        # is a duplicate business call on the far side. Belt to the braces in
        # `_record_attempt`, which never schedules them; this filter also
        # covers any row scheduled before the exclusion existed.
        from app.a2a_common.integration_contract import TUNNEL_TASK_TYPES

        q = (db.query(A2AMessage)
               .filter(A2AMessage.direction == A2ADirection.OUTBOUND,
                       A2AMessage.next_retry_at.isnot(None),
                       A2AMessage.next_retry_at <= utcnow(),
                       A2AMessage.task_type.notin_(list(TUNNEL_TASK_TYPES)))
               .order_by(A2AMessage.next_retry_at.asc())
               .limit(limit))
        if db.bind.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)
        due = q.all()
        if not due:
            return {"retried": 0, "delivered": 0}

        ids = [m.id for m in due]
        lease_until = utcnow() + _CLAIM_LEASE
        for m in due:
            m.next_retry_at = lease_until
        db.commit()   # persist the lease + release the FOR UPDATE locks

        delivered = 0
        for mid in ids:
            try:
                msg = db.get(A2AMessage, mid)
                if msg is None:
                    continue
                out = asyncio.run(resend_message(db, msg))
                if out.status == "delivered":
                    delivered += 1
            except Exception:
                logger.exception("A2A retry failed for message %s", mid)
                # Don't leave the session in a failed-transaction state — it would poison
                # every remaining message in this batch with InFailedSqlTransaction.
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
        logger.info("A2A delivery retry sweep: attempted=%d delivered=%d", len(ids), delivered)
        return {"retried": len(ids), "delivered": delivered}
    except Exception as e:
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        self.update_state(state="FAILURE", meta={"error": str(e)})
        raise
    finally:
        db.close()


# ── R-9 — orphan-job sweeper task ──────────────────────────────────────────
@celery_app.task(bind=True, name="jobs.sweep_orphan_jobs")
def sweep_orphan_jobs_task(self, max_idle_minutes: int = 90):
    """Periodic sweep of stuck `running` jobs. Marks any agent_jobs row
    in PENDING/RUNNING with updated_at older than `max_idle_minutes` as
    failed with `error_message='Orphaned — no updates for >N minutes'`.

    Called by Celery beat every 30 min (see beat_schedule above). The
    interval inside the task can be tuned per-call by the scheduler if
    operators want a different cadence. Default idle threshold is 90 min
    (see beat_schedule comment for why).
    """
    from app.core.database import SessionLocal
    from app.services.job_registry import sweep_orphan_jobs

    db = SessionLocal()
    try:
        n = sweep_orphan_jobs(db, max_idle_minutes=max_idle_minutes)
        if n:
            self.update_state(state="SUCCESS", meta={"swept": n, "max_idle_minutes": max_idle_minutes})
        return {"swept": n, "max_idle_minutes": max_idle_minutes}
    except Exception as e:
        self.update_state(state="FAILURE", meta={"error": str(e)})
        raise
    finally:
        db.close()


# ── Partner negotiation — silent-acceptance sweep ──────────────────────────
@celery_app.task(bind=True, name="negotiation.sweep_silent_acceptances")
def sweep_silent_acceptances_task(self):
    """Periodic sweep of overdue negotiation rounds. For every OPEN round
    whose deadline has passed, mark it `silently_accepted` and auto-accept
    the partner's open counter-proposals — "no partner response within the
    round window = acceptance." Mirrors the on-demand
    /negotiation/rounds/sweep endpoint, applied platform-wide.

    Called by Celery beat every 15 min (see beat_schedule above).
    Idempotent: only acts on rounds already past their deadline.
    """
    import asyncio

    from app.core.database import SessionLocal
    from app.services.negotiation_extended import apply_silent_acceptances, send_round_events

    db = SessionLocal()
    try:
        affected, events = apply_silent_acceptances(db)
        db.commit()
        # Fan round_closed(silent_acceptance) per state that flipped. The
        # celery task is sync — use asyncio.run to drive the async sends the
        # same way the retry sweeper does (see resend_message above).
        if events:
            try:
                asyncio.run(send_round_events(events, db))
            except Exception:
                logger.exception("round_closed(silent_acceptance) send failed for sweep")

        # After any rounds close, the changes whose rounds are now all closed
        # need a v(N+1) revision plan drafted. That draft does an LLM call per
        # change, so DON'T run it inline here — the sweep must stay fast (it can
        # fire as often as every 60s in dev). Just find the ready changes and
        # dispatch one prepare task each; each runs in its own worker job.
        dispatched = []
        try:
            from app.services.kit_revision_runner import find_changes_ready_for_revision
            for cid in find_changes_ready_for_revision(db):
                prepare_revision_plan_task.delay(cid)
                dispatched.append(cid)
        except Exception:
            logger.exception("revision-plan preparation scan failed")

        if affected or dispatched:
            self.update_state(state="SUCCESS", meta={"silently_accepted": affected, "revision_dispatched": dispatched})
        return {"silently_accepted": affected, "revision_dispatched": dispatched}
    except Exception as e:
        db.rollback()
        self.update_state(state="FAILURE", meta={"error": str(e)})
        raise
    finally:
        db.close()


# ── Partner negotiation — per-change revision-plan prepare ─────────────────
@celery_app.task(bind=True, name="negotiation.prepare_revision_plan")
def prepare_revision_plan_task(self, change_id: str):
    """Draft the v(N+1) revision plan for ONE change and notify the PM.

    Dispatched by the silent-acceptance sweep (one job per ready change) so the
    LLM call this does runs off the sweep's hot path. Idempotent — auto_prepare_
    revision skips if a plan for the target version already exists.
    """
    import asyncio
    from app.services.kit_revision_runner import auto_prepare_revision

    try:
        created = asyncio.run(auto_prepare_revision(change_id))
        return {"change_id": change_id, "prepared": bool(created)}
    except Exception as e:
        logger.exception("prepare_revision_plan errored for change=%s", change_id)
        self.update_state(state="FAILURE", meta={"error": str(e), "change_id": change_id})
        raise


# ── Upload reconciliation — check an uploaded doc against the ratified plan ──
@celery_app.task(bind=True, name="reconciliation.reconcile_upload")
def reconcile_upload_task(self, change_id: str, doc_kind: str, doc_id: str, doc_version: int | None = None):
    """Reconcile ONE uploaded document against the ratified plan, off the upload's
    hot path. Loads the uploaded row's content, runs the plan-conformance check,
    and persists a pending DocumentReconciliation when there are conflicts.

    Best-effort: a failure here never affects the already-committed upload. The
    reconciler short-circuits when there is no ratified plan (accept as today) or
    the document is clean. BRD only for now; TSD is a later switch-on.
    """
    import asyncio
    from app.core.database import SessionLocal
    from app.agents.upload_reconciler import reconcile_upload

    def _content(db, kind: str, row_id: str) -> str | None:
        if kind == "brd":
            from app.models.brd import BRD
            row = db.get(BRD, row_id)
            return row.content if row else None
        if kind == "tech_spec":
            from app.models.tech_spec import TechSpec
            row = db.get(TechSpec, row_id)
            return row.content if row else None
        logger.warning("reconcile_upload_task: unsupported doc_kind=%s", kind)
        return None

    db = SessionLocal()
    try:
        try:
            from app.core.observability import set_usage_context as _set_usage_ctx
            _set_usage_ctx(change_request_id=change_id)
        except Exception:
            pass
        content = _content(db, doc_kind, doc_id)
        if not (content or "").strip():
            return {"change_id": change_id, "reconciled": False, "reason": "no content"}
        recon = asyncio.run(reconcile_upload(
            db, change_id=change_id, doc_kind=doc_kind,
            doc_content=content, doc_id=doc_id, doc_version=doc_version))
        n = len(recon.conflicts or []) if recon else 0
        logger.info("reconcile_upload_task: change=%s doc=%s conflicts=%d", change_id, doc_id, n)
        return {"change_id": change_id, "conflicts": n}
    except Exception as e:  # noqa: BLE001 — advisory background work; upload already succeeded
        logger.exception("reconcile_upload_task errored for change=%s doc=%s", change_id, doc_id)
        return {"change_id": change_id, "error": str(e)}
    finally:
        db.close()


# ── Reconciliation: correct the uploaded BRD for plan-wins conflicts ─────────
def _ground_reconciliation_deltas(db, change_id: str, recon, doc_kind: str) -> None:
    """Code-back the brd-wins/custom deltas against the real analysis checkout and store
    the result on ``recon.grounding``. Best-effort — never breaks the correction task."""
    import asyncio
    try:
        from app.agents.plan_versioning import reconciliation_deltas
        from app.agents.delta_grounding import ground_deltas
        from app.agents.plan_contract import build_plan_contract
        from app.agents.upload_reconciler import _analysis_checkouts
        deltas = reconciliation_deltas(recon)
        if not deltas:
            return
        plan_contract = build_plan_contract(db, change_id)
        checkouts = _analysis_checkouts(db, change_id, allow_clone=True)
        grounding = asyncio.run(ground_deltas(
            db, change_id=change_id, deltas=deltas, doc_kind=doc_kind,
            plan_contract=plan_contract, checkouts=checkouts))
        recon.grounding = grounding
        db.commit()
        logger.info("delta grounding: change=%s status=%s deltas=%d", change_id,
                    grounding.get("status"), len(grounding.get("deltas") or []))
    except Exception:  # noqa: BLE001 — grounding is advisory; the fold degrades to presence-check
        logger.warning("delta grounding failed for %s", change_id, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


@celery_app.task(bind=True, name="reconciliation.apply_corrections")
def apply_corrections_task(self, change_id: str, reconciliation_id: str):
    """After a resolution, correct the uploaded BRD for its 'plan-wins' conflicts:
    derive targeted {find,replace} edits (LLM) and apply them in place -> a new BRD
    version. Best-effort background work; the resolution already stands."""
    import asyncio
    from app.core.database import SessionLocal
    from app.models.document_reconciliation import DocumentReconciliation
    from app.agents.brd_corrector import (propose_brd_corrections, apply_doc_corrections,
                                           uncorrected_followups, _doc_model)

    db = SessionLocal()
    try:
        try:
            from app.core.observability import set_usage_context as _set_usage_ctx
            _set_usage_ctx(change_request_id=change_id)
        except Exception:
            pass
        recon = db.get(DocumentReconciliation, reconciliation_id)
        if not recon or not recon.resolutions:
            return {"change_id": change_id, "corrected": False}
        doc_kind = recon.doc_kind or "brd"
        Model, _draft = _doc_model(doc_kind)
        if Model is None:
            return {"change_id": change_id, "corrected": False, "reason": f"unsupported doc_kind {doc_kind}"}

        # Delta grounding (brd-wins/custom → plan amendments): code-back the accepted deltas
        # against the real checkout and store them on the row — so the fold at approval merges
        # structured grounding, not verbatim text. Runs regardless of whether any BRD
        # correction is derived below (a brd-wins-only resolution still needs grounding).
        _ground_reconciliation_deltas(db, change_id, recon, doc_kind)

        by_id = {c.get("id"): c for c in (recon.conflicts or [])}
        # Conflicts whose resolution should EDIT the doc: plan_wins (→ correct to the
        # plan) and custom (→ correct to the reviewer's ruling). Omissions add back.
        to_edit, additions = [], []
        for cid, r in recon.resolutions.items():
            c = by_id.get(cid)
            if not c:
                continue
            custom = ((r or {}).get("custom_answer") or "").strip()
            chosen = (r or {}).get("chosen_option_id")
            if custom:
                to_edit.append({**c, "_target": custom})
            elif chosen == "plan_wins":
                if c.get("jurisdiction") == "drops_requirement":
                    additions.append((c.get("evidence") or {}).get("detail") or c.get("text") or "")
                else:
                    to_edit.append({**c, "_target": "the ratified plan"})
        additions = [a for a in additions if a.strip()]
        doc = (db.query(Model).filter(Model.change_request_id == change_id)
               .order_by(Model.version.desc()).first())
        if not doc or not (doc.content or "").strip():
            return {"change_id": change_id, "corrected": False}
        corrections = asyncio.run(propose_brd_corrections(doc.content, to_edit)) if to_edit else []
        # Hardening: a plan-wins/custom conflict the LLM couldn't ground into a verbatim
        # edit must not no-op silently — surface it as a manual-review follow-up so the
        # user knows their resolution still needs a hand-edit (produces a version + note).
        followups = uncorrected_followups(to_edit, corrections)
        if not corrections and not additions and not followups:
            return {"change_id": change_id, "corrected": False, "reason": "no edits derived"}
        v = apply_doc_corrections(db, change_id, doc_kind, corrections, additions=additions + followups)
        logger.info("apply_corrections_task: change=%s kind=%s -> v%s (%d edits, %d added-back, %d manual-followups)",
                    change_id, doc_kind, v, len(corrections), len(additions), len(followups))
        return {"change_id": change_id, "new_doc_version": v, "edits": len(corrections),
                "added": len(additions), "followups": len(followups)}
    except Exception:  # noqa: BLE001 — best-effort; the resolution stands
        logger.exception("apply_corrections_task errored for change=%s", change_id)
        db.rollback()
        return {"change_id": change_id, "error": "failed"}
    finally:
        # Advance the lifecycle out of 'applying' no matter how this task ended — even a
        # failed/no-op correction must clear the regenerating state so the gate opens and
        # the user isn't stranded (the resolution itself already stands). Roll back first:
        # a mid-flush DB error above leaves the session poisoned, and db.get() below would
        # raise PendingRollbackError, skipping the status flip and stranding the row in
        # 'applying' forever.
        try:
            db.rollback()
            r = db.get(DocumentReconciliation, reconciliation_id)
            if r is not None and r.status == "applying":
                folded = None
                if (r.doc_kind or "brd") == "tech_spec":
                    # TSD has no approval to defer to → fold its deltas into the plan HERE,
                    # after grounding was stored above, so the new plan version merges the
                    # code-backed grounding. Runs on every exit path exactly once.
                    try:
                        from app.agents.plan_versioning import record_reconciliation_version
                        folded = record_reconciliation_version(
                            db, change_request_id=change_id, reconciliation=r)
                    except Exception:  # noqa: BLE001 — fold is best-effort
                        logger.warning("TSD fold failed for %s", change_id, exc_info=True)
                r.status = "applied" if folded else "resolved"
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        db.close()


# ── Slice 9 — partner secret age scan ──────────────────────────────────────
@celery_app.task(bind=True, name="a2a.scan_partner_secret_ages")
def scan_partner_secret_ages_task(self, max_age_days: int = 90):
    """Periodic scan that flags partners whose long-lived secrets are
    older than `max_age_days`. Logs a WARN per partner; the operator
    rotates via the per-secret endpoints during a maintenance window.

    `updated_at` is the proxy for "last touched" — the rotate-* endpoints
    bump this column. This is approximate (a non-secret field edit also
    bumps it) but the scan is a soft warning, not a hard gate, so the
    occasional false positive is fine. Slice 9's per-secret rotation
    timestamp columns (api_key_rotated_at etc.) would tighten this; the
    plan parks them as a follow-up.
    """
    from datetime import timedelta
    import logging
    from app.core.database import SessionLocal
    from app.models.phase_c import PartnerAgent, PartnerStatus
    from app.models.base import utcnow

    log = logging.getLogger("a2a.secret_age_scan")
    cutoff = utcnow() - timedelta(days=max_age_days)
    flagged: list[dict] = []

    db = SessionLocal()
    try:
        partners = db.query(PartnerAgent).filter(
            PartnerAgent.status == PartnerStatus.ACTIVE,
        ).all()
        for p in partners:
            stale = p.updated_at is not None and p.updated_at < cutoff
            if stale:
                log.warning(
                    "a2a_partner_secrets_stale partner_id=%s name='%s' "
                    "last_touched=%s max_age_days=%d",
                    p.id, p.name, p.updated_at.isoformat(), max_age_days,
                )
                flagged.append({"partner_id": p.id, "name": p.name})
        return {"scanned": len(partners), "flagged": flagged, "max_age_days": max_age_days}
    finally:
        db.close()


def find_orphan_governance_runs(db, idle_before):
    """Runs committed but never dispatched (worker died in the create→delay window,
    or nothing was consuming the queue they were routed to): active, lease-free,
    idle past the TTL, and in a DRIVEABLE phase.

    Runs parked at a human gate (awaiting_*) are lease-free + idle BY DESIGN —
    including them re-dispatched a gate-parked stage every sweep forever (observed
    live), so they are explicitly excluded.

    Covers EVERY run kind, not just governance. This filter used to be
    ``kind IN ('gov_ea','gov_is')``, which left a hole big enough to strand the
    platform: an `analysis`/`full`/`xsd`/`code` run created but never driven is
    lease-free, so `recover_runs` (which requires a NON-NULL expired lease) skips
    it too — meaning NOTHING could recover it. Such a run wedges forever, keeps
    `status='active'`, and holds its change's one-active-run slot, so the UI hands
    the same dead run back on every retry. The name is kept for import
    compatibility; the governance-only scope was never the intent, just the
    original caller.
    """
    from app.models.agentic import AgenticRun
    return (db.query(AgenticRun)
            .filter(AgenticRun.status == "active",
                    AgenticRun.lease_owner.is_(None),
                    AgenticRun.phase.notlike("awaiting_%"),
                    AgenticRun.updated_at < idle_before).all())


# ── Agentic codegen — run recovery sweep ───────────────────────────────────
@celery_app.task(bind=True, name="agentic.recover")
def agentic_recover_task(self):
    """Reclaim agentic runs whose lease expired (crashed/redeployed worker).

    Releases the stale lease + emits a `lease_expired_recovered` event so the
    run becomes re-claimable and the orchestrator resumes it from its persisted
    phase (§3/§14). Idempotent — only touches active runs past their lease.
    """
    from app.core.database import SessionLocal
    from app.agents.agentic_state import recover_runs, honour_cancel

    db = SessionLocal()
    try:
        recovered = recover_runs(db)
        db.commit()
        # Freeing the lease only makes a run RE-CLAIMABLE — it does not resume itself.
        # Re-dispatch so a worker picks it up and continues from the persisted phase
        # under a fresh lease (§3/§14). A run mid-`pushing` resumes via the PUSH task
        # (the drive loop has no pushing handler); everything else via the driver.
        # Per-run isolation: leases are already cleared, so a run that blows up here
        # would otherwise abort the loop and permanently orphan the rest of the batch
        # (the next sweep only finds runs whose lease is still set).
        from app.models.agentic import AgenticRun
        for rid in recovered:
            try:
                r = db.get(AgenticRun, rid)
                if not r:
                    continue
                # A user cancelled this run while its lease was stale/dead — the worker
                # that would have honoured the cooperative flag was already gone, so it
                # never fired. Terminate now instead of re-dispatching, or a wedged push
                # keeps crash-looping (lease expires → recovered → re-dispatched → wedges
                # again) forever.
                if r.cancel_requested:
                    honour_cancel(db, r)
                    db.commit()
                    continue
                # phase=completed only reaches here as a re-opened deferred push
                # (status flipped to active by /push) — that's push work, not drive work.
                if r.phase in ("pushing", "rebase_reverify", "completed"):
                    agentic_push_task.delay(rid)
                else:
                    agentic_drive_task.delay(rid)
            except Exception:  # noqa: BLE001 — keep sweeping; this run retries next cycle
                logger.exception("agentic.recover: re-dispatch failed for run %s", rid)
                db.rollback()
                # recover_runs() cleared and committed every stale lease before this
                # loop. Without restoring an expired marker here, this active run is
                # lease-free and therefore invisible to the next recovery sweep.
                # Re-arm it as stale so a transient broker/row failure is retried.
                try:
                    from app.models.base import utcnow
                    retry_run = db.get(AgenticRun, rid)
                    if retry_run and retry_run.status == "active":
                        retry_run.lease_owner = "agentic.recover:retry"
                        retry_run.lease_expires_at = utcnow()
                        retry_run.updated_at = utcnow()
                        db.commit()
                except Exception:  # noqa: BLE001 — preserve isolation for later rows
                    logger.exception("agentic.recover: could not re-arm run %s for retry", rid)
                    db.rollback()
        # F7: a run committed by chain_next_stage / start / ensure_analysis but never
        # dispatched (worker died in the create→delay window, or nothing was consuming
        # the queue it was routed to) is active, PENDING and LEASE-FREE — invisible to
        # recover_runs (which needs an expired lease) yet it blocks the next start via
        # the one-active-run guard. Reclaim lease-free active runs of ANY kind idle past
        # the lease TTL and dispatch them. Idle-guarded so a run genuinely mid-dispatch
        # is never double-driven.
        try:
            from datetime import timedelta
            from app.models.base import utcnow
            from app.core.config import settings as _cfg
            idle_before = utcnow() - timedelta(seconds=getattr(_cfg, "agentic_lease_ttl_seconds", 300))
            orphans = find_orphan_governance_runs(db, idle_before)
            for r in orphans:
                if r.id in recovered:
                    continue
                try:
                    from app.agents.agentic_events import emit_event
                    emit_event(db, r.id, "governance_orphan_recovered",
                               {"phase": r.phase, "kind": getattr(r, "kind", None),
                                "action": "♻ Reclaimed a run that was never picked up "
                                          "by a worker — resuming"})
                    db.commit()
                    if r.phase in ("pushing", "rebase_reverify", "completed"):
                        agentic_push_task.delay(r.id)
                    else:
                        agentic_drive_task.delay(r.id)
                except Exception:  # noqa: BLE001 — re-arm as a stale lease for the next sweep
                    logger.exception("agentic.recover: orphan re-dispatch failed for %s", r.id)
                    db.rollback()
                    o = db.get(AgenticRun, r.id)
                    if o and o.status == "active":
                        o.lease_owner = "agentic.recover:orphan"
                        o.lease_expires_at = utcnow()
                        db.commit()
        except Exception:  # noqa: BLE001 — orphan reconcile must never break the sweep
            logger.exception("agentic.recover: governance orphan reconcile failed")
            db.rollback()
        if recovered:
            self.update_state(state="SUCCESS", meta={"recovered": len(recovered)})
        return {"recovered": recovered}
    except Exception as e:
        db.rollback()
        self.update_state(state="FAILURE", meta={"error": str(e)})
        raise
    finally:
        db.close()


def _active_agentic_run_count(db) -> int:
    """Count of agentic runs currently EXECUTING on a worker, across all changes.
    Backs the A16 global concurrency cap — distinct from `uq_agentic_runs_active`,
    which only bounds ONE active run PER change request, not the platform total.

    "Executing" means ``status='active'`` AND the run holds an UNEXPIRED worker
    lease. The lease qualifier is load-bearing, not a refinement:

    `create_run` stamps ``status='active'`` at row-INSERT time, before any worker
    has seen the task. Counting bare `status='active'` therefore counted runs that
    were merely QUEUED — including, fatally, the very run this call is gating. With
    N >= cap runs sitting undispatched (e.g. nothing consuming the `agentic` queue
    because a worker was started without `-Q agentic`), every drive task counted its
    own idle siblings, deferred via `self.retry`, and never started a phase — so no
    run could ever reach a terminal state to free a slot. That is a permanent,
    self-inflicted deadlock: the queue is full of tasks whose only blocker is the
    existence of the other tasks in the queue, and `max_retries=None` means it
    never breaks on its own.

    Gating on the lease instead measures what the cap actually exists to bound —
    concurrent repo clones, LLM calls and Maven builds — all of which only happen
    AFTER `drive_run` acquires a lease. A queued-but-undriven run consumes none of
    those resources and must not count against the budget. Runs whose lease has
    expired (crashed/redeployed worker) are likewise excluded: they aren't running
    either, and `agentic.recover` reclaims them separately.
    """
    from app.models.agentic import AgenticRun, AgenticStatus
    from app.models.base import utcnow
    return (
        db.query(AgenticRun)
        .filter(
            AgenticRun.status == AgenticStatus.ACTIVE.value,
            AgenticRun.lease_owner.isnot(None),
            AgenticRun.lease_expires_at.isnot(None),
            AgenticRun.lease_expires_at > utcnow(),
        )
        .count()
    )


# ── Agentic codegen — orchestrator driver ───────────────────────────────────
@celery_app.task(bind=True, name="agentic.drive", max_retries=None)
def agentic_drive_task(self, run_id: str, intent: str = ""):
    """Drive an agentic run from its current phase to awaiting_human_approval (or
    terminal). Resumable: re-dispatching after a crash continues from the
    persisted phase under a fresh lease (§3).

    A3 (architecture review Critical #16, "No Bounded Queue or Backpressure
    on Agentic Runs") — before doing any work, checks the GLOBAL concurrent-
    active-run count against `agentic_max_concurrent_runs`. Each run clones a
    repo, holds a workspace, makes dozens of LLM calls, and runs Maven builds
    (JVM heap) — unbounded concurrency here is what OOM-kills workers under a
    burst of simultaneous changes. When over the cap, the task re-queues
    itself via Celery's own `self.retry(countdown=...)` (NOT a blocking
    sleep — this returns the worker slot immediately so it can pick up other
    short tasks) rather than proceeding, so excess changes wait in the
    QUEUE rather than all starting workspace clones simultaneously.
    """
    import asyncio
    from app.core.database import SessionLocal
    from app.agents.agentic_orchestrator import drive_run

    cap = int(getattr(settings, "agentic_max_concurrent_runs", 0) or 0)
    if cap > 0:
        db_check = SessionLocal()
        try:
            active_n = _active_agentic_run_count(db_check)
        finally:
            db_check.close()
        if active_n >= cap:
            delay = int(getattr(settings, "agentic_concurrency_requeue_delay_s", 30))
            logger.info(
                "agentic.drive DEFERRED run_id=%s — %d running runs >= cap %d; "
                "re-queuing in %ds (attempt %d)",
                run_id, active_n, cap, delay, self.request.retries,
            )
            # Tell the HUMAN why nothing is happening. Without this the UI sits on
            # "Getting ready…" with an empty event stream — indistinguishable from
            # "no worker is running" — while the task quietly re-queues every 30s.
            # Emitted once (first deferral only) so a long wait doesn't spam the
            # timeline. Fail-soft: a queue-position notice must never break dispatch.
            if self.request.retries == 0:
                try:
                    from app.core.database import SessionLocal as _SL
                    from app.agents.agentic_events import emit_event
                    _db = _SL()
                    try:
                        emit_event(_db, run_id, "queued_behind_cap", {
                            "running": active_n, "cap": cap,
                            "action": f"⏳ Queued — {active_n} run(s) already using all {cap} "
                                      f"slots. This run starts automatically when one frees up.",
                        })
                        _db.commit()
                    finally:
                        _db.close()
                except Exception:  # noqa: BLE001 — advisory only
                    logger.warning("agentic.drive: could not emit queued_behind_cap for %s", run_id)
            raise self.retry(countdown=delay)

    logger.info("agentic.drive received run_id=%s intent_len=%d", run_id, len(intent or ""))
    db = SessionLocal()
    try:
        result = asyncio.run(drive_run(db, run_id, intent=intent))
        logger.info("agentic.drive done run_id=%s result=%s", run_id, result)
        return result
    except Exception:
        # drive_run swallows+records phase errors itself; this catches the rarer
        # case where it raises (import error, lease/DB failure) so a stuck run isn't
        # silent in the worker log.
        logger.exception("agentic.drive crashed run_id=%s", run_id)
        raise
    finally:
        db.close()


@celery_app.task(bind=True, name="agentic.reverify")
def agentic_reverify_task(self, run_id: str):
    """On-demand re-verification of an already-generated/approved change: rebuild
    the existing workspace and record pass/fail. Read-only w.r.t. workflow state —
    never changes the run's phase/status. Sync work (mvn), so no asyncio wrapper."""
    from app.core.database import SessionLocal
    from app.agents.agentic_orchestrator import reverify_run

    logger.info("agentic.reverify received run_id=%s", run_id)
    db = SessionLocal()
    try:
        result = reverify_run(db, run_id)
        logger.info("agentic.reverify done run_id=%s result=%s", run_id, result)
        return result
    except Exception:
        logger.exception("agentic.reverify crashed run_id=%s", run_id)
        raise
    finally:
        db.close()


@celery_app.task(bind=True, name="agentic.push")
def agentic_push_task(self, run_id: str):
    """Resume an APPROVED run: preflight + the one guarded push + MR (§12/§22)."""
    import asyncio
    from app.core.database import SessionLocal
    from app.agents.agentic_orchestrator import push_run

    logger.info("agentic.push received run_id=%s", run_id)
    db = SessionLocal()
    try:
        result = asyncio.run(push_run(db, run_id))
        logger.info("agentic.push done run_id=%s result=%s", run_id, result)
        return result
    except Exception:
        logger.exception("agentic.push crashed run_id=%s", run_id)
        raise
    finally:
        db.close()


# ── Agentic codegen — workspace GC sweep ────────────────────────────────────
@celery_app.task(bind=True, name="agentic.gc_workspaces")
def agentic_gc_workspaces_task(self):
    """Remove terminal + past-TTL + lease-free agentic workspaces (§6).

    Never touches an active or leased workspace. Idempotent.
    """
    from app.core.database import SessionLocal
    from app.agents.workspace_local import gc_workspaces

    db = SessionLocal()
    try:
        removed = gc_workspaces(db)
        if removed:
            self.update_state(state="SUCCESS", meta={"removed": len(removed)})
        return {"removed": removed}
    except Exception as e:
        db.rollback()
        self.update_state(state="FAILURE", meta={"error": str(e)})
        raise
    finally:
        db.close()


@celery_app.task(bind=True, name="artifact.tiering_sweep")
def artifact_tiering_sweep_task(self):
    """Finding #8 (architecture review, "No Data Tiering for Large
    Payloads") — non-destructive tiering sweep. No-ops entirely unless
    `settings.artifact_tiering_enabled=True`. Compresses aging TSD/BRD/A2A
    content into workspace-local cold storage (a manifest row per
    compressed copy, source rows untouched) and flags old cold-storage
    entries as ready for an operator to move to real archive storage.
    See app/services/artifact_tiering.py for the full safety model.
    """
    from app.core.database import SessionLocal
    from app.services.artifact_tiering import run_tiering_sweep

    db = SessionLocal()
    try:
        result = run_tiering_sweep(db)
        self.update_state(state="SUCCESS", meta=result)
        return result
    except Exception as e:
        db.rollback()
        self.update_state(state="FAILURE", meta={"error": str(e)})
        raise
    finally:
        db.close()


@celery_app.task(bind=True, name="rag.ingest_all")
def ingest_knowledge_base(self, force: bool = False):
    """
    Celery task: ingest all documents from knowledge_base/ into pgvector.
    Runs asynchronously so the API call returns immediately.

    After ingestion, bumps a Redis key ("bm25:generation") so live FastAPI
    workers know to rebuild their in-memory BM25 index on the next query.
    """
    from app.core.database import SessionLocal
    from app.rag.ingestion import ingest_all

    self.update_state(state="STARTED", meta={"status": "Initialising embedding model…"})

    db = SessionLocal()
    try:
        self.update_state(state="PROGRESS", meta={"status": "Ingesting documents…"})
        summary = ingest_all(db, force=force)

        # Bump generation so FastAPI workers rebuild their BM25 index lazily
        try:
            import redis
            r = redis.from_url(settings.redis_url)
            r.incr("bm25:generation")
        except Exception:
            pass  # non-fatal: at worst the index is stale until a backend restart

        # Rebuild this worker's own BM25 index (useful if this task runs in-process)
        try:
            from app.rag import bm25_search
            bm25_search.build_index(db)
        except Exception:
            pass

        return {"status": "completed", **summary}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}
    finally:
        db.close()


# ── Phase A Product Kit — segmented video generation ────────────────────────
@celery_app.task(bind=True, name="video_gen.generate")
def generate_video_task(self, change_id: str, doc_type: str, job_id: str,
                        provider: str | None = None, model: str | None = None):
    """Generate + merge the segmented video for one product-kit video doc.

    The job_registry row (job_id) is created by the API before dispatch; this
    task drives it to terminal state. Each ≤8s segment is one provider call, so
    the whole task can run for several minutes — hence off the request path.
    """
    import asyncio
    from app.core.database import SessionLocal
    from app.services import job_registry
    from app.services.video_gen_runner import generate_video

    try:
        result = asyncio.run(generate_video(
            change_id=change_id, doc_type=doc_type, job_id=job_id,
            provider=provider, model=model,
        ))
        db = SessionLocal()
        try:
            job_registry.complete_job(db, job_id, result=result, final_stage="Video ready")
        finally:
            db.close()
        return {"status": "completed", "job_id": job_id, **result}
    except Exception as e:
        logger.exception("video_gen errored for change=%s doc=%s", change_id, doc_type)
        db = SessionLocal()
        try:
            job_registry.fail_job(db, job_id, error=str(e), final_stage="Video failed")
        finally:
            db.close()
        self.update_state(state="FAILURE", meta={"error": str(e), "change_id": change_id})
        raise
