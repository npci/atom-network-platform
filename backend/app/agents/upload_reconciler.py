# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Reconcile an uploaded document against the ratified Change-Analysis plan.

When a user uploads a BRD instead of using the generated one, it bypasses every
plan-conformance check the generate path runs. This module points the existing
plan-conformance auditor (``check_doc_against_plan``) at the uploaded content,
classifies each finding by how it relates to the plan (contradicts / extends /
drops a requirement), and persists a pending ``DocumentReconciliation`` that the
async gate blocks downstream generation on while unresolved.

Best-effort by contract: detection is one fail-open LLM call. A failure here must
never break the upload — it degrades to "no reconciliation" (accept as today).
Doc-kind-parameterized so TSD reconciliation is a later switch-on.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# A 'checking' or 'applying' row is a worker holding the gate closed while it works.
# If the worker died mid-task (killed, broker up/worker down, DB error before the
# lifecycle-advancing commit), nothing ever flips the row on — it blocks approval /
# TSD / codegen forever. Treat a row stuck past this TTL as non-blocking rather than
# trusting worker liveness; 'pending' (awaiting the user, not a worker) never expires.
_STALE_TRANSIENT_TTL_MINUTES = 15


def _jurisdiction(kind: str, detail: str) -> str:
    """Bucket an auditor finding by how it relates to the plan, so the UI can
    label the conflict and pick the option wording. The auditor emits free-form
    ``kind`` strings; unknown kinds fall back to "review". Direction (added vs
    dropped) must come from ``kind`` alone — an added item's free-text ``detail``
    routinely reads "...is not mentioned in the ratified plan", which the old
    detail-substring fallback matched as drops_requirement and inverted the
    resolution options (offered to drop an addition instead of extend the plan)."""
    k = (kind or "").lower()
    if any(t in k for t in ("contradict", "conflict", "mismatch", "regress", "wrong")):
        return "contradicts_plan"
    if any(t in k for t in ("endpoint", "api", "wire", "message", "schema",
                            "enum", "field", "extra", "undefined", "invent")):
        return "extends_plan"
    if any(t in k for t in ("missing", "omit", "absent", "uncovered", "dropped")):
        return "drops_requirement"
    return "review"


def _options(jurisdiction: str) -> list[dict]:
    """The three choices every conflict offers — BRD wins / plan wins / custom.
    Only the labels adapt to the jurisdiction; the last option carries the
    free-text box (the "options + open text" pattern)."""
    if jurisdiction == "drops_requirement":
        brd_label = "My BRD is right — drop this from the plan"
        plan_label = "The plan is right — add it back to the BRD"
    elif jurisdiction == "extends_plan":
        brd_label = "Keep it — add this to the plan"
        plan_label = "Out of scope — remove it from the BRD"
    else:
        brd_label = "My BRD is right — update the plan to match"
        plan_label = "The plan is right — correct the BRD"
    return [
        {"id": "brd_wins", "label": brd_label},
        {"id": "plan_wins", "label": plan_label},
        {"id": "custom", "label": "Resolve differently…", "free_text": True},
    ]


def classify_findings(findings: list[dict] | None) -> list[dict]:
    """Turn auditor findings into reconciliation conflicts (the clarification
    question shape). Pure — no I/O — so classification is unit-testable without
    the LLM. Each conflict::

        {id, text, jurisdiction, kind, severity, evidence, options}
    """
    conflicts: list[dict] = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        item = str(f.get("item") or "").strip()
        detail = str(f.get("detail") or "").strip()
        kind = str(f.get("kind") or "").strip()
        severity = str(f.get("severity") or "warning").strip().lower()
        juris = _jurisdiction(kind, detail)
        conflicts.append({
            "id": str(uuid.uuid4()),
            "text": detail or item or "This point in the document differs from the plan.",
            "jurisdiction": juris,
            "kind": kind,
            "severity": severity,
            "evidence": {"item": item, "detail": detail},
            "options": _options(juris),
        })
    return conflicts


