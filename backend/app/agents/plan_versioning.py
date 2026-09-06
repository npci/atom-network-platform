# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Plan versioning at the approach gate.

When the human picks an approach at the reuse-vs-new gate that DIVERGES from the ratified
Change-Analysis plan's recommendation, we don't silently leave the plan contradicting what
gets built. We create the NEXT plan version (v+1) that records the chosen approach and WHY
it diverged, mark the prior version ``superseded``, and carry the rest of the plan forward
unchanged — so the plan a reviewer reads always matches the decision that was actually made.

Best-effort by contract: a failure here must never break the human's gate decision (the
decision is already persisted to the run before this is called).
"""
from __future__ import annotations

import logging

from app.models.base import utcnow

logger = logging.getLogger(__name__)


def record_approach_decision_version(db, *, change_request_id: str, run_id: str | None,
                                     chosen: dict, decided_by: str | None,
                                     kind: str = "approach_decision") -> int | None:
    """Create ``change_analyses`` v+1 capturing a plan-diverging approach decision.

    ``chosen`` is the full approach option the human selected (id/title/approach/
    target_api/divergence_note). ``kind`` labels the plan_revisions changelog entry —
    "approach_decision" at the reuse-vs-new gate, "refine_supersession" when an approved
    XSD refine request supersedes that earlier decision. Returns the new version number,
    or ``None`` when there is no prior plan to supersede (nothing to diverge from) or the
    write fails.
    """
    from app.models.change_analysis import ChangeAnalysis
    try:
        prior = (db.query(ChangeAnalysis)
                 .filter(ChangeAnalysis.change_request_id == change_request_id)
                 .order_by(ChangeAnalysis.version.desc()).first())
        if prior is None:
            return None  # no ratified plan — the gate had nothing to diverge from
        new_version = (prior.version or 1) + 1
        carried_status = prior.status  # the body is unchanged; keep its ratification state

        decision = {
            "chosen_option_id": chosen.get("id"),
            "chosen_title": chosen.get("title"),
            "approach": chosen.get("approach"),
            "target_api": chosen.get("target_api"),
            "diverges_from_plan": True,
            "why": (chosen.get("divergence_note") or chosen.get("how_it_fits") or "").strip(),
            "decided_by": decided_by,
            "decided_at": utcnow().isoformat(),
            "supersedes_version": prior.version,
        }
        # Annotate a COPY of the plan body — approach_decision is authoritative; plan_revisions
        # is the append-only changelog of why each version exists.
        # Codex P2 fix (light): the old `data_model_changes` / pre-gate technical recommendation
        # may now CONTRADICT the chosen approach. Naive consumers reading the raw plan can still
        # receive the stale recommendation, so flag it at the top of `technical_analysis` so a
        # downstream reader can detect 'this section is no longer authoritative — see
        # approach_decision'. The renderer already prioritises approach_decision; this is
        # defense-in-depth for non-renderer consumers.
        tech = dict(prior.technical_analysis or {})
        tech["approach_decision"] = decision
        if "data_model_changes" in tech:
            tech["data_model_changes_superseded_by_approach_decision"] = True
        revisions = list(tech.get("plan_revisions") or [])
        revisions.append({"version": new_version, "kind": kind, **decision})
        tech["plan_revisions"] = revisions

        prior.status = "superseded"
        db.add(ChangeAnalysis(
            change_request_id=change_request_id, run_id=run_id, version=new_version,
            status=carried_status,
            technical_analysis=tech,
            functional_plan=dict(prior.functional_plan or {}),
            flow_spec=dict(prior.flow_spec or {}),
            analysis_sha=prior.analysis_sha,
            validated_against_brd_id=prior.validated_against_brd_id,
            validated_against_brd_version=prior.validated_against_brd_version,
            validated_against_brd_hash=prior.validated_against_brd_hash,
            pm_ratified_by=prior.pm_ratified_by, pm_ratified_at=prior.pm_ratified_at,
            tech_ratified_by=prior.tech_ratified_by, tech_ratified_at=prior.tech_ratified_at,
        ))
        db.commit()
        return new_version
    except Exception as e:  # noqa: BLE001 — versioning is best-effort; the decision must stand
        logger.warning("approach-decision plan version failed for %s: %s", change_request_id, e)
        db.rollback()
        return None


def reconciliation_deltas(reconciliation) -> list[dict]:
    """The brd-wins / custom resolutions that CHANGE the plan (plan-wins leaves it
    untouched — the BRD is corrected instead). Each: ``{resolution, conflict, directive}``.
    Shared by the grounding pass (applying phase) and the fold (approval)."""
    conflicts = {c.get("id"): c for c in (getattr(reconciliation, "conflicts", None) or [])}
    resolutions = getattr(reconciliation, "resolutions", None) or {}
    out: list[dict] = []
    for cid, r in resolutions.items():
        chosen = (r or {}).get("chosen_option_id")
        custom = ((r or {}).get("custom_answer") or "").strip()
        # A resolution can reference a conflict id no longer on the row (edited/re-run
        # between the answer being recorded and this read) — don't let a missing lookup
        # surface as the literal string "None" in a directive shown to reviewers/agents.
        text = (conflicts.get(cid) or {}).get("text") or "this item"
        if chosen == "brd_wins":
            out.append({"resolution": "brd_wins", "conflict": text,
                        "directive": f"The uploaded BRD is authoritative here — {text}"})
        elif custom:
            out.append({"resolution": "custom", "conflict": text, "directive": custom})
    return out


def record_reconciliation_version(db, *, change_request_id: str, reconciliation,
                                  decided_by: str | None = None) -> int | None:
    """Create ``change_analyses`` v+1 folding an uploaded-BRD reconciliation's
    resolutions into the plan.

    Only ``brd_wins`` and custom-text resolutions change the plan (the BRD's
    position becomes authoritative); ``plan_wins`` leaves the plan untouched (the
    BRD is corrected instead — Path B). Accepted deltas are recorded as
    authoritative addenda in ``technical_analysis.plan_revisions`` +
    ``upload_reconciliation_addenda`` (which ``build_plan_contract`` renders into
    the binding contract), and ``validated_against_brd_*`` is re-stamped to the
    uploaded BRD. Ratification carries forward — this runs at BRD approval, so the
    human gate already covered it (no separate plan sign-off).

    Best-effort: returns the new version, or None when nothing changed the plan or
    the write fails (the resolution itself is already persisted)."""
    from app.models.change_analysis import ChangeAnalysis
    try:
        deltas = reconciliation_deltas(reconciliation)
        if not deltas:
            return None

        prior = (db.query(ChangeAnalysis)
                 .filter(ChangeAnalysis.change_request_id == change_request_id)
                 .order_by(ChangeAnalysis.version.desc()).first())
        if prior is None:
            return None
        new_version = (prior.version or 1) + 1
        carried_status = prior.status  # capture before superseding

        tech = dict(prior.technical_analysis or {})
        revisions = list(tech.get("plan_revisions") or [])
        # Validate the NEW plan's accepted deltas against the same repo pull the
        # clarification-stage analysis used: every wire entity a delta introduces is
        # checked in the actual checkout, and the evidence rides the revision entry.
        code_validation: list[dict] = []
        impacted: list = []          # (repo_id, path) schema surface to register — S1.2
        grounded_sha: dict = {}
        try:
            from app.agents.upload_reconciler import (
                _analysis_checkouts, _wire_entity_in_code, _wire_entity_paths,
                _wire_names, _checkout_heads)
            checkouts = _analysis_checkouts(db, change_request_id, allow_clone=True)
            grounded_sha = _checkout_heads(checkouts)   # the EXACT commit these deltas were checked against
            seen: set = set()
            for d in deltas:
                for name in _wire_names({"text": f"{d.get('conflict') or ''} {d.get('directive') or ''}"}):
                    if name in seen:
                        continue
                    seen.add(name)
                    exists = _wire_entity_in_code(checkouts, name)
                    code_validation.append({
                        "entity": name,
                        "in_code": exists,
                        "note": ("exists in the code (reuse)" if exists
                                 else "NOT in the code — new build required" if exists is False
                                 else "could not check (no checkout)"),
                    })
                    if exists is True:
                        impacted.extend(_wire_entity_paths(checkouts, name))
        except Exception as e:  # noqa: BLE001 — validation is advisory
            logger.warning("reconciliation code-validation failed for %s: %s", change_request_id, e)
        # S2.4: merge the precomputed delta grounding (from the applying phase). Structural,
        # not prose — schema additions extend the inventory (tagged with their origin), so
        # LLM variance can never mutate ratified content, and downstream (XSD, contract) sees
        # the reconciliation-added surface. Falls back to the S1 presence-check when absent.
        grounding = getattr(reconciliation, "grounding", None) or {}
        g_deltas = grounding.get("deltas") if grounding.get("status") == "ok" else None
        g_addenda: list[str] = []
        if g_deltas:
            inv = tech.get("schema_inventory")
            if isinstance(inv, list):
                # `tech` is a SHALLOW copy of prior.technical_analysis — this list object is
                # still the one stored on the PRIOR version. Copy before appending so the
                # prior version's schema_inventory isn't mutated in place underneath it.
                inv = list(inv)
                have_paths = {i.get("path") for i in inv if isinstance(i, dict)}
                for gd in g_deltas:
                    for s in (gd.get("schema_inventory_add") or []):
                        p = s.get("path") if isinstance(s, dict) else None
                        if p and p not in have_paths:          # dedup vs existing inventory
                            have_paths.add(p)
                            inv.append({"path": p, "note": s.get("note"),
                                        "origin": f"reconciliation v{new_version}"})
                tech["schema_inventory"] = inv
            # Planned-NEW schemas must reach ChangeImpactedPath too — S1 only registers
            # entities that already exist in the code; without this a grounding-proposed
            # schema stays invisible to cross-change collision detection until XSD phase.
            # repo unknown pre-XSD → "" (same convention as _persist_change_analysis).
            for gd in g_deltas:
                for s in (gd.get("schema_inventory_add") or []):
                    p = (s.get("path") or "") if isinstance(s, dict) else ""
                    if p.lower().endswith((".xsd", ".xjb")):
                        impacted.append(("", p))
            # Route the grounding's data-model + reuse insights into the AUTHORITATIVE
            # addenda (build_plan_contract renders these as reconciled overrides). Type-safe,
            # unlike mutating the string-or-list schema_inventory/data_model_changes fields —
            # this is what makes the amendment's data-model reality reach downstream agents.
            for gd in g_deltas:
                for dm in (gd.get("data_model_changes_add") or []):
                    g_addenda.append(f"Data-model change (code-grounded): {dm}")
                for ru in (gd.get("reuse") or []):
                    g_addenda.append(f"Reuse existing code: {ru}")

        revisions.append({
            "version": new_version, "kind": "upload_reconciliation",
            "decided_by": decided_by, "decided_at": utcnow().isoformat(),
            "doc_kind": getattr(reconciliation, "doc_kind", None) or "brd",
            "doc_version": getattr(reconciliation, "doc_version", None),
            "deltas": deltas,
            "grounded_at": utcnow().isoformat(),
            **({"code_validation": code_validation} if code_validation else {}),
            **({"grounded_sha": grounded_sha} if grounded_sha else {}),
            **({"grounding": g_deltas} if g_deltas else {}),
        })
        tech["plan_revisions"] = revisions
        tech["upload_reconciliation_addenda"] = (
            list(tech.get("upload_reconciliation_addenda") or [])
            + [d["directive"] for d in deltas] + g_addenda
        )

        # Bind staleness to the CURRENT latest BRD — a plan_wins correction may have
        # produced a newer BRD version than the one originally reconciled, so
        # reconciliation.doc_id can be stale (Flaw-2 fix).
        val_id = getattr(reconciliation, "doc_id", None)
        val_ver = getattr(reconciliation, "doc_version", None)
        try:
            from app.models.brd import BRD
            _lb = (db.query(BRD).filter(BRD.change_request_id == change_request_id)
                   .order_by(BRD.version.desc()).first())
            if _lb is not None:
                val_id, val_ver = _lb.id, _lb.version
        except Exception:  # noqa: BLE001 — advisory
            pass

        prior.status = "superseded"
        db.add(ChangeAnalysis(
            change_request_id=change_request_id, run_id=prior.run_id, version=new_version,
            status=carried_status,
            technical_analysis=tech,
            functional_plan=dict(prior.functional_plan or {}),
            flow_spec=dict(prior.flow_spec or {}),
            analysis_sha=prior.analysis_sha,
            validated_against_brd_id=val_id or prior.validated_against_brd_id,
            validated_against_brd_version=val_ver or prior.validated_against_brd_version,
            validated_against_brd_hash=prior.validated_against_brd_hash,
            pm_ratified_by=prior.pm_ratified_by, pm_ratified_at=prior.pm_ratified_at,
            tech_ratified_by=prior.tech_ratified_by, tech_ratified_at=prior.tech_ratified_at,
        ))

        # S1.2: register reconciliation-added schema surface so cross-change collision
        # detection + XSD _planned_files can see what the amendment touched. ADD-only and
        # deduped — never rebuild (unlike _persist_change_analysis, which owns the full set).
        if impacted:
            from app.models.change_analysis import ChangeImpactedPath
            have = {(r.repo_id, r.path) for r in
                    db.query(ChangeImpactedPath.repo_id, ChangeImpactedPath.path)
                    .filter(ChangeImpactedPath.change_request_id == change_request_id).all()}
            for repo_id, path in impacted:
                if (repo_id, path) not in have:
                    have.add((repo_id, path))
                    db.add(ChangeImpactedPath(change_request_id=change_request_id,
                                              repo_id=repo_id, path=path, kind="xsd"))

        db.commit()
        logger.info("reconciliation plan version v%d for %s (%d deltas, %d impacted paths)",
                    new_version, change_request_id, len(deltas), len(impacted))
        return new_version
    except Exception as e:  # noqa: BLE001 — versioning is best-effort; the resolution already stands
        logger.warning("reconciliation plan version failed for %s: %s", change_request_id, e)
        db.rollback()
        return None