def _dedup_findings(findings: list[dict]) -> list[dict]:
    """Drop duplicate findings that overlapping doc windows surface twice —
    keyed on (kind, item)."""
    seen: set = set()
    out: list[dict] = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        key = (str(f.get("kind") or "").lower(), str(f.get("item") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _alignment_conflict(f: dict) -> dict:
    """Synthesize a conflict from a BRD→plan alignment finding (the axis the other
    two detectors are blind to): a material commitment the doc ADDS beyond the plan
    (relation 'brd_only' → extends_plan), or one where the doc describes the SAME
    concern with an INCOMPATIBLE implementation story — mechanism / step order /
    responsible actor / scope / value (→ contradicts_plan, with the fine-grained
    relation carried in ``kind``)."""
    relation = str(f.get("relation") or "")
    juris = "extends_plan" if relation == "brd_only" else "contradicts_plan"
    detail = str(f.get("detail") or "").strip()
    commitment = str(f.get("commitment_text") or "").strip()
    if relation == "brd_only":
        text = f"Your document adds this beyond the plan: {commitment or detail}"
    else:
        text = detail or f"The document and the plan diverge on: {commitment}"
    return {
        "id": str(uuid.uuid4()),
        "text": text,
        "jurisdiction": juris,
        "kind": relation,
        "severity": str(f.get("severity") or "warning"),
        "evidence": {"item": str(f.get("item") or "") or commitment[:80], "detail": detail},
        "options": _options(juris),
    }


def _is_dup_conflict(item: str, conflicts: list[dict]) -> bool:
    """True when an alignment finding re-surfaces something the technical axis
    already flagged — keyed on the evidence item, containment either way."""
    key = (item or "").strip().lower()
    if not key:
        return False
    for c in conflicts:
        other = str((c.get("evidence") or {}).get("item") or "").strip().lower()
        if other and (key in other or other in key):
            return True
    return False


def _omission_conflict(req: dict) -> dict:
    """Synthesize a 'drops_requirement' conflict from a plan requirement the
    uploaded doc failed to cover (the axis the shared auditor is blind to)."""
    text = str(req.get("text") or "").strip()
    return {
        "id": str(uuid.uuid4()),
        "text": f"The plan requires this, but the uploaded BRD does not appear to cover it: {text}",
        "jurisdiction": "drops_requirement",
        "kind": "omission",
        "severity": "warning",
        "evidence": {"item": str(req.get("id") or ""), "detail": text},
        "options": _options("drops_requirement"),
    }


async def reconcile_upload(db, *, change_id: str, doc_kind: str, doc_content: str,
                           doc_id: str | None = None, doc_version: int | None = None):
    """Check an uploaded document against the ratified plan; persist the result.

    Returns the pending ``DocumentReconciliation`` when conflicts were found, or
    None when there is nothing to gate on — no ratified plan, a clean document,
    empty content, or a detection failure (fail-open → accept the upload as today).
    """
    from app.agents.plan_contract import build_plan_contract
    from app.agents.doc_consistency import check_doc_against_plan
    from app.agents.plan_coverage import (
        extract_plan_requirements, find_uncovered_requirements, windows,
    )
    from app.models.change_analysis import ChangeAnalysis
    from app.models.document_reconciliation import DocumentReconciliation

    if not (doc_content or "").strip():
        return None

    try:
        plan_contract = build_plan_contract(db, change_id)
    except Exception as e:  # noqa: BLE001 — never break the upload
        logger.warning("reconcile_upload: build_plan_contract failed for %s: %s", change_id, e)
        return None
    if not (plan_contract or "").strip():
        return None  # no ratified plan → nothing to reconcile against (accept as today)

    # Persist a 'checking' marker BEFORE the slow work — this is what lets the UI show a
    # loader ("checking your doc against the plan…") the moment the task starts, instead
    # of an ambiguous silence indistinguishable from 'done, clean'. Supersede any prior
    # open row for this doc kind (re-upload / re-check).
    supersede_open_reconciliations(db, change_id, doc_kind)
    try:
        db.commit()  # durable now — a failure below must not roll back the supersede too
    except Exception as e:  # noqa: BLE001 — never break the upload
        logger.warning("reconcile_upload: supersede commit failed for %s: %s", change_id, e)
        db.rollback()
    recon = DocumentReconciliation(
        change_request_id=change_id, doc_kind=doc_kind, doc_id=doc_id,
        doc_version=doc_version, status="checking", conflicts=[])
    try:
        db.add(recon)
        db.commit()
    except Exception as e:  # noqa: BLE001 — never break the upload
        logger.warning("reconcile_upload: checking-row persist failed for %s: %s", change_id, e)
        db.rollback()
        recon = None

    def _mark_clean():
        # No conflicts (or a detection failure) → accept the doc as-is. Drop the transient
        # 'checking' marker so the gate releases, the loader clears, and no inert row
        # lingers (keeps the 'clean upload → no row' contract).
        if recon is not None:
            try:
                db.delete(recon)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()

    try:
        # Detection runs three axes, all fail-open, one direction each.
        # (1) Technical tripwire — the shared auditor over every window: wire messages /
        #     schemas / endpoints the doc INVENTS, and ratified values it contradicts.
        # (2) BRD→plan alignment — the business/flow level the auditor is told to ignore:
        #     material commitments the doc ADDS beyond the plan (extends_plan) and places
        #     where it describes the SAME concern with an incompatible implementation
        #     story — mechanism / sequence / actor / scope / value (contradicts_plan).
        # (3) Omissions — plan requirements the doc drops (plan→doc direction).
        addition_findings: list[dict] = []
        try:
            for w in windows(doc_content):
                audit = await check_doc_against_plan(
                    doc_kind=doc_kind, doc_content=w, plan_contract=plan_contract)
                addition_findings.extend((audit or {}).get("findings") or [])
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("reconcile_upload: additions audit failed for %s: %s", change_id, e)
            addition_findings = []
        conflicts = classify_findings(_dedup_findings(addition_findings))

        try:
            from app.agents.doc_alignment import extract_doc_commitments, align_commitments
            commitments = await extract_doc_commitments(doc_content, doc_kind)
            align_findings = await align_commitments(plan_contract, commitments, doc_kind)
            conflicts.extend(_alignment_conflict(f) for f in align_findings
                             if not _is_dup_conflict(f.get("item") or "", conflicts))
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("reconcile_upload: alignment pass failed for %s: %s", change_id, e)

        try:
            reqs = await extract_plan_requirements(plan_contract)
            uncovered = await find_uncovered_requirements(reqs, doc_content)
            conflicts.extend(_omission_conflict(r) for r in uncovered)
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("reconcile_upload: omission pass failed for %s: %s", change_id, e)

        if not conflicts:
            _mark_clean()
            return None  # clean — nothing to gate on

        # Feasibility is computed HERE (async context, may shallow-clone) and STORED on
        # the conflicts, so the GET endpoint serves real-code-validated verdicts.
        try:
            feas = assess_feasibility(db, change_id, conflicts, allow_clone=True)
            for c in conflicts:
                v = feas.get(c.get("id")) or {}
                c["red_options"] = v.get("red") or []
                c["warn_options"] = v.get("warn") or []
                c["feasibility_reason"] = v.get("reason")
                c["feasibility_checked"] = True
        except Exception as e:  # noqa: BLE001 — feasibility is advisory
            logger.warning("reconcile_upload: feasibility failed for %s: %s", change_id, e)

        plan_version = None
        try:
            ca = (db.query(ChangeAnalysis)
                  .filter(ChangeAnalysis.change_request_id == change_id)
                  .order_by(ChangeAnalysis.version.desc()).first())
            plan_version = ca.version if ca else None
        except Exception:  # noqa: BLE001 — advisory field only
            plan_version = None

        # Flip the marker to 'pending' with the conflicts (or create one if the marker
        # write failed above). If a CONCURRENT upload superseded this row while this
        # (slow, LLM-driven) detection was still running, don't resurrect it — an
        # unconditional write here would silently overwrite that supersede back to
        # 'pending', reopening a gate the newer upload's flow already closed.
        if recon is not None:
            still_checking = (db.query(DocumentReconciliation)
                              .filter(DocumentReconciliation.id == recon.id,
                                      DocumentReconciliation.status == "checking")
                              .update({"status": "pending", "conflicts": conflicts,
                                       "plan_version_before": plan_version},
                                      synchronize_session=False))
            if not still_checking:
                logger.info("reconcile_upload: change=%s row %s no longer 'checking' "
                            "(superseded concurrently) — discarding this detection run",
                            change_id, recon.id)
                db.rollback()
                return None
            # synchronize_session=False leaves the in-memory object stale — sync it so
            # the returned recon reflects the flip (callers read recon.conflicts) without
            # depending on expire-on-commit reloading it.
            recon.status = "pending"
            recon.conflicts = conflicts
            recon.plan_version_before = plan_version
        else:
            recon = DocumentReconciliation(
                change_request_id=change_id, doc_kind=doc_kind, doc_id=doc_id,
                doc_version=doc_version, status="pending", conflicts=conflicts,
                plan_version_before=plan_version)
            db.add(recon)
        db.commit()
        logger.info("reconcile_upload: change=%s kind=%s conflicts=%d plan_v=%s",
                    change_id, doc_kind, len(conflicts), plan_version)
        return recon
    except Exception as e:  # noqa: BLE001 — never leave a stuck 'checking' row
        logger.warning("reconcile_upload: detection errored for %s: %s", change_id, e)
        _mark_clean()
        return None


def has_unresolved_reconciliation(db, change_id: str, doc_kind: str | None = None) -> bool:
    """True when an OPEN reconciliation should block downstream generation for this
    change. 'Open' = ``pending`` (conflicts not yet resolved) OR ``checking``/``applying``
    (a background task is producing the audit / corrected doc) — the doc isn't final in
    either case, so approval/TSD/XSD/codegen stay gated. The async gate calls this.

    ``checking``/``applying`` are worker-held states: nothing else ever flips them off.
    If the worker that was supposed to (task never enqueued to a live worker, killed
    mid-task, DB error before its terminal commit) never runs, the row is stuck open
    forever with no signal to the user. Past ``_STALE_TRANSIENT_TTL_MINUTES`` with no
    update, treat it as abandoned rather than blocking — worker liveness must not be
    load-bearing for whether the user can proceed. ``pending`` is not worker-held (it
    waits on the user) so it never expires. Best-effort False so a DB glitch here never
    blocks approval/generation (matches ``overturns_needs_ack`` below)."""
    from app.models.document_reconciliation import DocumentReconciliation
    try:
        q = (db.query(DocumentReconciliation)
             .filter(DocumentReconciliation.change_request_id == change_id,
                     DocumentReconciliation.status.in_(("checking", "pending", "applying"))))
        if doc_kind is not None:
            q = q.filter(DocumentReconciliation.doc_kind == doc_kind)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=_STALE_TRANSIENT_TTL_MINUTES)
        for r in q.all():
            if r.status == "pending":
                return True
            anchor = r.updated_at or r.created_at
            if anchor is None or anchor >= cutoff:
                return True
        return False
    except Exception as e:  # noqa: BLE001 — advisory gate; never block on a glitch
        logger.warning("has_unresolved_reconciliation query failed for %s: %s", change_id, e)
        return False


def overturns_needs_ack(db, change_id: str, doc_kind: str = "brd") -> bool:
    """True when the latest resolved reconciliation's delta-grounding flagged a change that
    OVERTURNS a ratified plan decision AND the user hasn't acknowledged it — a soft gate on
    approval (§8.1): the amendment is allowed, but must be seen first. Best-effort False so a
    query glitch never blocks approval."""
    from app.models.document_reconciliation import DocumentReconciliation
    try:
        r = (db.query(DocumentReconciliation)
             .filter(DocumentReconciliation.change_request_id == change_id,
                     DocumentReconciliation.doc_kind == doc_kind,
                     DocumentReconciliation.status.in_(("resolved", "applied")))
             .order_by(DocumentReconciliation.created_at.desc()).first())
        g = (r.grounding if r else None) or {}
        if g.get("status") != "ok" or g.get("overturns_acked"):
            return False
        return any(d.get("overturns_ratified") for d in (g.get("deltas") or []))
    except Exception:  # noqa: BLE001 — advisory gate; never block on a glitch
        return False


def supersede_open_reconciliations(db, change_id: str, doc_kind: str) -> int:
    """Mark pending/resolved reconciliations for this doc superseded — used when the
    upload they describe is replaced or withdrawn (re-upload, revert-to-generated,
    generate-instead, explicit dismiss) so stale conflicts don't linger or fold a
    superseded doc's decisions into the plan. Staged, not committed (the caller
    commits). Best-effort. Returns the count updated."""
    from app.models.document_reconciliation import DocumentReconciliation
    try:
        return (db.query(DocumentReconciliation)
                .filter(DocumentReconciliation.change_request_id == change_id,
                        DocumentReconciliation.doc_kind == doc_kind,
                        DocumentReconciliation.status.in_(("checking", "pending", "applying", "resolved")))
                .update({"status": "superseded"}, synchronize_session=False)) or 0
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("supersede_open_reconciliations failed for %s: %s", change_id, e)
        return 0


def validate_resolutions(conflicts: list[dict], resolutions: dict) -> tuple[bool, str | None]:
    """Every conflict must be answered — a chosen option, or a non-empty custom
    answer for the free-text choice. Returns (ok, error_message). Pure."""
    resolutions = resolutions or {}
    for c in conflicts or []:
        cid = c.get("id")
        r = resolutions.get(cid)
        if not isinstance(r, dict):
            return False, f"Missing resolution for conflict {cid}"
        chosen = str(r.get("chosen_option_id") or "").strip()
        custom = str(r.get("custom_answer") or "").strip()
        if not chosen and not custom:
            return False, f"Conflict {cid} has no answer"
        if chosen == "custom" and not custom:
            return False, f"Conflict {cid}: the custom option needs text"
        valid = {o.get("id") for o in (c.get("options") or [])}
        if chosen and chosen not in valid:
            return False, f"Conflict {cid}: unknown option '{chosen}'"
    return True, None


def apply_reconciliation_on_brd_approval(db, change_id: str, approved_by: str | None = None) -> int:
    """At BRD approval, fold any RESOLVED (not-yet-applied) uploaded-BRD
    reconciliation into a new plan version (BRD-wins/custom deltas) and mark it
    applied. Idempotent: only ``status='resolved'`` rows fire, and each flips to
    ``applied`` so a re-approval never double-versions the plan. Best-effort —
    never breaks the approval. Returns the number applied."""
    from app.models.document_reconciliation import DocumentReconciliation
    from app.agents.plan_versioning import record_reconciliation_version
    applied = 0
    try:
        # Lock the resolved rows for this change before reading them — without this, a
        # double-clicked (or racing double-request) approval has both transactions read
        # status='resolved' before either commits its 'applied' flip, and both fold the
        # same reconciliation into a new plan version (double plan-version). The second
        # transaction blocks here until the first commits, then re-reads and finds
        # 'applied' rows already excluded by the WHERE clause.
        recons = (db.query(DocumentReconciliation)
                  .filter(DocumentReconciliation.change_request_id == change_id,
                          DocumentReconciliation.doc_kind == "brd",
                          DocumentReconciliation.status == "resolved")
                  .with_for_update()
                  .all())
        for recon in recons:
            # Stage the flip BEFORE the version write: when a version IS created,
            # record_reconciliation_version's commit flushes the flip atomically
            # with it — no window where a version exists but the row is still
            # 'resolved' (which would double-version on re-approval).
            recon.status = "applied"
            record_reconciliation_version(db, change_request_id=change_id,
                                          reconciliation=recon, decided_by=approved_by)
            applied += 1
        if recons:
            db.commit()  # flush flips for the no-delta (all-plan-wins) case
    except Exception as e:  # noqa: BLE001 — never break the approval
        logger.warning("apply_reconciliation_on_brd_approval failed for %s: %s", change_id, e)
        try:
            db.rollback()
        except Exception:
            pass
    return applied


# ── Code-grounded feasibility (B1-v3): validate against the ACTUAL repo pull ──
def _analysis_checkouts(db, change_id: str, allow_clone: bool = False) -> list:
    """The git checkouts to validate against — preferring THE SAME PULL the
    clarification-stage analysis used (its per-run workspace persists on disk:
    ``WORKSPACE_ROOT/<run_id>/<repo_id>/``). Fallback (celery context only,
    ``allow_clone=True``): a cached shallow clone per repo under the workspace
    root, so feasibility still has real code when the run workspace was GC'd.
    Returns a list of Paths; [] when nothing is available (callers degrade to
    the cached index)."""
    from pathlib import Path
    out: list = []
    repo_ids = _change_repo_ids(db, change_id)
    if not repo_ids:
        return out
    try:
        from app.agents import workspace_local
        from app.models.agentic import AgenticRun
        runs = (db.query(AgenticRun).filter(AgenticRun.change_request_id == change_id)
                .order_by(AgenticRun.created_at.desc()).limit(6).all())
        for rid in repo_ids:
            found = None
            for run in runs:
                d = workspace_local.repo_dir(run.id, rid)
                if (d / ".git").exists():
                    found = d
                    break
            if found is None and allow_clone:
                found = _cached_clone(db, rid)
            if found is not None:
                out.append(found)
    except Exception as e:  # noqa: BLE001 — advisory; degrade to the index
        logger.warning("feasibility checkouts unavailable for %s: %s", change_id, e)
    return out


def _cached_clone(db, repo_id: str):
    """Shallow-clone the repo's branch into a persistent per-repo cache dir
    (``WORKSPACE_ROOT/_reconcile_cache/<repo_id>``) and return its Path — or the
    existing cache if already cloned. None on any failure."""
    import subprocess
    from pathlib import Path
    try:
        from app.core.config import settings
        from app.agents import workspace_local
        from app.models.code_repo import CodeRepo
        repo = db.get(CodeRepo, repo_id)
        if repo is None:
            return None
        dest = Path(settings.agentic_workspace_root) / "_reconcile_cache" / repo_id
        if (dest / ".git").exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = workspace_local.build_clone_url(
            repo.gitlab_url or settings.gitlab_url, repo.gitlab_repo, settings.gitlab_token)
        # Validate the ref before it lands in an option slot (a leading '-' would
        # be parsed as a flag); `--` then ends option parsing for url/dest.
        safe_branch = workspace_local.validate_git_ref(repo.gitlab_branch or "main")
        r = subprocess.run(["git", "clone", "--depth", "1", "--branch",
                            safe_branch, "--", url, str(dest)],
                           capture_output=True, text=True, timeout=180)
        return dest if r.returncode == 0 else None
    except Exception as e:  # noqa: BLE001 — advisory
        logger.warning("feasibility cache clone failed for %s: %s", repo_id, e)
        return None


def _wire_entity_in_code(checkouts: list, name: str):
    """Does ``name`` (a wire schema/message base name) exist in the ACTUAL code?
    Checks filenames (``git ls-files``) and content (``git grep -il``) in every
    checkout. True = found · False = every checkout searched clean · None = no
    checkout could be searched (can't tell)."""
    import subprocess
    base = (name or "").rsplit(".", 1)[0]
    if not base or not checkouts:
        return None
    searched = 0
    low = base.lower()
    for d in checkouts:
        try:
            ls = subprocess.run(["git", "ls-files"],
                                cwd=str(d), capture_output=True, text=True, timeout=30)
            gr = subprocess.run(["git", "grep", "-il", base],
                                cwd=str(d), capture_output=True, text=True, timeout=60)
            # grep exit 1 = clean search with no hits; other codes = search failed
            if ls.returncode == 0 and gr.returncode in (0, 1):
                searched += 1
                # filenames matched case-insensitively in Python (git pathspecs are case-sensitive)
                if any(low in ln.lower() for ln in (ls.stdout or "").splitlines()) \
                        or (gr.stdout or "").strip():
                    return True
        except Exception:  # noqa: BLE001 — one checkout failing must not fabricate absence
            continue
    return False if searched else None


def _wire_entity_paths(checkouts: list, name: str) -> list:
    """The schema files (``.xsd`` / ``.xjb``) whose name matches wire entity ``name``,
    as ``[(repo_id, path)]`` — the checkout dir name IS the repo_id (workspace layout
    ``<run_id>/<repo_id>/``). Used to REGISTER reconciliation-added schema surface in
    ``ChangeImpactedPath`` so cross-change collision detection + XSD planning can see it.
    Capped at 5. Empty on no match / no checkout."""
    import subprocess
    base = (name or "").rsplit(".", 1)[0]
    if not base or not checkouts:
        return []
    low = base.lower()
    out: list = []
    for d in checkouts:
        try:
            ls = subprocess.run(["git", "ls-files"], cwd=str(d),
                                capture_output=True, text=True, timeout=30)
            if ls.returncode != 0:
                continue
            for ln in (ls.stdout or "").splitlines():
                p = ln.strip()
                if low in p.lower() and p.lower().endswith((".xsd", ".xjb")):
                    out.append((d.name, p))
                    if len(out) >= 5:
                        return out
        except Exception:  # noqa: BLE001 — advisory; a checkout failing just yields fewer paths
            continue
    return out


def _checkout_heads(checkouts: list) -> dict:
    """``{repo_id: HEAD sha}`` for the checkouts feasibility/grounding read — so a plan
    amendment can record the EXACT commit its wire deltas were grounded against, instead
    of dishonestly inheriting the original ``analysis_sha``. Fail-open ``{}``."""
    import subprocess
    out: dict = {}
    for d in checkouts:
        try:
            r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(d),
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                out[d.name] = r.stdout.strip()
        except Exception:  # noqa: BLE001 — advisory
            continue
    return out


def _frozen_schema_files(db, change_id: str) -> set:
    """Schema filenames the Phase-A XSD freeze actually locked (from the xsd run's
    ChangeManifest). Empty when there is no freeze yet (the BRD stage) — which the
    feasibility check treats as 'can't tell', never 'impossible'."""
    try:
        from app.models.agentic import AgenticRun, ChangeManifest
        xrun = (db.query(AgenticRun)
                .filter(AgenticRun.change_request_id == change_id, AgenticRun.kind == "xsd")
                .order_by(AgenticRun.created_at.desc()).first())
        if xrun is None:
            return set()
        man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == xrun.id)
               .order_by(ChangeManifest.created_at.desc()).first())
        out: set = set()
        for op in ((man.operations if man else None) or []):
            p = (op.get("path") or "") if isinstance(op, dict) else ""
            if p.lower().endswith((".xsd", ".xjb")):
                out.add(p.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower())   # base name
        return out
    except Exception:  # noqa: BLE001 — advisory
        return set()


def _change_repo_ids(db, change_id: str) -> list:
    """The repos this change is scoped to — the latest agentic run's selection,
    falling back to the repos named in the plan's impacted paths."""
    try:
        from app.models.agentic import AgenticRun
        run = (db.query(AgenticRun).filter(AgenticRun.change_request_id == change_id)
               .order_by(AgenticRun.created_at.desc()).first())
        ids = list(run.selected_repo_ids or []) if run and run.selected_repo_ids else []
        if ids:
            return ids
        from app.models.change_analysis import ChangeImpactedPath
        rows = (db.query(ChangeImpactedPath.repo_id)
                .filter(ChangeImpactedPath.change_request_id == change_id).distinct().all())
        return [r[0] for r in rows if r[0]]
    except Exception:  # noqa: BLE001 — advisory
        return []


def _plan_index_stale(db, ca) -> bool:
    """True when the code index has moved from the SHA the plan was grounded on
    (``analysis_sha``) — the cached facts may be outdated, so feasibility should
    say 'can't tell' rather than assert. (The merge fixed the false-stale bug, so
    this is now accurate.)"""
    try:
        from app.agents import repo_scope
        ash = getattr(ca, "analysis_sha", None) or {}
        return any(repo_scope.is_stale(db, rid, sha) for rid, sha in ash.items())
    except Exception:  # noqa: BLE001 — advisory
        return False


def _wire_names(conflict: dict) -> set:
    """Wire schema files (*.xsd) + wire message names (ReqXxx/RespXxx) a conflict is
    about, lowercased — the surface an XSD freeze can lock."""
    text = ((conflict.get("evidence") or {}).get("item") or "") + " " + (conflict.get("text") or "")
    raw = set(re.findall(r"[\w-]+\.xsd", text, re.I))
    raw |= set(re.findall(r"\b(?:Req|Resp)[A-Z][A-Za-z0-9]+", text))
    return {n.rsplit(".", 1)[0].lower() for n in raw}   # base names (a schema + its message collapse)


def _plan_wire_surface(db, change_id: str) -> set:
    """The wire surface the ratified plan is scoped to touch — schema_inventory
    filenames + flow_spec message names, lowercased. Anything inside this is in
    scope (buildable); outside it would need a new wire surface."""
    try:
        from app.models.change_analysis import ChangeAnalysis
        ca = (db.query(ChangeAnalysis).filter(ChangeAnalysis.change_request_id == change_id)
              .order_by(ChangeAnalysis.version.desc()).first())
        if ca is None:
            return set()
        out: set = set()
        for i in ((ca.technical_analysis or {}).get("schema_inventory") or []):
            p = (i.get("path") if isinstance(i, dict) else str(i)) or ""
            if p:
                out.add(p.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower())   # base name
        for m in ((ca.flow_spec or {}).get("messages") or []):
            out |= {x.lower() for x in re.findall(r"\b(?:Req|Resp)[A-Z][A-Za-z0-9]+", str(m))}
        return out
    except Exception:  # noqa: BLE001 — advisory
        return set()


def _wire_entity_indexed(db, repo_ids: list, name: str):
    """Does the wire schema/message ``name`` exist in the CACHED schema graph for
    these repos? True = exists (reuse is buildable) · False = graph present but
    lacks it · None = no schema graph indexed for these repos (can't tell)."""
    if not repo_ids:
        return None
    try:
        from sqlalchemy import or_
        from app.models.xsd_graph import XsdSchemaNode, XsdJavaLink
        if db.query(XsdSchemaNode).filter(XsdSchemaNode.repo_id.in_(repo_ids)).limit(1).first() is None:
            return None  # schema graph not indexed for these repos → can't tell
        base = name.rsplit(".", 1)[0]
        if db.query(XsdSchemaNode).filter(
                XsdSchemaNode.repo_id.in_(repo_ids),
                or_(XsdSchemaNode.path.ilike(f"%{base}%"),
                    XsdSchemaNode.target_namespace.ilike(f"%{base}%"))).first() is not None:
            return True
        if db.query(XsdJavaLink).filter(
                XsdJavaLink.repo_id.in_(repo_ids),
                XsdJavaLink.xpath.ilike(f"%{base}%")).first() is not None:
            return True
        return False
    except Exception:  # noqa: BLE001 — can't determine
        return None


def assess_feasibility(db, change_id: str, conflicts: list, allow_clone: bool = False) -> dict:
    """Code-grounded feasibility, validated against the ACTUAL repo pull — the same
    checkout the clarification-stage analysis used (fallback: the cached schema
    graph). For every extends/contradicts conflict that names a wire schema/message
    outside the plan's surface:

      · exists in the code            → note it (reuse — buildable, informative reason)
      · absent + wire FROZEN          → RED on brd_wins (can't be built without unfreezing)
      · absent + wire not frozen yet  → WARN on brd_wins (adopting it means NEW wire
                                        build the plan never scoped — possible, but flagged)

    'Can't tell' (no checkout AND no index) stays silent — never red on a guess.
    Returns {conflict_id: {'red': [...], 'warn': [...], 'reason', 'evidence'}}.
    ``allow_clone=True`` (celery context) lets it shallow-clone when the analysis
    workspace was GC'd; the request path never clones.
    """
    from app.models.change_analysis import ChangeAnalysis
    ca = (db.query(ChangeAnalysis).filter(ChangeAnalysis.change_request_id == change_id)
          .order_by(ChangeAnalysis.version.desc()).first())
    if ca is None:
        return {}                       # nothing ratified to validate against
    frozen = _frozen_schema_files(db, change_id)
    repo_ids = _change_repo_ids(db, change_id)
    checkouts = _analysis_checkouts(db, change_id, allow_clone=allow_clone)
    plan_surface = _plan_wire_surface(db, change_id)
    out: dict = {}
    for c in (conflicts or []):
        if c.get("jurisdiction") not in ("extends_plan", "contradicts_plan"):
            continue                    # omissions/reviews aren't a buildability question
        absent, reused = [], []
        source = "checkout"
        for name in _wire_names(c):
            if name in frozen or name in plan_surface:
                continue                # inside the locked / ratified wire surface → buildable
            exists = _wire_entity_in_code(checkouts, name)
            if exists is None:
                exists = _wire_entity_indexed(db, repo_ids, name)   # fallback: cached graph
                source = "index"
            if exists is True:
                reused.append(name)     # already in the code → reuse, not a build risk
            elif exists is False:
                absent.append(name)     # definitively absent from the actual code
        if not absent:
            if reused:
                out[c.get("id")] = {
                    "red": [], "warn": [],
                    "reason": "‘" + "’, ‘".join(sorted(reused)) + "’ already exists in the code (reuse).",
                    "evidence": {"reused": sorted(reused)},
                }
            continue
        names = "‘" + "’, ‘".join(sorted(absent)) + "’"
        if frozen:
            out[c.get("id")] = {
                "red": ["brd_wins"], "warn": [],
                "reason": (f"{names} isn’t in the code (checked against the analysis checkout) and the "
                           "wire schema is frozen for this change — adopting it into the plan can’t be built."),
                "evidence": {"absent": sorted(absent), "source": source},
            }
        else:
            out[c.get("id")] = {
                "red": [], "warn": ["brd_wins"],
                "reason": (f"{names} isn’t in the code (checked against the analysis checkout) nor the "
                           "ratified plan — adopting it means NEW wire/schema build the plan never scoped."),
                "evidence": {"absent": sorted(absent), "source": source},
            }
    return out
