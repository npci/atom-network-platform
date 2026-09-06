# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The orchestrator — drives the durable state machine through every phase
(THE BOOK §3 + §13 capstone).

``drive_run`` walks a leased run from ``pending`` to ``awaiting_human_approval``,
calling each phase body (S4 workspace → S8 context → S9 xsd/code → S10 verify →
S11 review → S12 freeze) and advancing the S2 state machine, emitting an event
per transition. It stops at ``awaiting_human_approval`` (a human approves the
exact manifest hash); ``push_run`` resumes after approval, under the git-guard
policy (§22). Crash-resume: ``drive_run`` restarts from the persisted ``phase``
(phases are idempotent; artifacts are rebuilt from the workspace / DB).

Convention: the handler for the phase a run is IN performs that phase's work and
advances to its successor.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from app.agents import (
    agentic_push,
    agentic_review,
    agentic_subagents,
    context_assembler,
    git_guard,
    manifest as M,
    plan_files,
    repo_scope,
    toolchain_report,
    verification_plan,
    workspace_local,
)
from app.agents.verifier import select_verifier
from app.agents.agentic_events import emit_event
from app.agents.platform_adapter import adapter
from app.agents import agentic_state as S
from app.core.config import settings
from app.models.agentic import AgenticPhase as P, AgenticRun, AgenticStatus, TERMINAL_STATUSES

logger = logging.getLogger("app.agentic")


def _amendment_key(a: dict) -> str:
    """Stable identity for a staged schema amendment: the file plus the exact text it
    replaces. Used to remember which proposals a human already ruled on, so a re-stage of a
    decided edit is ignored while a genuinely different one still opens the gate."""
    return "|".join((str(a.get("repo_id") or ""), str(a.get("path") or ""),
                     str(a.get("kind") or "edit"), str(a.get("old_string") or "")[:400]))


def _ws_id(run: AgenticRun) -> str:
    """The run_id whose on-disk clone holds this run's working tree (THE BOOK v3.4).

    For a ``full``/``xsd`` run that's the run itself. A Phase-B (``code``) run sets
    ``workspace_run_id`` to its Phase-A parent so it edits/verifies/pushes the SAME
    tree Phase A left the approved XSDs in — giving a single combined MR with no
    context gap. Every disk-touching call routes through this."""
    return getattr(run, "workspace_run_id", None) or run.id


# ── Phase bodies ──────────────────────────────────────────────────────────────

def _phase_workspace(db, run: AgenticRun, art: dict) -> None:
    """Clone selected repos, record base SHAs, toolchain preflight (§5/§6/§18).

    A Phase-B (``code``) run does NOT clone — it adopts Phase A's workspace so the
    approved XSDs are already on disk (combined MR). See ``_adopt_parent_workspace``."""
    if getattr(run, "kind", "full") == "code":
        _adopt_parent_workspace(db, run, art)
        return
    # First COMMITTED progress of this phase. Unlike the agent-loop phases (which
    # commit each event live via the runtime's _emit), the workspace phase emits +
    # flushes only — so without an explicit commit here the whole phase (clone + the
    # LLM-backed indexing below) is invisible until the phase boundary, leaving the UI
    # frozen on `run_created`. Commit each milestone as it happens so progress streams.
    emit_event(db, run.id, "workspace_start",
               {"repos": len(run.selected_repo_ids or []),
                "action": "📦 Preparing sandbox — validating selection + build toolchain"})
    db.commit()
    logger.info("workspace: start run=%s repos=%s", run.id, run.selected_repo_ids)
    repos = repo_scope.validate_selection(db, run.selected_repo_ids)
    report = toolchain_report.build_toolchain_report(use_cache=False)
    if not report.ok:
        # Only truly-blocking gaps (git / GITLAB_TOKEN) stop the run. A missing BUILD
        # toolchain is NOT fatal — verification degrades to the deferred backend (§9).
        raise RuntimeError(f"preflight failed: missing {report.blocking_missing}")
    if not report.build_ready:
        emit_event(db, run.id, "verifier_degraded",
                   {"missing": [t for t in ("mvn", "javac") if not (report.tools.get(t) and report.tools[t].found)],
                    "action": "⚠ No local build toolchain — changes will be marked UNVERIFIED (verify via CI)"})
    # The feature branch is born HERE, at provisioning — not at push time. The whole
    # run (XSD edits, Phase-B code, verification) happens ON it, Phase B adopts the
    # same tree, and the push/MR ships the same name — so the branch the human sees
    # in the UI is the branch the work actually lives on, from the first edit.
    feature_branch = ((run.handoff_json or {}).get("feature_branch")
                      or agentic_push.branch_name(_title(db, run)))
    base_sha: dict[str, str] = {}
    for repo in repos:
        gl = repo.gitlab_url or settings.gitlab_url
        url = workspace_local.build_clone_url(gl, repo.gitlab_repo, settings.gitlab_token)
        emit_event(db, run.id, "repo_cloning",
                   {"repo_id": repo.id, "repo": repo.gitlab_repo, "branch": repo.gitlab_branch,
                    "action": f"📥 Creating sandbox + cloning {repo.gitlab_repo} ({repo.gitlab_branch})"})
        db.commit()                              # show "cloning" BEFORE the slow clone
        logger.info("workspace: cloning run=%s repo=%s (%s @ %s)",
                    run.id, repo.id, repo.gitlab_repo, repo.gitlab_branch)
        base_sha[repo.id] = workspace_local.clone(run.id, repo.id, url, repo.gitlab_branch)
        workspace_local.create_branch(run.id, repo.id, feature_branch)
        sandbox = str(workspace_local.repo_dir(run.id, repo.id))
        logger.info("workspace: cloned run=%s repo=%s base_sha=%s", run.id, repo.id, base_sha[repo.id][:8])
        emit_event(db, run.id, "repo_cloned",
                   {"repo_id": repo.id, "sandbox": sandbox, "base_sha": base_sha[repo.id],
                    "action": f"🗂 Sandbox ready: {repo.gitlab_repo} @ {base_sha[repo.id][:8]} → {sandbox}"})
        db.commit()
        # Java-version awareness (§18.1): check EARLY what JDK the repo targets. If a
        # matching JDK is installed (chosen from the system alternatives), the verify
        # gate will build with it (auto-switch) — just note it. If it's NOT installed,
        # ADVISE the user to install it (we never auto-install); a later
        # `invalid target release: N` would otherwise look like a code error.
        if report.build_ready:
            required = verification_plan.detect_required_java(workspace_local.repo_dir(run.id, repo.id))
            active = max(report.jdk_majors) if report.jdk_majors else None
            if required is not None:
                from app.agents import jdk_discovery
                home = jdk_discovery.select_jdk_home(required)
                if home and active != required:
                    emit_event(db, run.id, "java_version_switch",
                               {"repo_id": repo.id, "required_java": required, "active_java": active,
                                "java_home": home,
                                "action": f"♻ {repo.gitlab_repo} targets Java {required}; the build will use the "
                                          f"installed JDK {required} (active is {active})."})
                    db.commit()
                elif not home and required != active:
                    emit_event(db, run.id, "java_version_warning",
                               {"repo_id": repo.id, "required_java": required, "active_java": active,
                                "action": f"⚠ {repo.gitlab_repo} targets Java {required}, which is NOT installed "
                                          f"on the build host (active JDK is {active}). Please install JDK "
                                          f"{required}, then resume — this is a toolchain gap, not a code error."})
                    db.commit()
        # §22 — scrub the token from origin so a tool-spawned git push can't reach
        # the remote (runs now, before any git-guard policy is set).
        workspace_local.set_remote(run.id, repo.id, workspace_local.build_clone_url(gl, repo.gitlab_repo, ""))
        # Populate XSD graph + JAXB links + module_context from the CLONE (ground
        # truth) — so find_existing_xsd, the XSD diff, and module orientation have
        # real data. Fail-soft: never blocks the run.
        _index_repo_artifacts(db, run.id, repo.id, base_sha[repo.id])
    art["repos"] = repos
    art["repo_base_sha"] = base_sha
    art["branch"] = feature_branch
    # MERGE into handoff_json (don't overwrite) so the branch survives crash-resume
    # and is visible to the Phase-B child + the classic Phase-B pipeline.
    h = dict(run.handoff_json or {})
    h["feature_branch"] = feature_branch
    run.handoff_json = h
    run.platform = report.platform
    emit_event(db, run.id, "workspace_ready",
               {"base_sha": base_sha, "platform": report.platform, "branch": feature_branch,
                "action": f"✅ Workspace ready — {len(repos)} repo(s) cloned on branch {feature_branch}"})
    db.commit()
    logger.info("workspace: ready run=%s repos=%d platform=%s branch=%s",
                run.id, len(repos), report.platform, feature_branch)


def _adopt_parent_workspace(db, run: AgenticRun, art: dict) -> None:
    """Phase B (``code``) inherits Phase A's workspace instead of cloning, so the
    approved XSD edits are already in the tree (one combined MR, no context gap).

    Happy path: the shared clone (keyed by the parent's run_id) is still on disk →
    adopt it, base SHA = its recorded HEAD. Fallback: the workspace was GC'd before
    Phase B started → re-clone into the shared dir and re-materialize the XSDs from
    the parent's persisted ``handoff_json``."""
    ws_id = _ws_id(run)
    # Committed progress immediately — same reason as _phase_workspace: this body
    # emits+flushes only, so without explicit commits Phase B looks frozen on
    # `run_created` while it adopts/re-clones the shared tree.
    emit_event(db, run.id, "workspace_start",
               {"repos": len(run.selected_repo_ids or []), "adopting_from": run.parent_run_id,
                "action": "📦 Continuing Phase-A workspace (combined MR)"})
    db.commit()
    logger.info("workspace(adopt): start run=%s ws_id=%s parent=%s", run.id, ws_id, run.parent_run_id)
    repos = repo_scope.validate_selection(db, run.selected_repo_ids)
    report = toolchain_report.build_toolchain_report(use_cache=False)
    if not report.ok:
        raise RuntimeError(f"preflight failed: missing {report.blocking_missing}")
    if not report.build_ready:
        emit_event(db, run.id, "verifier_degraded",
                   {"missing": [t for t in ("mvn", "javac") if not (report.tools.get(t) and report.tools[t].found)],
                    "action": "⚠ No local build toolchain — changes will be marked UNVERIFIED (verify via CI)"})
    parent = db.get(AgenticRun, run.parent_run_id) if run.parent_run_id else None
    handoff = (parent.handoff_json if parent else None) or {}
    base_sha: dict[str, str] = {}
    for repo in repos:
        rd = workspace_local.repo_dir(ws_id, repo.id)
        if (rd / ".git").exists():
            base_sha[repo.id] = workspace_local.read_base_sha(ws_id, repo.id)
            if (run.handoff_json or {}).get("fresh_codegen"):
                # Clean-slate RE-RUN (consistency re-test): wipe any prior Phase-B edits and
                # restore the EXACT Phase-A-approved baseline — pinned base SHA + approved XSDs —
                # so every re-run starts byte-identical (a dirty adopted tree would make outputs
                # incomparable). Nothing is cached; the agent re-generates from scratch.
                workspace_local.reset_to_base(ws_id, repo.id, base_sha[repo.id])
                n = workspace_local.materialize_files(ws_id, repo.id, handoff.get("xsd_files") or [])
                emit_event(db, run.id, "workspace_reset_for_rerun",
                           {"repo_id": repo.id, "base_sha": base_sha[repo.id], "xsd_files": n,
                            "action": f"🔄 Reset {repo.gitlab_repo} to the approved baseline ({n} XSD) for a clean re-run"})
            emit_event(db, run.id, "workspace_adopted",
                       {"repo_id": repo.id, "from_run": run.parent_run_id, "base_sha": base_sha[repo.id],
                        "action": f"🔗 Adopted Phase-A workspace: {repo.gitlab_repo} @ {base_sha[repo.id][:8]}"})
            db.commit()
        else:
            # Shared workspace gone (GC'd during the PRODUCT_KIT→Phase-B gap) → rebuild it.
            gl = repo.gitlab_url or settings.gitlab_url
            url = workspace_local.build_clone_url(gl, repo.gitlab_repo, settings.gitlab_token)
            emit_event(db, run.id, "repo_recloning",
                       {"repo_id": repo.id, "repo": repo.gitlab_repo,
                        "action": f"📥 Phase-A workspace gone — re-cloning {repo.gitlab_repo} + restoring approved XSDs"})
            db.commit()                          # show "re-cloning" before the slow clone
            base_sha[repo.id] = workspace_local.clone(ws_id, repo.id, url, repo.gitlab_branch)
            # Recreate the feature branch the parent provisioned on (deterministic:
            # same title → same name), so the rebuilt tree matches the lost one.
            workspace_local.create_branch(
                ws_id, repo.id,
                handoff.get("feature_branch") or agentic_push.branch_name(_title(db, run)))
            workspace_local.set_remote(ws_id, repo.id, workspace_local.build_clone_url(gl, repo.gitlab_repo, ""))
            xsd_files = handoff.get("xsd_files") or []
            n = workspace_local.materialize_files(ws_id, repo.id, xsd_files)
            # Guard: if Phase A approved XSDs for this repo but the handoff is empty/
            # corrupt, re-materialize writes 0 files and Phase B would code against a
            # vanilla checkout — silently wrong. Fail loudly so the human re-runs Phase A
            # instead of getting an MR built on the wrong schema.
            expected = [f for f in xsd_files if (f.get("repo_id") in (None, repo.id))]
            if expected and n == 0:
                raise RuntimeError(
                    f"Phase-A handoff for {repo.gitlab_repo} restored 0 of {len(expected)} approved "
                    "XSD file(s) — the parent workspace was lost and the handoff is incomplete. "
                    "Re-run Phase A (XSD) for this change before Phase B.")
            # The XSD graph / JAXB links / module_context Phase A persisted (keyed by
            # repo_id + base_sha) survive in the DB, so context rebuilds without re-indexing.
            emit_event(db, run.id, "xsd_restored",
                       {"repo_id": repo.id, "files": n,
                        "action": f"♻ Restored {n} approved XSD file(s) from Phase A"})
        # Early JDK-mismatch advice (§18.1), same as the fresh-clone path — Phase B
        # inherits the workspace, so without this the first signal of a wrong JDK would
        # be a late build failure. Note an auto-switch (matching JDK installed) or warn
        # to install it, so a later `invalid target release: N` reads as a toolchain gap.
        if report.build_ready:
            required = verification_plan.detect_required_java(rd)
            active = max(report.jdk_majors) if report.jdk_majors else None
            if required is not None:
                from app.agents import jdk_discovery
                home = jdk_discovery.select_jdk_home(required)
                if home and active != required:
                    emit_event(db, run.id, "java_version_switch",
                               {"repo_id": repo.id, "required_java": required, "active_java": active,
                                "java_home": home,
                                "action": f"♻ {repo.gitlab_repo} targets Java {required}; the build will use the "
                                          f"installed JDK {required} (active is {active})."})
                    db.commit()
                elif not home and required != active:
                    emit_event(db, run.id, "java_version_warning",
                               {"repo_id": repo.id, "required_java": required, "active_java": active,
                                "action": f"⚠ {repo.gitlab_repo} targets Java {required}, which is NOT installed "
                                          f"on the build host (active JDK is {active}). Please install JDK "
                                          f"{required}, then resume — this is a toolchain gap, not a code error."})
                    db.commit()
    # Consume the one-shot re-run flag so a later RESUME (fresh worker) doesn't wipe the
    # in-progress edits by resetting again — resume must continue from disk.
    if (run.handoff_json or {}).get("fresh_codegen"):
        _h = dict(run.handoff_json or {}); _h.pop("fresh_codegen", None); run.handoff_json = _h
    art["repos"] = repos
    art["repo_base_sha"] = base_sha
    # Inherit the parent's provisioned feature branch (the tree is already ON it) so
    # this run's freeze/push and the classic Phase-B pipeline reuse the SAME branch.
    feature_branch = (handoff.get("feature_branch")
                      or (run.handoff_json or {}).get("feature_branch"))
    if feature_branch:
        art["branch"] = feature_branch
        _h = dict(run.handoff_json or {}); _h["feature_branch"] = feature_branch
        run.handoff_json = _h
    run.platform = report.platform
    emit_event(db, run.id, "workspace_ready",
               {"base_sha": base_sha, "platform": report.platform, "adopted_from": run.parent_run_id,
                "branch": feature_branch,
                "action": f"✅ Phase-B workspace ready (continuing Phase A) — {len(repos)} repo(s)"})
    db.commit()
    logger.info("workspace(adopt): ready run=%s repos=%d branch=%s", run.id, len(repos), feature_branch)


_INDEX_FILE_CAP = 5000
_PRUNE_DIRS = {".git", "target", "node_modules", "build", ".idea", ".gradle", "dist"}


def _index_repo_artifacts(db, run_id: str, repo_id: str, base_sha: str) -> None:
    """Build the XSD schema graph + JAXB links + module_context for a freshly
    cloned repo, from the clone (ground truth). Java files are pre-filtered to
    JAXB-bound ones (contain ``@Xml``) to bound cost on large repos. Fail-soft."""
    import os
    from app.agents import xsd_graph_builder, jaxb_mapper, module_context_generator, flow_context_generator
    from app.models.module_context import ModuleContext
    from app.models.flow_context import FlowContext
    from app.models.xsd_graph import XsdSchemaNode
    rd = workspace_local.repo_dir(run_id, repo_id)
    # Already-indexed fast path: when the schema graph + module map + flow map all
    # exist for THIS exact commit (a prior run or the scheduled code-indexing
    # pipeline built them), sandbox indexing is not a step at all — skip the whole
    # walk + LLM generation and just note the reuse. The expensive parts (one AiNxt
    # call per module + the tree walk on a multi-GB clone) are pure waste here.
    have_mod = db.query(ModuleContext.repo_id).filter(
        ModuleContext.repo_id == repo_id, ModuleContext.base_commit_sha == base_sha).first() is not None
    have_flow = db.query(FlowContext.repo_id).filter(
        FlowContext.repo_id == repo_id, FlowContext.base_commit_sha == base_sha).first() is not None
    have_xsd = db.query(XsdSchemaNode.repo_id).filter(
        XsdSchemaNode.repo_id == repo_id, XsdSchemaNode.base_commit_sha == base_sha).first() is not None
    if have_mod and have_flow and have_xsd:
        logger.info("index: SKIP run=%s repo=%s sha=%s — already indexed (schema+modules+flows)",
                    run_id, repo_id, (base_sha or "")[:8])
        emit_event(db, run_id, "repo_indexed",
                   {"repo_id": repo_id, "reused": True,
                    "action": "♻ Reusing existing index (schema graph + module/flow map)"})
        db.commit()
        return
    # Committed marker BEFORE the LLM-backed module/flow generation below — that's the
    # slow part (one AiNxt call per module + one for the flow map), and the place a
    # stalled gateway used to silently wedge the run. Now bounded (see _run_coro) AND
    # visible.
    emit_event(db, run_id, "repo_indexing",
               {"repo_id": repo_id,
                "action": "🧱 Indexing sandbox (schema graph + module map) — this can take a minute"})
    db.commit()
    logger.info("index: start run=%s repo=%s", run_id, repo_id)
    try:
        xsd_files, java_files, xjb_files = [], [], []
        scanned = 0
        # os.walk with dir PRUNING so we don't descend the .git object store or
        # build/dep trees on a 200MB-2GB clone (the same fix as glob, §6).
        for dirpath, dirnames, filenames in os.walk(rd):
            dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
            if scanned > _INDEX_FILE_CAP:
                break
            for fn in filenames:
                low = fn.lower()
                if not low.endswith((".xsd", ".xjb", ".java")):
                    continue
                if scanned > _INDEX_FILE_CAP:
                    break
                scanned += 1
                p = Path(dirpath) / fn
                rel = p.relative_to(rd).as_posix()
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if low.endswith(".xsd"):
                    xsd_files.append({"path": rel, "content": content})
                elif low.endswith(".xjb"):
                    xjb_files.append({"path": rel, "content": content})
                elif "@Xml" in content:                 # only JAXB-bound java
                    java_files.append({"path": rel, "content": content})
        if xsd_files:
            xsd_graph_builder.persist_graph(db, repo_id, xsd_files, base_commit_sha=base_sha)
        links = jaxb_mapper.build_links(java_files=java_files, xjb_files=xjb_files)
        if links:
            jaxb_mapper.persist_links(db, repo_id, links, base_commit_sha=base_sha)
        # Module/flow context is ALSO built (in parallel) by the scheduled
        # code-indexing pipeline (api/code_indexing.py) and stored keyed by
        # repo_id + base_commit_sha. The generators below DELETE + rebuild with
        # one LLM call per module — slow on big monorepos and pure waste when the
        # sandbox is at an already-indexed commit. Reuse when fresh rows exist for
        # this exact sha; only (re)generate what's missing or stale.
        # have_mod / have_flow were computed at the top (DB unchanged since). We only
        # reach here when at least one of schema/module/flow was missing — reuse what
        # exists, (re)generate only what doesn't.
        if have_mod and have_flow:
            logger.info("index: reusing module/flow context run=%s repo=%s sha=%s (already indexed)",
                        run_id, repo_id, (base_sha or "")[:8])
        else:
            logger.info("index: generating module/flow context run=%s repo=%s xsd=%d "
                        "(LLM-backed; have_mod=%s have_flow=%s)",
                        run_id, repo_id, len(xsd_files), have_mod, have_flow)
            if not have_mod:
                module_context_generator.maybe_generate_module_context(db, repo_id, rd, base_sha)
            # Flow map (which API carries the transaction leg vs meta APIs) — after module
            # context, since it reads those entry points. Best-effort; never blocks indexing.
            if not have_flow:
                flow_context_generator.maybe_generate_flow_context(db, repo_id, base_sha)
        logger.info("index: done run=%s repo=%s xsd=%d jaxb=%d", run_id, repo_id, len(xsd_files), len(links))
        emit_event(db, run_id, "repo_indexed",
                   {"repo_id": repo_id, "xsd_nodes": len(xsd_files), "jaxb_links": len(links),
                    "action": f"🧱 Indexed sandbox — {len(xsd_files)} XSD, {len(links)} JAXB links"})
    except Exception as e:  # noqa: BLE001 — indexing is best-effort orientation, but VISIBLE:
        # a silent miss here weakens the reuse-first gate + schema discovery, so surface it.
        logger.warning("repo artifact indexing skipped for %s: %s", repo_id, e)
        try:
            emit_event(db, run_id, "repo_index_failed",
                       {"repo_id": repo_id, "error": repr(e)[:200],
                        "action": "⚠ Sandbox indexing failed — reuse/schema discovery may be weaker"})
        except Exception:  # noqa: BLE001
            pass


def _phase_context(db, run: AgenticRun, art: dict) -> None:
    import time as _time
    logger.info("context: assembling run=%s repos=%s", run.id, run.selected_repo_ids)
    _t0 = _time.monotonic()
    art["ctx"] = context_assembler.assemble_context_pack(
        db, change_request_id=run.change_request_id, selected_repo_ids=run.selected_repo_ids,
        repo_base_sha=art["repo_base_sha"], run_id=run.id, intent=art.get("intent", ""),
        tsd_version_locked=getattr(run, "tsd_version_locked", None))
    logger.info("context: assembled run=%s impact_files=%d stale_index=%s in %dms",
                run.id, len(art["ctx"].impact_files), art["ctx"].stale_index,
                int((_time.monotonic() - _t0) * 1000))
    emit_event(db, run.id, "context_ready",
               {"stale": art["ctx"].stale_index, "impact_files": len(art["ctx"].impact_files),
                "action": "🧭 Context assembled"})


def _heartbeat(db, run: AgenticRun, art: dict):
    """Lease-renewal callback the long agent loops call each iteration so a healthy
    long phase keeps its lease (§3) and isn't reclaimed mid-flight."""
    owner, rid = art.get("_owner", "orchestrator"), run.id

    def _beat() -> None:
        S.renew_lease(db, rid, owner)
        db.commit()

    return _beat


async def _phase_propose(db, run: AgenticRun, art: dict, model) -> dict | None:
    """Reuse-first decision pass: the agent maps the existing flows and proposes
    OPTIONS (no edits). Phase A (kind='xsd') asks ONLY the schema question, in plain
    language; a full run asks the broader reuse-vs-new question."""
    # Give the approach agent the ratified plan so it can flag, per option, whether the
    # option diverges from what the plan recommended (drives the gate's divergence badge).
    plan_block = _analysis_plan_block(db, run.change_request_id)
    # Author-memory across runs: continue the ratified analysis run's transcript so propose
    # doesn't redo the identical read-only discovery sweep (same 14 tools, same flows). The
    # analysis run is linked by change_request_id only (no parent_run_id for xsd/full kinds).
    # Skipped when the repo selections drifted — the transcript's reads would reference repos
    # outside this run's workspace. Fail-open: any problem → fresh exploration (legacy path).
    resume = None
    if settings.agentic_propose_replay_transcript and db is not None:
        try:
            from sqlalchemy import select
            arun = db.scalars(
                select(AgenticRun).where(AgenticRun.change_request_id == run.change_request_id,
                                         AgenticRun.kind == "analysis")
                .order_by(AgenticRun.created_at.desc())
            ).first()
            if arun is not None and set(arun.selected_repo_ids or []) <= set(run.selected_repo_ids or []):
                resume = (arun.handoff_json or {}).get("analysis_transcript")
        except Exception as e:  # noqa: BLE001 — replay is an optimization, never a blocker
            logger.debug("propose: analysis-transcript lookup failed (%s) — fresh exploration", e)
    proposal = await agentic_subagents.run_approach_proposal(
        db, run_id=run.id, ctx=art["ctx"], intent=art.get("intent", ""), model=model,
        scope="xsd" if (run.kind or "full") == "xsd" else "full", plan_block=plan_block,
        resume_transcript=resume,
        cancel_check=lambda: S.check_cancel(db.get(AgenticRun, run.id)) or _lease_lost_set(art),
        heartbeat=_heartbeat(db, run, art), workspace_run_id=_ws_id(run))
    return proposal


async def _phase_analysis(db, run: AgenticRun, art: dict, model) -> dict | None:
    """Change-Analysis pass (S2, kind='analysis'): read the code and either ask the
    PM (ask_clarifications) or propose the dual-view plan (propose_plan). Read-only.
    Re-entry inputs (PM answers / re-validation feedback) come from handoff_json."""
    handoff = run.handoff_json or {}
    # Name this pass's transcript folder after WHY we are (re-)entering analysis: a fresh
    # run, PM answers to ask_clarifications, or a reopened plan. See app.core.transcripts.
    try:
        from app.core import transcripts as _tr
        _tr.set_trigger("after_plan_reopened" if handoff.get("plan_feedback")
                        else "after_clarifications" if handoff.get("clarification_answers")
                        else "initial")
    except Exception:  # noqa: BLE001 — labelling must never break the phase
        pass
    proposal, transcript, facts = await agentic_subagents.run_analysis(
        db, run_id=run.id, ctx=art["ctx"], intent=art.get("intent", ""), model=model,
        clarification_answers=handoff.get("clarification_answers"),
        plan_feedback=handoff.get("plan_feedback"),
        resume_transcript=handoff.get("analysis_transcript"),
        facts=handoff.get("facts"),
        cancel_check=lambda: S.check_cancel(db.get(AgenticRun, run.id)) or _lease_lost_set(art),
        heartbeat=_heartbeat(db, run, art), workspace_run_id=_ws_id(run))
    # Persist this drive's transcript so the NEXT drive (PM answers / plan revision) can
    # REPLAY it instead of re-reading the codebase from turn 1. Reassign (not mutate) so
    # SQLAlchemy flags the JSON column dirty; the driver commits at the phase boundary.
    if transcript and settings.agentic_analysis_replay_transcript:
        h = dict(run.handoff_json or {})
        h["analysis_transcript"] = transcript
        run.handoff_json = h
    # Fact sheet: MERGED, not overwritten — each drive builds a fresh RunContext, so
    # drive 2's sheet holds only its own facts; a plain assignment would delete drive 1's.
    if facts:
        h = dict(run.handoff_json or {})
        h["facts"] = _merge_facts(h.get("facts"), facts)
        run.handoff_json = h
    return proposal


def _merge_facts(old: list | None, new: list | None) -> list:
    """Union of two fact sheets, deduped by fact text, ids reassigned contiguously.
    Bounded to the sheet cap so repeated drives can't grow it without limit."""
    seen: set[str] = set()
    out: list[dict] = []
    for f in (old or []) + (new or []):
        if isinstance(f, dict) and f.get("fact") and f["fact"] not in seen:
            seen.add(f["fact"])
            out.append({**f, "id": f"F{len(out) + 1}"})
            if len(out) >= 30:
                break
    return out


def _facts_block(handoff: dict | None) -> str:
    """Render the analysis pass's persisted fact sheet for a later phase's prompt.
    '' when absent — and on any error: the sheet is memory, never a blocker."""
    facts = (handoff or {}).get("facts") or []
    if not facts:
        return ""
    try:
        from app.agents.agentic_tools import format_facts
        return format_facts(facts)
    except Exception:  # noqa: BLE001
        return ""


def _scope_signals_captured(db, change_request_id: str) -> bool:
    """True once the cert scope-signal questions have been answered for this change — an
    active decision-ledger entry keyed ``scope_signal::…`` exists. Mirrors the same check
    clarification_loader.get_scope_signals uses to decode them, so a plan-first analysis run
    asks them exactly once and a clarifications-round run is never re-asked. Fail-open to
    False (ask) — a check error must never let the questions silently evaporate."""
    if db is None:
        return False
    try:
        from app.services.decision_ledger import active_entries
        from app.services.clarification_loader import SCOPE_SIGNAL_QK_PREFIX
        return any((getattr(e, "question_key", None) or "").startswith(SCOPE_SIGNAL_QK_PREFIX)
                   for e in active_entries(db, change_request_id))
    except Exception:  # noqa: BLE001 — never break analysis on the capture check
        logger.debug("scope-signal capture check failed", exc_info=True)
        return False


def _persist_change_analysis(db, run: AgenticRun, proposal: dict) -> None:
    """Write a ChangeAnalysis row from a proposed plan + index its impacted XSD paths
    (queryable rows for cross-change collision detection, S8)."""
    from app.models.change_analysis import ChangeAnalysis, ChangeImpactedPath
    existing = (db.query(ChangeAnalysis)
                .filter(ChangeAnalysis.change_request_id == run.change_request_id)
                .order_by(ChangeAnalysis.version.desc()).first())
    version = (existing.version + 1) if existing else 1
    tech = proposal.get("technical_analysis") or {}
    db.add(ChangeAnalysis(
        change_request_id=run.change_request_id, run_id=run.id, version=version,
        status="awaiting_ratification",
        technical_analysis=tech,
        functional_plan=proposal.get("functional_plan") or {},
        flow_spec=proposal.get("flow_spec") or {},
        analysis_sha=(run.handoff_json or {}).get("analysis_sha"),
    ))
    # A revised/reopened plan SUPERSEDES the prior one's impacted paths. Drop the old rows
    # before re-indexing so cross-change collision detection (which reads ALL rows for the
    # change) stops flagging files that are no longer in scope of the latest analysis.
    db.query(ChangeImpactedPath).filter(
        ChangeImpactedPath.change_request_id == run.change_request_id).delete(synchronize_session=False)
    for item in (tech.get("schema_inventory") or []):
        if isinstance(item, dict) and item.get("path"):
            db.add(ChangeImpactedPath(
                change_request_id=run.change_request_id,
                repo_id=item.get("repo") or item.get("repo_id") or "",
                path=item["path"], namespace=item.get("namespace"), kind="xsd"))
    db.commit()


async def _phase_xsd(db, run: AgenticRun, art: dict, model) -> None:
    # Apply mode: inject the human's approach choice + any requested XSD changes (refine loop).
    handoff = run.handoff_json or {}
    decision = handoff.get("approach_decision")
    cr_rec = handoff.get("xsd_change_request") or {}
    change_request = cr_rec.get("feedback")
    # Phase A consumes the ratified PLAN (context_assembler v2): the binding ledger
    # decisions PLUS the analysis plan's overview + data-model changes, so the XSD
    # agent designs to the decided reality (no TSD exists yet on the reordered flow).
    _xsd_decisions = ""
    _required_schema = ""      # .xsd/.xjb deliverables the plan REQUIRES — mandatory even under a 'reuse' approach
    _plan_schema_files: set[str] = set()   # their basenames — plan-SANCTIONED files are never a supersession
    try:
        from app.services.decision_ledger import build_decisions_block
        _xsd_decisions = build_decisions_block(run.change_request_id, db)
        # RATIFIED plan only — a newer draft rendered as "PLAN:" gave the XSD agent
        # unapproved authority (and the digest's silent [:600] slices were the BT/80
        # failure shape: the operative enum item fell past the cut).
        _ca = _latest_ratified_analysis(db, run.change_request_id)
        if _ca:
            _fp = _ca.functional_plan or {}
            _ta = _ca.technical_analysis or {}
            _plan_bits = []
            if _fp.get("overview"):
                _plan_bits.append(_clipped("PLAN", _fp["overview"], 1200))
            if _ta.get("data_model_changes"):
                _plan_bits.append(_clipped("DATA MODEL", _ta["data_model_changes"], 2400))
            if _plan_bits:
                _xsd_decisions = (_xsd_decisions + "\n\n" + "\n".join(_plan_bits)).strip()
            # The plan's CONCRETE .xsd/.xjb file list — the authoritative statement of required
            # schema. A 'reuse' approach must still apply these (reuse = no new API, not no schema
            # change); passing them explicitly stops the XSD phase from silently doing nothing.
            _sch = []
            for _p, _pf in _plan_file_entries(_ta):
                if _p.lower().endswith((".xsd", ".xjb")):
                    _sch.append(f"- {_p}: {str(_pf.get('intent') or '').strip()[:220]}")
                    _plan_schema_files.add(_p.rsplit("/", 1)[-1].strip().lower())
            _required_schema = "\n".join(_sch)
    except Exception:  # noqa: BLE001 — plan enrichment is best-effort
        pass
    # The analysis pass's fact sheet rides the decisions block: the curated, provenance-
    # tagged facts (existing value bindings, occupancy verdicts, human decisions) survive
    # the handoff at full fidelity even where the plan itself is digested down.
    _fb = _facts_block(handoff)
    if _fb:
        _xsd_decisions = (_xsd_decisions + "\n\n" + _fb).strip()
    art["xsd_scope"] = await agentic_subagents.run_xsd_discovery(
        db, run_id=run.id, ctx=art["ctx"], intent=art.get("intent", ""), model=model,
        decision=decision, change_request=change_request, decisions_block=_xsd_decisions,
        required_schema=_required_schema,
        accepted_risk=bool(cr_rec.get("accepted_risk")),
        via_revision=bool(cr_rec.get("via_revision")), workspace_run_id=_ws_id(run),
        cancel_check=lambda: S.check_cancel(db.get(AgenticRun, run.id)) or _lease_lost_set(art),
        heartbeat=_heartbeat(db, run, art))
    if art["xsd_scope"].revision_proposal:
        # The refine pass STOPPED to converse (disruptive request) — keep the change
        # request in the handoff so the chosen direction resumes with full context.
        return
    _merge_disk_created(run, art["xsd_scope"])   # git truth, not just this loop's ops
    emit_event(db, run.id, "xsd_scope", {"edits": art["xsd_scope"].edits_applied,
                                         "determinism_ok": art["xsd_scope"].determinism_ok})
    # Enum-occupancy gate: every WIRE LITERAL this round adds to a schema is git-grepped
    # against the real code BEFORE the human approves it. The motivating incident: Phase A
    # added txnPurpose "BG" while CommonConstant already bound TRANSIT_UTP_PURPOSE_CODE="BG",
    # nothing checked it, and Phase B deadlocked for a full run against the locked schema it
    # could not correct. Deterministic + advisory: the human decides, but never unknowingly.
    # Persisted to handoff so the approval UI can render it, not just the event stream.
    _eo = getattr(art["xsd_scope"], "enum_occupancy", None) or {}
    if _eo:
        _h_eo = dict(run.handoff_json or {})
        _h_eo["enum_occupancy"] = _eo
        run.handoff_json = _h_eo
        db.add(run); db.commit()
        if _eo.get("occupied"):
            _lines = "; ".join(
                f"'{o['value']}' ({o['hits']} hit(s), e.g. {(o['sample'] or ['?'])[0]})"
                for o in _eo["occupied"][:5])
            emit_event(db, run.id, "xsd_enum_occupancy",
                       {**_eo, "action": (
                           f"⚠ {len(_eo['occupied'])} new schema enum value(s) ALREADY appear in the "
                           f"code — confirm each is really free before approving: {_lines}")})
        elif _eo.get("unchecked"):
            emit_event(db, run.id, "xsd_enum_occupancy",
                       {**_eo, "action": (
                           f"⚠ {len(_eo['unchecked'])} new schema enum value(s) could NOT be "
                           "occupancy-checked (a repo failed to scan) — availability unverified")})
        else:
            emit_event(db, run.id, "xsd_enum_occupancy",
                       {**_eo, "action": (f"✓ all {_eo.get('checked', 0)} new schema enum value(s) are "
                                          "unused elsewhere in the selected repos")})
    # Empty refine round: the human requested changes but this round applied ZERO edits.
    # Legitimate only when every item was already satisfied (the guardrail demands file+line
    # evidence for that claim) — surface it instead of a bare "0 file(s), approve" review.
    # The code phase fails hard on an empty round (code_no_change); here the human gate
    # follows immediately, so this is a warning tied back to the ignored request.
    if change_request and not art["xsd_scope"].edits_applied:
        emit_event(db, run.id, "xsd_refine_no_change",
                   {"request": str(change_request)[:300],
                    "action": "⚠ Your change request produced NO schema edits this round — the agent "
                              "reported it as already satisfied, or did not act on it. Check its "
                              "explanation; if the change should have been made, re-send or rephrase "
                              "the request."})
    # Contract-coverage advisory: schema files the PLAN's change-list names but Phase A did NOT freeze —
    # exactly what the code phase would be forced to author (cbabbf9c: plan listed ApiName + 4 split XSDs,
    # Phase A froze only network-common, codegen created the 5). Deterministic + advisory; surfaced at the XSD
    # gate so a human binds them (create + approve, or confirm reuse) before codegen improvises.
    _sc = _reconcile_schema_coverage(db, run.change_request_id, art["xsd_scope"])
    if _sc.get("unfrozen"):
        emit_event(db, run.id, "xsd_schema_coverage",
                   {**_sc, "action": (f"⚠ {len(_sc['unfrozen'])} plan schema file(s) NOT frozen by Phase A — "
                                      "bind them here (create + approve, or confirm reuse) before codegen "
                                      "improvises them: " + ", ".join(_sc["unfrozen"]))})
    elif _sc.get("plan_schema"):
        emit_event(db, run.id, "xsd_schema_coverage",
                   {**_sc, "action": f"✓ all {len(_sc['plan_schema'])} plan schema file(s) frozen by Phase A"})
    # Surface refine-round concerns: a decline (genuine breakage, change NOT applied) vs an
    # on-record objection (comply-first — the request WAS applied, disagreement noted).
    for c in (art["xsd_scope"].concerns or []):
        if c.get("declined_change"):
            emit_event(db, run.id, "xsd_change_declined",
                       {"severity": c.get("severity"), "declined": c.get("declined_change"),
                        "action": f"⚠ Declined disruptive change: {c.get('message', '')[:160]}"})
        else:
            emit_event(db, run.id, "xsd_change_declined",
                       {"severity": c.get("severity"), "declined": None,
                        "action": f"ℹ Applied as requested, objection on record: {c.get('message', '')[:160]}"})
    # Comply-first coherence: a refine round that ADDED schema files while the ratified
    # approach decision says reuse/extend has SUPERSEDED the plan. Don't mutate the plan
    # silently — stash the pending update and tell the human exactly how it will roll;
    # /approve-xsd applies it, i.e. only after their approval.
    # The allowlist is unioned with the coverage advisory's own derivation: `_plan_schema_files`
    # is built inside the enrichment try above, so an exception BEFORE its loop (the decisions
    # block, the analysis query) leaves it empty — and an empty allowlist fails the wrong way,
    # making every plan-ordered schema file look like a supersession. `_sc["plan_schema"]` is the
    # same set derived under its own guard and its own query, so one failing can't disarm both.
    h = dict(run.handoff_json or {})
    # Element-level companion to `scope.created`: a new API landed INSIDE an existing
    # bundled schema (network-meta.xsd) is a file "modify", invisible to the file check.
    # Only scanned under a reuse/extend decision (the only state a supersession can
    # fire from); the plan's own message stems are sanctioned, and a failed derivation
    # fails BLIND (skip the scan), never toward a false supersession banner.
    _new_msgs: list[str] = []
    if ((h.get("approach_decision") or {}).get("approach") in ("reuse", "extend")):
        try:
            from app.models.change_analysis import ChangeAnalysis as _CA
            _ca_m = (db.query(_CA).filter(_CA.change_request_id == run.change_request_id)
                     .order_by(_CA.version.desc()).first())
            _plan_msgs = {s.lower() for s in plan_files.touched_message_stems(
                getattr(_ca_m, "technical_analysis", None) or {},
                getattr(_ca_m, "flow_spec", None) or {})}
            _new_msgs = _new_message_elements(run, plan_msgs=_plan_msgs)
        except Exception:  # noqa: BLE001
            _new_msgs = []
    _pend = _pending_plan_supersession(handoff, change_request, art["xsd_scope"],
                                       plan_schema=_plan_schema_files | set(_sc.get("plan_schema") or []),
                                       prior=h.get("pending_plan_supersession"),
                                       new_messages=_new_msgs)
    if _pend:
        h["pending_plan_supersession"] = _pend
        _added = ", ".join([p.rsplit("/", 1)[-1] for p in _pend["new_files"]]
                           + [f"{m} (in an existing schema)" for m in (_pend.get("new_messages") or [])])
        emit_event(db, run.id, "plan_supersession_pending",
                   {**_pend, "action": (
                       f"📝 Your change request supersedes the ratified '{_pend['prior_approach']}' "
                       f"approach. On approval the plan is updated to a new version: approach becomes "
                       f"NEW, adding {_added}; the 'do not create a new API' directive is lifted for "
                       "the code phase. Approving the XSDs approves this plan update.")})
    elif h.pop("pending_plan_supersession", None) is not None:
        # A standing record can now only clear for a real reason (files reverted, the plan
        # re-sanctioned them, approach no longer reuse/extend) — the earlier pending event
        # must not keep telling the PM that approval will roll a plan update it won't.
        emit_event(db, run.id, "plan_supersession_cleared",
                   {"action": "ℹ The pending plan update no longer applies — approving the "
                              "XSDs now approves the schemas only; the ratified plan is unchanged."})
    run.handoff_json = h
    # A change request is consumed once applied — clear it so the next freeze isn't stale.
    if change_request:
        h = dict(run.handoff_json or {}); h.pop("xsd_change_request", None); run.handoff_json = h


def _pending_plan_supersession(handoff: dict, change_request: str | None, scope,
                               plan_schema: set[str] | frozenset = frozenset(),
                               prior: dict | None = None,
                               new_messages: list[str] | tuple = ()) -> dict | None:
    """Comply-first refine coherence: a refine round that CREATED schema files while the
    ratified approach decision says reuse/extend has superseded the plan (the request was
    applied, per the guardrail). Returns the pending-update record for the handoff — the
    plan itself rolls only at /approve-xsd, after the human approves — or None when
    nothing diverged. Pure/deterministic: keyed off the op list, not the model's say-so.
    ``plan_schema`` is the plan's own .xsd/.xjb basenames (lowercase): a refine round that
    finally creates a file the PLAN already ordered (reuse = no new API, NOT no schema
    change — split files are legitimately new) is completing the plan, not superseding it.

    ``prior`` is the handoff's still-unapproved record from an earlier round. Attribution
    belongs to the round that CREATED each file: ``changed_files`` is HEAD-relative and
    refine rounds are not committed in between, so an earlier round's add keeps
    reappearing in ``scope.created`` — a later, unrelated request must not be re-stamped
    as its creator (the consent/audit record would name the wrong request), nor may a
    request-free later round drop a supersession the human has not yet ruled on.

    ``new_messages`` is the element-level companion (:func:`_new_message_elements`,
    already plan-filtered): a new API landed INSIDE an existing bundled schema
    (network-meta.xsd) is a file "modify", so ``scope.created`` alone would let it through
    with the reuse directive — and its rejected new-API option — still standing."""
    ad = (handoff or {}).get("approach_decision") or {}
    if ad.get("approach") not in ("reuse", "extend"):
        return None
    new_schema = [p for p in (getattr(scope, "created", None) or [])
                  if p.lower().endswith((".xsd", ".xjb"))
                  and p.rsplit("/", 1)[-1].strip().lower() not in plan_schema]
    new_msgs = [str(m) for m in (new_messages or [])]
    prior = prior if isinstance(prior, dict) else None
    prior_files = [p for p in (prior or {}).get("new_files") or [] if p in new_schema]
    prior_msgs = [m for m in (prior or {}).get("new_messages") or [] if m in new_msgs]
    fresh = [p for p in new_schema if p not in prior_files]
    fresh_msgs = [m for m in new_msgs if m not in prior_msgs]
    if prior and (prior_files or prior_msgs):
        if change_request and (fresh or fresh_msgs):   # this round added MORE unsanctioned schema
            return {**prior,
                    "requested": (str(prior.get("requested") or "")
                                  + " | " + str(change_request)[:300])[:700],
                    "new_files": (prior_files + fresh)[:12],
                    "new_messages": (prior_msgs + fresh_msgs)[:12]}
        return {**prior, "new_files": prior_files[:12], "new_messages": prior_msgs[:12]}
    if not change_request or not (fresh or fresh_msgs):
        return None
    return {"prior_approach": ad.get("approach"),
            "prior_title": (ad.get("option") or {}).get("title") or ad.get("selected_option_id") or "",
            "requested": str(change_request)[:400],
            "new_files": fresh[:12],
            "new_messages": fresh_msgs[:12]}


# Req/Resp message element declarations inside a schema file — the network bundles many
# messages per file (network-meta.xsd), so a refine that lands a NEW API there is a
# file-level "modify" and file-based supersession detection never sees it.
_XSD_MSG_ELEMENT_RE = re.compile(
    r"<\s*(?:\w+:)?element\b[^>]*?\bname\s*=\s*\"((?:Req|Resp)[A-Z][A-Za-z0-9]*)\"")


def _new_message_elements(run: AgenticRun, plan_msgs: set[str] | frozenset = frozenset()) -> list[str]:
    """Req/Resp element names ADDED to MODIFIED .xsd files since the base commit
    (current content vs ``git show HEAD:path``), minus the plan-sanctioned message
    stems — the element-level companion to file-level ``scope.created`` for the
    supersession check. Best-effort: any read failure yields [] (fail-blind toward
    the pre-existing file-only behaviour, never a false supersession banner)."""
    found: set[str] = set()
    try:
        ws = _ws_id(run)
        for rid in (run.selected_repo_ids or []):
            rd = workspace_local.repo_dir(ws, rid)
            for op, path in workspace_local.changed_files(ws, rid):
                if op != "modify" or not path.lower().endswith(".xsd"):
                    continue
                try:
                    cur = (rd / path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                res = adapter.run_command(rd, ["git", "show", f"HEAD:{path}"])
                base = res.stdout if res.ok else ""
                found |= (set(_XSD_MSG_ELEMENT_RE.findall(cur))
                          - set(_XSD_MSG_ELEMENT_RE.findall(base)))
    except Exception as e:  # noqa: BLE001
        logger.warning("new-message-element scan failed for run=%s: %s", run.id, e)
        return []
    return sorted(m for m in found if m.lower() not in plan_msgs)


def _merge_disk_created(run: AgenticRun, scope) -> None:
    """Union git's view of ADDED schema files into ``scope.created``. The agent loop's own
    op list resets every loop, so a crash-RESUMED refine round re-edits the file it created
    last time — ``create_file`` refuses an existing path, the re-edit records as "modify",
    and ``created`` comes back empty. The supersession would then go undetected while the
    frozen manifest (built from disk) DOES carry the new schema, handing Phase B an approved
    contract that contradicts its own 'do not create a new API' directive. Disk is ground
    truth for "new since the base commit". Best-effort: a read hiccup leaves the loop's view."""
    try:
        ws = _ws_id(run)
        disk = set()
        for rid in (run.selected_repo_ids or []):
            for op, path in workspace_local.changed_files(ws, rid):
                if op == "add" and path.lower().endswith((".xsd", ".xjb")):
                    disk.add(f"{rid}:{path}")
        if disk:
            scope.created = sorted(set(scope.created or []) | disk)
    except Exception as e:  # noqa: BLE001 — disk read is advisory
        logger.warning("disk-created schema read failed for run=%s: %s", run.id, e)


def _disk_change_set(db, run: AgenticRun):
    """Effective change-set from the workspace (disk = ground truth) so a CONTINUED
    or crash-RESUMED run verifies/freezes ALL edits, not just the last loop's
    in-memory ops (which reset each loop)."""
    import hashlib
    from types import SimpleNamespace
    from app.agents.agentic_tools import FileOp
    ws = _ws_id(run)
    ops = []
    for rid in (run.selected_repo_ids or []):
        try:
            rd = workspace_local.repo_dir(ws, rid)
            for op, path in workspace_local.changed_files(ws, rid):
                content = None
                if op != "delete":
                    try:
                        content = (rd / path).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        content = None
                ch = hashlib.sha256((content or "").encode()).hexdigest() if content is not None else None
                ops.append(FileOp(op=op, repo_id=rid, path=path, content=content, content_hash=ch))
        except Exception as e:  # noqa: BLE001 — disk read is best-effort
            logger.warning("disk change-set read failed for %s: %s", rid, e)
    return SimpleNamespace(operations=ops)


def _diff_stat(run: AgenticRun) -> str:
    ws = _ws_id(run)
    lines: list[str] = []
    for rid in (run.selected_repo_ids or []):
        try:
            for op, path in workspace_local.changed_files(ws, rid):
                lines.append(f"  {op:7} {path}")
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines)[:2000] or "(no changes recorded yet)"


def _disk_change_count(run: AgenticRun) -> int:
    """How many files this run actually changed on disk, across all its repos. Ground truth —
    covers continuations/retries (unlike the last batch's in-memory ops), so it answers 'did the
    code phase produce ANY change?' correctly."""
    ws = _ws_id(run)
    n = 0
    for rid in (run.selected_repo_ids or []):
        try:
            n += len(workspace_local.changed_files(ws, rid))
        except Exception:  # noqa: BLE001
            pass
    return n


def _capture_diffs(db, run: AgenticRun) -> dict:
    """STRUCTURED per-file diff artifact (INCLUDING new/untracked files) for the durable
    'changes artifact'. Scoped to the change-set (so build output / .lease are excluded).
    New files are shown via an intent-to-add (`git add -N`), then unstaged again — no
    real index mutation persists.

    Per repo: ``{"v": 2, "files": [{path, op, add, del, patch, truncated}]}``. The file
    list and its ± counts are computed from the FULL diff BEFORE any storage bounding,
    so they are exact regardless of size — bounding only ever shortens the stored
    ``patch`` preview text (flagged ``truncated``; the pushed branch holds the full
    content). This replaces the old bounded text blob, whose cap silently dropped
    files from the rendered list and produced cut-off ± counts.

    Legacy runs still hold a plain string here — readers must accept both shapes."""
    ws = _ws_id(run)
    # Sized to the real fleet: the largest source file is ~25K lines, so a FULL-file
    # rewrite diff (old + new ≈ 50K diff lines) is ~3-4MB — 4M/file holds it in full.
    # 20M total keeps a broad change-set of giant files intact before previews drop.
    # The panel lazy-renders per file and line-caps each rendered patch, so these are
    # storage/transfer sanity bounds only — and counts stay exact past them anyway.
    _FILE_PATCH_CAP, _TOTAL_PATCH_CAP = 4_000_000, 20_000_000
    out: dict[str, dict] = {}
    for rid in (run.selected_repo_ids or []):
        try:
            rd = workspace_local.repo_dir(ws, rid)
            paths = [p for _op, p in workspace_local.changed_files(ws, rid)]
            if not paths:
                continue
            # Diff vs the RECORDED base (not HEAD): the artifact must show the agent's
            # complete change even when part of it sits in a local commit (failed-push
            # leftover, agent-made commit) — changed_files above is anchored the same way.
            base = workspace_local.recorded_base(ws, rid)
            adapter.run_command(rd, ["git", "add", "-A", "-N", "--", *paths])   # show untracked as new
            res = adapter.run_command(rd, ["git", "diff", base, "--", *paths])
            adapter.run_command(rd, ["git", "reset", "-q", "--", *paths])       # undo intent-to-add
            d = (res.stdout or "").strip()
            if not d:
                continue
            files, budget = [], _TOTAL_PATCH_CAP
            for sec in re.split(r"(?m)^(?=diff --git )", d):
                if not sec.strip():
                    continue
                header = sec.split("\n", 1)[0]
                m = re.match(r"diff --git a/(.+?) b/", header)
                lines = sec.split("\n")
                added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
                removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
                op = ("add" if re.search(r"(?m)^new file mode", sec)
                      else "delete" if re.search(r"(?m)^deleted file mode", sec) else "modify")
                patch, truncated = sec, False
                if len(patch) > _FILE_PATCH_CAP:
                    patch = patch[:_FILE_PATCH_CAP] + \
                        "\n… (patch preview truncated — the branch holds the full file)\n"
                    truncated = True
                if budget - len(patch) < 0:
                    patch = f"{header}\n… (patch preview omitted — the branch holds the full file)\n"
                    truncated = True
                else:
                    budget -= len(patch)
                files.append({"path": (m.group(1) if m else "(file)"), "op": op,
                              "add": added, "del": removed, "patch": patch,
                              "truncated": truncated})
            out[rid] = {"v": 2, "files": files}
            logger.info(
                "diff_capture: ws=%s repo=%s files=%d (+%d/−%d) bounded_previews=%d bytes=%d",
                ws, rid, len(files), sum(f["add"] for f in files),
                sum(f["del"] for f in files), sum(1 for f in files if f["truncated"]),
                sum(len(f["patch"]) for f in files))
        except Exception as e:  # noqa: BLE001 — artifact capture is best-effort
            logger.warning("diff capture failed for %s: %s", rid, e)
    return out


def _format_plan(plan: dict | None) -> str:
    if not plan:
        return ""
    parts = [(plan.get("summary") or "").strip()]
    if plan.get("files"):
        parts.append("Planned files: " + ", ".join(str(f) for f in plan["files"]))
    return "\n".join(p for p in parts if p)[:2500]


def critical_directives(ta: dict | None) -> list[str]:
    """The plan's critical-decision directives as numbered '[D<n>] <dimension>: <directive>'
    strings — the binding contract the code phase must obey and the reviewer must verdict
    on, one by one. Empty on legacy plans without the block. Never raises."""
    out: list[str] = []
    try:
        for cd in ((ta or {}).get("critical_decisions") or []):
            if isinstance(cd, dict) and (cd.get("directive") or cd.get("decision")):
                # 1200, not 400: these are BINDING contract items the reviewer verdicts one by
                # one — an XSD cardinality rule or error-code table clipped at 400 chars had its
                # operative detail cut, and the reviewer then verdicted a different directive.
                out.append(f"[D{len(out) + 1}] {cd.get('dimension', '?')}: "
                           f"{str(cd.get('directive') or cd.get('decision'))[:1200]}")
    except Exception:  # noqa: BLE001 — plan enrichment must never break the run
        return []
    return out[:20]


def _clipped(label: str, value, cap: int) -> str:
    """Render ``label: value`` bounded at ``cap`` chars — LOUDLY. A silent ``[:N]`` slice
    here is the BT/80 failure shape: the operative detail falls past the cut and the agent
    substitutes a plausible value with no way to know anything is missing. The marker
    states how much was cut and where the full text lives."""
    s = str(value)
    if len(s) <= cap:
        return f"{label}: {s}"
    return (f"{label}: {s[:cap]}\n  ⚠ [{label} CLIPPED — {len(s) - cap} of {len(s)} chars "
            "omitted; fetch the full section with read_doc(doc='plan')]")


# Whole-plan render budget. Structural: whole trailing parts are dropped WITH a note naming
# them (never a mid-part character cut), and every dropped/clipped piece points at
# read_doc(doc='plan') for full fidelity.
_PLAN_RENDER_BUDGET = 14_000


def _render_analysis_plan(ta: dict | None, fp: dict | None, flow: dict | None) -> str:
    """Render the ratified Change-Analysis plan as Phase B's BINDING implementation spec.
    Bounded but LOUD: every clip is marked, whole-part drops are named, and the per-file
    deliverables (the plan's concrete work list) are always rendered. Empty string when
    there's nothing to render."""
    ta, fp, flow = ta or {}, fp or {}, flow or {}
    parts: list[str] = []
    # BINDING DIRECTIVES first — the critical decisions (settlement/money legs/atomicity/…)
    # are the contract the implementer may NOT re-decide. Rendering them at the very top is
    # the fix for the observed drift class: a ratified 'per-participant credit' living only
    # in assumptions was silently re-decided into a consolidated credit two runs in a row.
    _dirs = critical_directives(ta)
    if _dirs:
        parts.append("BINDING DIRECTIVES (ratified critical decisions — implement EXACTLY; "
                     "never re-decide; raise a decision question if one conflicts with code "
                     "reality):\n" + "\n".join(_dirs))
    # A human approach-gate decision (possibly diverging from the pre-gate recommendation) is
    # BINDING and authoritative — render it FIRST so Phase B builds the chosen direction, not the
    # superseded recommendation that may still sit in data_model_changes below.
    ad = ta.get("approach_decision")
    if isinstance(ad, dict) and ad.get("approach"):
        line = ("APPROACH DECISION (human-chosen at the gate — BINDING, supersedes the pre-gate "
                f"recommendation): {ad.get('chosen_title') or ad.get('chosen_option_id') or '?'} "
                f"[{ad.get('approach')}]")
        if ad.get("diverges_from_plan") and ad.get("why"):
            line += f" — diverges from the original plan: {str(ad['why'])[:400]}"
        parts.append(line)
    # The PM's rectifications are BINDING functional choices (repercussions accepted at
    # ratification) — rendered right after the approach decision so Phase A/B and the
    # reviewer treat them as contract, not as suggestions the agent may re-decide.
    _rects = [r for r in (ta.get("user_rectifications") or []) if isinstance(r, dict)]
    if _rects:
        lines = [f"- {str(r.get('requested', ''))[:200]} → {str(r.get('applied', ''))[:200]}"
                 + (f" (repercussions: {str(r['repercussions'])[:200]})" if r.get("repercussions") else "")
                 for r in _rects[:6]]
        parts.append("USER RECTIFICATIONS (binding functional choices — implement EXACTLY; "
                     "repercussions were accepted at ratification):\n" + "\n".join(lines))
    if fp.get("overview"):
        parts.append(_clipped("OVERVIEW", fp["overview"], 2000))
    steps = flow.get("steps") or flow.get("flow")
    if steps:
        parts.append(_clipped("FLOW STEPS", steps, 1600))
    # The flow structure the implementer must honour — actors, state machine, message shapes —
    # not just the linear steps, so state transitions / actor wiring get built correctly.
    if flow.get("actors"):
        parts.append(_clipped("FLOW ACTORS", flow["actors"], 600))
    if flow.get("states"):
        parts.append(_clipped("STATE MACHINE", flow["states"], 1000))
    if flow.get("messages"):
        parts.append(_clipped("MESSAGES", flow["messages"], 800))
    if ta.get("data_model_changes"):
        parts.append(_clipped("DATA MODEL CHANGES", ta["data_model_changes"], 2400))
    # The plan's CONCRETE per-file work list — the deliverables Phase B is judged against.
    # Omitting these (the pre-2026-08 render did) reduced "implement the plan in full" to a
    # digest with no file-level contract.
    pfe = _plan_file_entries(ta)
    if pfe:
        rows = [f"  - {p} — {str(pf.get('intent') or pf.get('change') or '').strip()[:300]}"
                for p, pf in pfe[:40]]
        if len(pfe) > 40:
            rows.append(f"  … +{len(pfe) - 40} more planned files — read_doc(doc='plan') lists all")
        parts.append("PLANNED FILE CHANGES (deliverables — cover every one):\n" + "\n".join(rows))
    inv = [f"{i.get('repo', '')}:{i.get('path', '')}" for i in (ta.get("schema_inventory") or [])
           if isinstance(i, dict) and i.get("path")]
    if inv:
        parts.append("SCHEMA FILES: " + ", ".join(inv[:30])
                     + (f" … +{len(inv) - 30} more (read_doc(doc='plan'))" if len(inv) > 30 else ""))
    for key, label in (("modules", "MODULES"), ("flows", "FLOWS"), ("reuse_findings", "REUSE"),
                       ("constraints", "CONSTRAINTS"), ("risks", "RISKS (implement with guardrails)")):
        if ta.get(key):
            parts.append(_clipped(label, ta[key], 800))
    if fp.get("assumptions"):
        parts.append(_clipped("RATIFIED ASSUMPTIONS (honour these; do not re-decide)",
                              fp["assumptions"], 1200))
    # Structural budget: drop whole TRAILING parts (never a mid-part character cut) and NAME
    # what was dropped — a bare join()[:5000] severed directives mid-sentence with no marker.
    out: list[str] = []
    used = 0
    dropped: list[str] = []
    for part in parts:
        if out and used + len(part) + 1 > _PLAN_RENDER_BUDGET:
            dropped.append(part.split(":", 1)[0].split("\n", 1)[0][:48])
            continue
        out.append(part)
        used += len(part) + 1
    if dropped:
        out.append("⚠ PLAN SECTIONS OMITTED for size: " + ", ".join(dropped)
                   + " — fetch the full ratified plan with read_doc(doc='plan').")
    return "\n".join(out)


def _latest_ratified_analysis(db, change_request_id: str):
    """Newest RATIFIED ChangeAnalysis row — the only version that may be rendered as a
    binding/ratified plan. Returns None rather than falling back to a newer draft: a
    draft or awaiting-ratification version labeled "ratified/BINDING" hands the phases
    unapproved authority (the ratification endpoint sets status="ratified")."""
    from app.models.change_analysis import ChangeAnalysis
    return (db.query(ChangeAnalysis)
            .filter(ChangeAnalysis.change_request_id == change_request_id,
                    ChangeAnalysis.status == "ratified")
            .order_by(ChangeAnalysis.version.desc()).first())


def _analysis_plan_block(db, change_request_id: str) -> str:
    """The latest ratified Change-Analysis plan, rendered as Phase B's binding spec. Empty
    on legacy runs with no analysis (Phase B then falls back to intent + decisions, as before)
    and on changes whose analysis was never ratified — never renders a draft as binding.
    Best-effort — plan enrichment must never break the run."""
    if db is None:
        return ""
    try:
        ca = _latest_ratified_analysis(db, change_request_id)
        return _render_analysis_plan(ca.technical_analysis, ca.functional_plan, ca.flow_spec) if ca else ""
    except Exception:  # noqa: BLE001 — best-effort
        return ""


def _latest_tsd(db, change_request_id: str):
    """The latest generated TSD row for this change — the doc↔code gate audits its content against the
    frozen diff and (on a doc over-claim) reconciles it in place. Returns the ORM row or None.
    Best-effort — the gate must never break the run."""
    if db is None:
        return None
    try:
        from app.models.tech_spec import TechSpec
        return (db.query(TechSpec)
                .filter(TechSpec.change_request_id == change_request_id)
                .order_by(TechSpec.version.desc()).first())
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _tsd_approval_gate(db, run: AgenticRun) -> bool:
    """ADR-0005 / SDLC review gap 4 — the TSD binding-contract gate. Called ONLY at a
    CODE_CHANGE entry point (never on the code-fix retry loop's re-entries, which
    already passed this gate once for the run). Returns True iff the run may proceed
    to CODE_CHANGE this call — False means the caller must route to
    AWAITING_TSD_APPROVAL instead. Always emits a `tsd_approval_gate` telemetry event
    so the shadow rollout (agentic_tsd_approval_gate=True,
    agentic_tsd_approval_gate_enforce=False) can be measured before it blocks
    anything. Fail-open: any error resolving the TSD is treated as "approved" —
    a checker fault must never wedge every run in the platform."""
    if not getattr(settings, "agentic_tsd_approval_gate", False) or db is None:
        return True
    try:
        from app.models.research import ArtifactStatus
        ts = _latest_tsd(db, run.change_request_id)
        approved = bool(ts is not None and ts.status == ArtifactStatus.APPROVED)
        enforce = bool(getattr(settings, "agentic_tsd_approval_gate_enforce", False))
        emit_event(db, run.id, "tsd_approval_gate",
                   {"approved": approved, "enforce": enforce,
                    "tsd_version": getattr(ts, "version", None), "tsd_status": (ts.status.value if ts else None),
                    "action": ("✅ TSD approved — proceeding to code generation" if approved else
                               ("🔒 TSD not approved — blocking code generation" if enforce else
                                "⚠ TSD not approved (shadow — not blocking yet)"))})
        if approved and ts is not None:
            # Lock the version NOW, at the moment the gate passes — every subsequent
            # read_doc(doc='tsd') and context re-assembly for this run resolves against
            # THIS version, even if the TSD is later regenerated mid-run.
            run.tsd_version_locked = ts.version
            if db is not None:
                db.add(run)
        if not enforce:
            return True                      # shadow mode: measure only, never block
        return approved
    except Exception as e:  # noqa: BLE001 — fail-open: a checker fault must not wedge every run
        logger.warning("tsd_approval_gate check failed for run=%s (%s) — failing open (approved)", run.id, e)
        return True


# The plan file list lives in app.agents.plan_files so the orchestrator, the plan audit and
# TSD generation all read the same key/field variants (see that module's docstring).
_PLAN_FILE_KEYS = plan_files.PLAN_FILE_KEYS
_plan_file_entries = plan_files.plan_file_entries


def _planned_files(db, change_request_id: str, *, exclude_schema: bool = False) -> list[dict]:
    """The RATIFIED plan's planned files as ``[{path, intent}]`` — the source of truth for the
    plan-fidelity coverage check (distinct from the agent's OWN submitted plan, which can itself
    drop a file). Empty on legacy/no-analysis runs. Best-effort.

    The analysis agent's schema drifted across versions: the file list lives under ANY of
    ``files_to_modify | files_to_change | per_file_changes | per_file_change_list |
    file_change_list``. Reading only one key silently fed coverage an empty list — that is
    exactly how a run once dropped 13 planned files yet reported 0 missing (change 989aee7a),
    and how ``file_change_list`` (seen in the wild 2026-07-22, change 215ead25) disarmed the
    required-schema guard. Read whichever key is populated.

    ``exclude_schema`` drops .xsd/.xjb entries — used for Phase-B (code) runs, where the schema is
    the approved Phase-A baseline and NOT the code run's responsibility to (re)touch."""
    if db is None:
        return []
    try:
        ca = _latest_ratified_analysis(db, change_request_id)   # coverage is judged vs the RATIFIED plan
        if not ca:
            return []
        out = []
        for p, pf in _plan_file_entries(ca.technical_analysis):
            if exclude_schema and p.lower().endswith((".xsd", ".xjb")):
                continue   # schema is Phase A's domain; a code run must not be graded on it
            out.append({"path": p, "intent": str(pf.get("intent") or "")})
        return out
    except Exception:  # noqa: BLE001 — best-effort
        return []


def _new_api_flow_gaps(ta: dict | None, flow: dict | None) -> list[str]:
    """Message schemas the plan INTRODUCES whose four-party route is ABSENT from flow_spec.
    Deterministic + advisory: each planned Req*/Resp* .xsd stem must appear as an EXACT
    token in flow_spec's ROUTE fields (steps/messages/actors) — token equality, not
    substring, so an unrouted ReqTransfer is not masked by a routed ReqTransferVerify, and a prose
    mention in an overview field does not count as a route. Entries that EXTEND an existing
    message are skipped: the plan agent is told to spell out a route only for a NEW
    API/message, so demanding one for an already-routed ReqTransfer is a false alarm that costs
    the PM a needless reopen. Pure; fail-open."""
    import json
    try:
        stems = []
        for p, _pf in _plan_file_entries(ta):
            b = p.rsplit("/", 1)[-1]
            if (b.lower().endswith(".xsd") and b.lower().startswith(("req", "resp"))
                    and plan_files.entry_adds_file(_pf)):
                stems.append(b[:-4])
        if not stems:
            return []
        route = {k: (flow or {}).get(k) for k in ("steps", "flow", "messages", "actors")}
        tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9_]+", json.dumps(route, default=str))}
        return [s for s in stems if s.lower() not in tokens]
    except Exception:  # noqa: BLE001 — advisory; never break plan persistence
        return []


def _party_flow_gaps(ta: dict | None, flow: dict | None) -> dict:
    """Party-flow coverage audit (deterministic, advisory) — the companion to
    :func:`_new_api_flow_gaps` for EVERY touched message, existing or new. The plan
    agent is told to put a ``party_flows`` entry (parties + evidence-cited hops) in
    flow_spec for each Req*/Resp* schema the plan touches. This audit is plain code,
    so it cannot hallucinate and cannot be argued with:

      * ``missing``      — touched message schemas with no party_flows entry at all
      * ``unevidenced``  — hops carrying NO code/doc evidence yet not marked assumed
      * ``assumed``      — hops the agent explicitly could not confirm (the honest
                           unknowns — clarification candidates, never blockers)

    A change that touches no message schema stays silent: an internal change has no
    party flow to state, and a fully-confirmed flow produces no findings and no
    questions. Detection is by message TOKEN (plan_files.touched_message_stems), not
    Req/Resp file names — messages usually live inside combined schema files
    (network-meta.xsd), which a file-stem check silently misses. Pure; fail-open."""
    try:
        stems = plan_files.touched_message_stems(ta, flow)
        entries = [e for e in ((flow or {}).get("party_flows") or []) if isinstance(e, dict)]
        # SAME coverage definition as the propose_plan gate (api + hop messages) — an
        # api-only check here re-flagged business-named flows the gate had just accepted.
        covered = plan_files.party_flow_covered_tokens(flow)
        missing = [s for s in stems if s.lower() not in covered]
        unevidenced: list[str] = []
        assumed: list[str] = []
        for e in entries:
            api = str(e.get("api") or "?")
            hops = [h for h in (e.get("hops") or []) if isinstance(h, dict)]
            if not hops:
                unevidenced.append(f"{api}: no hops stated")
                continue
            for h in hops:
                leg = f"{api}: {h.get('from', '?')}→{h.get('to', '?')}"
                ev = h.get("evidence")
                has_evidence = bool(ev if isinstance(ev, (list, dict)) else str(ev or "").strip())
                if str(h.get("confidence") or "").strip().lower() == "assumed":
                    assumed.append(leg)
                elif not has_evidence:
                    unevidenced.append(leg)
        return {"missing": sorted(set(missing)), "unevidenced": unevidenced[:20],
                "assumed": assumed[:20]}
    except Exception:  # noqa: BLE001 — advisory; never break plan persistence
        return {"missing": [], "unevidenced": [], "assumed": []}


def _success_criteria(db, change_request_id: str) -> str:
    """The PM's success criteria (original ask + the plan's functional steps) so the behavioural
    fidelity check can verify the diff delivers what the PM expects. Best-effort."""
    if db is None:
        return ""
    parts: list[str] = []
    try:
        from app.models.change_request import ChangeRequest
        cr = db.get(ChangeRequest, change_request_id)
        if cr and getattr(cr, "initial_prompt", None):
            # 6000 + an explicit marker (was a silent [:1200]): this is the PM's ask that the
            # behavioural fidelity check verifies the diff against — requirements past the
            # cut were simply never checked.
            _ip = cr.initial_prompt
            parts.append(_ip if len(_ip) <= 6000 else _ip[:6000] + "\n…[initial prompt truncated]")
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.models.change_analysis import ChangeAnalysis
        ca = (db.query(ChangeAnalysis)
              .filter(ChangeAnalysis.change_request_id == change_request_id)
              .order_by(ChangeAnalysis.version.desc()).first())
        if ca and (ca.functional_plan or {}).get("steps"):
            parts.append("Planned steps: " + str(ca.functional_plan["steps"])[:800])
    except Exception:  # noqa: BLE001
        pass
    return "\n\n".join(parts)


def _diff_summary_for_fidelity(cs) -> str:
    """Compact per-file diff summary (op + path + produced content, truncated per file, ~18k cap)
    for the behavioural fidelity check so it can spot faked/stubbed logic, not just missing files."""
    ops = getattr(cs, "operations", None) or []
    chunks: list[str] = []
    budget = 18000
    for o in ops:
        head = f"\n### {str(o.op).upper()} {o.path}\n"
        body = (getattr(o, "content", None) or "")[:3000]
        chunk = head + body
        if budget - len(chunk) < 0:
            chunks.append(head + "[...truncated...]")
            break
        budget -= len(chunk)
        chunks.append(chunk)
    return "".join(chunks)


def _fidelity_diff_summary(run: AgenticRun, cs) -> str:
    """Diff the behavioural plan-fidelity judge sees. Prefer the REAL git diff (the actual changed
    hunks) so an edit deep inside a large file is visible: the per-file content-head summary
    (``_diff_summary_for_fidelity``) truncates each file to its first ~3k chars, which hid mid-file
    edits and made the judge flag PHANTOM 'missing behaviour' gaps for work that WAS present further
    down the file — escalating correct changes to the human gate. Fail-open to the content summary
    when the diff can't be rendered (resumed run whose clone was GC'd, or the flag is off)."""
    if getattr(settings, "agentic_fidelity_real_diff", True):
        try:
            diff = agentic_review._render_diff(_ws_id(run), cs, cap=None)   # pure/deterministic consumers grep the FULL change; workspace owner, not run.id (Phase-B adopts parent's clone)
            if diff and diff.strip():
                return diff
        except Exception as e:  # noqa: BLE001 — advisory gate must never break on diff rendering
            logger.debug("fidelity gate: real-diff render failed, using content summary (%s)", e)
    return _diff_summary_for_fidelity(cs)


# High-precision "this code is a stub, not an implementation" markers (low false-positive set).
_STUB_RE = re.compile(r"\bTODO\b|\bFIXME\b|not[\s_]?implemented|UnsupportedOperationException", re.I)


def _plan_gap_feedback(cs) -> str:
    """If the code agent declared DONE but the change is INCOMPLETE — a planned file left
    untouched, or a TODO/FIXME/stub/UnsupportedOperationException placeholder left in the produced
    code — return a 'finish it' string (else ''). Wires diff_stats_gate (file coverage) + a
    placeholder scan; this is what stops the agent freezing a half-baked change."""
    ops = cs.operations or []
    # Placeholder/stub markers in the produced code = not done.
    stubs: list[str] = []
    for o in ops:
        content = getattr(o, "content", None)
        if o.op == "delete" or not content:
            continue
        for ln in content.splitlines():
            if _STUB_RE.search(ln):
                stubs.append(f"{o.path}: {ln.strip()[:80]}")
                break
    # File coverage against the agent's own submitted plan.
    missing: list[str] = []
    plan = cs.plan or {}
    if plan.get("files"):
        try:
            from app.agents.diff_stats_gate import check_diff_stats
            _ACT = {"add": "create", "modify": "modify", "delete": "delete"}
            files_changed = [{"path": o.path, "action": _ACT.get(o.op, o.op)} for o in ops]
            missing = [f["path"] for f in check_diff_stats(files_changed, plan)
                       if f.get("kind") == "missing_planned_file"]
        except Exception:  # noqa: BLE001 — gate is advisory
            missing = []
    if not missing and not stubs:
        return ""
    parts = []
    if missing:
        parts.append("planned files you have NOT changed: " + ", ".join(missing[:20]))
    if stubs:
        parts.append("unfinished placeholders left in your code (TODO/FIXME/stub/UnsupportedOperationException): "
                     + "; ".join(stubs[:10]))
    return ("Your change is NOT complete — " + " | ".join(parts) + ". Finish every item now; if "
            "something is genuinely unnecessary, edit another planned file or state why — do not "
            "stop with the plan half-done.")


def _basename(p: str | None) -> str:
    """Lowercased file basename — robust to plan-vs-diff path-prefix differences (the reviewer
    names a full repo path, the disk change-set is repo-relative). Mirrors the coarse matching
    plan_fidelity already relies on."""
    return (p or "").replace("\\", "/").rstrip("/").split("/")[-1].lower()


def _untouched_review_files(flagged: list[str], touched: list[str]) -> list[str]:
    """Reviewer-flagged files (by basename) that the change never touched. Pure set logic so it is
    unit-testable; the caller feeds it disk truth. Empty entries (behavioural findings with no file)
    are ignored — file-touch can't check those. File-touch is a deterministic FLOOR, not proof the
    fix is correct (the re-review remains the authority); it exists only to stop the agent declaring
    'done' while a flagged file was never even opened — the 1-3-edits-then-stop dribble."""
    tset = {_basename(t) for t in (touched or []) if t}
    seen: set[str] = set()
    out: list[str] = []
    for f in (flagged or []):
        b = _basename(f)
        if not b or b in seen or b in tset:
            continue
        seen.add(b)
        out.append(b)
    return out


def _review_feedback_errors(items: list[dict], notes: list[dict]) -> list[str]:
    """Flatten the reviewer verdict into the COMPLETE fix list handed back to the code agent: every
    must-fix item (blockers + plan-fidelity gaps) AND every advisory note. Handing back ONLY the
    blockers (the old behaviour) left the non-blocking findings unfixed — so they re-surfaced and
    were re-counted every round, which is the bulk of why the gap total drifted UP instead of
    converging. One pass that clears the whole list is what lets the loop terminate. Blockers first
    (items are pre-sorted must-block-first) so the prompt's cap can never drop one."""
    def _fmt(i: dict, advisory: bool) -> str:
        tag = "[advisory] " if advisory else ""
        head = f"{tag}{(i.get('file') or '?')}:{i.get('line') or '?'} [{i.get('category')}] {i.get('why')}"
        return (head + (f" → fix: {i['suggested_fix']}" if i.get("suggested_fix") else "")
                + (f" → done when: {i['done_when']}" if i.get("done_when") else ""))
    # reviewer_gap items are verdict deficiencies (unverified directives, unparseable output)
    # — the code agent cannot fix the reviewer, so handing them over burns rounds on phantom
    # work (215ead25: two full rounds). They stay in `items` for the push gate + human only.
    return ([_fmt(i, False) for i in (items or []) if not i.get("reviewer_gap")]
            + [_fmt(n, True) for n in (notes or [])])


async def _phase_code(db, run: AgenticRun, art: dict, model) -> None:
    # On a verification-failure RETRY (re-entry via _step), hand the failing gates back
    # so the agent diagnoses (run_command) before re-editing (§9.3).
    feedback = art.get("verification") if (art.get("verification") or {}).get("status") == "needs_fix" else None
    # Anti-oscillation memory: hand the agent the signatures of EARLIER failed attempts (not just
    # the latest) so it doesn't re-apply a fix that already regressed. attempt_log lives on `art`,
    # which persists across the in-process code→verify→code retry loop.
    if feedback:
        prior = (art.get("attempt_log") or [])[:-1]      # exclude the current (latest) failure
        if prior:
            feedback = {**feedback, "history": prior}
    # Review loop-back: verification PASSED but the reviewer BLOCKED (half-baked / plan not met).
    # Previously the agent re-entered with NOTHING and re-looped the same diff. Hand it the actual
    # blocking findings so it fixes them. (Verify-feedback takes precedence — fix the build first.)
    if not feedback:
        rv = art.get("review") or {}
        if rv.get("blocking") and (rv.get("items") or rv.get("notes")):
            _errs = _review_feedback_errors(rv.get("items") or [], rv.get("notes") or [])
            if rv.get("dropped_must_block"):
                # Findings past the 15-item cap are persisted and re-checked next round;
                # without this line the capped list reads as the COMPLETE fix list and
                # the overflow resurfaces as "new" findings — incremental discovery.
                _errs.append(f"…plus {rv['dropped_must_block']} MORE blocking finding(s) beyond "
                             "the item cap, not listed here — they are persisted and will be "
                             "re-checked next round; none is resolved by omission.")
            if art.get("acceptance_feedback"):
                # The deterministic DEFINITION-OF-DONE block for unmet structural
                # predicates — carries the exact [unmet: …] evidence the one-line item
                # rendering drops. Was write-only before (set but never delivered).
                _errs.append(art["acceptance_feedback"])
            feedback = {"gates": {"review": False}, "errors": _errs, "source": "review"}
    # P2 — strategist stage: after N failed fix rounds, another "fix the errors" round is the
    # approach that already failed. Ask a one-shot strategist for ONE structural change and
    # attach it to the feedback (rendered as a binding change-of-approach directive). Fires on
    # every attempt at/past the threshold; fail-open (advice="" changes nothing).
    _strat_after = getattr(settings, "agentic_strategist_after_attempts", 0)
    if feedback and _strat_after:
        _n_att = (run.attempts_json or {}).get("code_change", 0)
        if _n_att >= _strat_after:
            try:
                from app.agents.strategist import structural_advice
                advice = await structural_advice(
                    plan_summary=_analysis_plan_block(db, run.change_request_id) or art.get("intent", ""),
                    attempts=_n_att, error_history=art.get("attempt_log") or [],
                    diff_stat=_diff_stat(run))
                if advice:
                    feedback = {**feedback, "strategy": advice}
                    # M3: signal the goal-verifier stall guard to relax (threshold 2 → 5) while this
                    # restructure is in flight — cleared in _phase_review_goal_verifier once it makes
                    # progress. Persisted in the durable ledger so it survives the phase hop + resume.
                    _sled = dict(getattr(run, "progress_ledger_json", None) or {})
                    _sled["gv_strategist_active"] = True
                    run.progress_ledger_json = _sled
                    emit_event(db, run.id, "strategist",
                               {"attempts": _n_att, "detail": advice[:400],
                                "action": "🧭 Strategist: recommending a structural change of approach"})
            except Exception as e:  # noqa: BLE001 — advisory; the fix round proceeds regardless
                logger.debug("strategist skipped: %s", e)
    # Did THIS code phase enter to fix reviewer findings? Captured before the loop — it gates the
    # review-completeness floor below.
    _review_fix = bool(feedback and feedback.get("source") == "review")
    # Author-memory continuity: a fix round CONTINUES the conversation that wrote the code (the
    # prior round's transcript, kept on `art` across the in-process code→verify→review loop) —
    # instead of a fresh agent re-deriving everything from a string summary. In-process only;
    # a crash-resume starts fresh (legacy behaviour).
    resume = art.get("code_transcript") if feedback else None
    # Decision memory: the approach the human chose at the reuse gate (chosen + rejected +
    # directive). Lives on this run's handoff (full) or the Phase-A parent's (code), so
    # Phase B implements exactly the chosen path and can't drift to a rejected approach.
    approach = (getattr(run, "handoff_json", None) or {}).get("approach_decision")
    parent_id = getattr(run, "parent_run_id", None)
    if not approach and parent_id and db is not None:
        parent = db.get(AgenticRun, parent_id)
        approach = (getattr(parent, "handoff_json", None) or {}).get("approach_decision") if parent else None
    continuation = None
    max_cont = settings.agentic_max_code_continuations
    read_acc: set = set()            # MEMORY across batches: files already explored
    intel_acc: set = set()           # SDLC gaps 7/8/9/11 — structural-intel tokens across ALL rounds
    plan_memory = ""                 # MEMORY: the agent's own plan
    # RESUME WITH MEMORY (§3): a crash / cancel / API-key failure kills the worker, and the
    # code agent's conversation + plan + read-set (kept only on the in-memory `art`) die with
    # it — so a fresh resume re-reads the codebase from turn 1. Unlike the analysis phase, the
    # code transcript was never persisted. We durably persist a lightweight resume-state
    # (plan + read-set) to handoff_json each round (below), and here — on a resume with no
    # in-process transcript/continuation but real edits already on disk — reconstruct the
    # CONTINUATION from it + the disk diff, so the agent picks up from its plan instead of
    # rediscovering everything. Full-transcript replay is deliberately avoided (it's large and
    # would bloat every round's DB write); the diff + plan + read-set is what stops re-exploration.
    _code_resume = (getattr(run, "handoff_json", None) or {}).get("code_resume")
    if resume is None and _code_resume and _disk_change_count(run) > 0:
        read_acc = {tuple(x) for x in (_code_resume.get("read_files") or []) if len(x) > 1}
        intel_acc = set(_code_resume.get("intel_queried") or [])
        plan_memory = _code_resume.get("plan") or ""
        _read_list = "\n".join(f"  - {p}" for p in sorted({x[1] for x in read_acc}))
        continuation = {
            "round": _code_resume.get("round", 2), "diff_stat": _diff_stat(run),
            "plan": plan_memory,
            "read": _read_list[:6000] + ("\n  … (read-list clipped — it is INCOMPLETE)"
                                         if len(_read_list) > 6000 else ""),
            "notes": ("Resumed after an interruption (cancel / API error / restart). Your prior "
                      "edits are ALREADY on disk (shown above as DONE) and the files you already "
                      "explored are listed. These lists are a compact CHECKPOINT, not full memory — "
                      "they may be incomplete. Continue from your plan and make the remaining "
                      "edits; avoid re-exploring wholesale, but whenever you are unsure what a "
                      "file contains NOW, re-read it: the working tree is ground truth, your "
                      "recollection is not."),
        }
        emit_event(db, run.id, "code_resumed",
                   {"round": continuation["round"], "read_files": len(read_acc),
                    "action": "⏩ Resuming code generation WITH memory — prior edits + plan + "
                              "read-set restored; not re-exploring from scratch"})
    gap_redrives = 0                 # bounded: drive completion rounds if the plan isn't done
    review_redrives = 0              # bounded: drive completion rounds when reviewer-flagged files stay untouched
    no_progress_rounds = 0           # consecutive CAPPED rounds with zero file edits (abort at 2)
    cs = None
    # Binding Decision Ledger (S7) — same for every continuation round.
    try:
        from app.services.decision_ledger import build_decisions_block
        _decisions_block = build_decisions_block(run.change_request_id, db)
    except Exception:  # noqa: BLE001
        _decisions_block = ""
    # The ratified Change-Analysis plan IS Phase B's spec (the plan the human approved must be
    # the one implemented — not re-derived from BRD/TSD). Empty on legacy runs → prior behaviour.
    _plan_block = _analysis_plan_block(db, run.change_request_id)
    # P1 — in-loop convergence nudge: pre-extract the acceptance predicates (they need only the
    # ratified plan, available now) so the TOOL LOOP can check completeness the moment the agent
    # declares done and nudge it with the unmet items — instead of waiting for this orchestrator's
    # post-round gap check to burn a full continuation round. The post-round check below still
    # runs (backstop, and it finds the predicates already cached). Fail-open throughout.
    _converge_check = None
    if getattr(settings, "agentic_acceptance_predicates", False) and _plan_block:
        try:
            from app.agents import acceptance_predicates as AP
            if art.get("acceptance_predicates") is None:
                art["acceptance_predicates"] = await AP.extract_predicates(_plan_block)

            def _converge_check():
                try:
                    preds = art.get("acceptance_predicates") or []
                    if not preds:
                        return None
                    dcs = _disk_change_set(db, run)
                    diff_text = _fidelity_diff_summary(run, dcs)
                    # Parser sanity gate: real edits exist but the diff parsed to ZERO files →
                    # the diff is in a shape parse_diff can't read, so every predicate would
                    # false-unmet and the nudge would imperatively demand phantom work
                    # (observed risk: the agent injects tokens to satisfy a blind check).
                    if getattr(dcs, "operations", None) and not AP.parse_diff(diff_text):
                        return None
                    miss = AP.unmet(AP.check_predicates(preds, diff_text))
                    # Bare-token predicates are naming-sensitive (a correct implementation
                    # under a different name false-unmets) — advisory here: they never drive
                    # the in-loop nudge. The post-round gap check still reports them.
                    hard = [r for r in miss if not AP.is_bare_token(r.predicate)]
                    return AP.feedback_block(hard) if hard else None
                except Exception:  # noqa: BLE001 — a check error must never block the stop
                    return None
        except Exception as e:  # noqa: BLE001
            logger.debug("convergence-check setup failed (%s) — post-round gap check remains", e)

    # SDLC-A22 (authoring half) — extract the APPROVED TSD's checkable assertions
    # ONCE per phase and hand them to every code round, so the agent knows which
    # assertions the post-codegen `tsd_test_coverage_gate` will grade it against
    # and which exact `tsd-ref` marker to emit for each. Previously the agent was
    # asked for markers but never shown the list, so it guessed section strings
    # that failed to match — coverage measured luck, not test quality, and the
    # gate could never responsibly be enforced.
    #
    # Extracted here (before the round loop) rather than per round: it is one LLM
    # call, and the approved TSD does not change mid-phase. Reuses the same
    # `_latest_tsd` + settings gate as the coverage check itself, so the agent is
    # never graded against a list that was not also offered to it.
    _tsd_assertions: list[dict] = []
    if getattr(settings, "agentic_tsd_test_coverage_gate", True):
        try:
            from app.agents import tsd_test_generator as _TTG
            _tsd_doc = _latest_tsd(db, run.change_request_id)
            _tsd_text = (getattr(_tsd_doc, "content", None) or "") if _tsd_doc else ""
            if _tsd_text.strip():
                _tsd_assertions = await _TTG.extract_tsd_assertions(_tsd_text)
                if _tsd_assertions:
                    # Stash on `art` so the post-codegen coverage gate grades the
                    # agent against the SAME list it was shown, and does not pay
                    # for a second extraction call.
                    art["tsd_assertions"] = _tsd_assertions
                    emit_event(db, run.id, "tsd_assertions_extracted",
                               {"count": len(_tsd_assertions),
                                "action": (f"📋 {len(_tsd_assertions)} TSD assertion(s) handed to the "
                                           "code agent for test authoring")})
        except Exception as e:  # noqa: BLE001 — prompt enrichment, never a blocker
            logger.debug("tsd assertion extraction for authoring failed: %s", e)

    for round_i in range(max_cont + 1):
        logger.info("code: run=%s continuation round %d/%d (retry_feedback=%s, approach=%s)",
                    run.id, round_i + 1, max_cont + 1, bool(feedback),
                    (approach or {}).get("approach") if isinstance(approach, dict) else None)
        _cancel = lambda: S.check_cancel(db.get(AgenticRun, run.id)) or _lease_lost_set(art)
        _hb = _heartbeat(db, run, art)
        cs = await agentic_subagents.run_code_change(
            db, run_id=run.id, ctx=art["ctx"], xsd_scope=art.get("xsd_scope"),
            intent=art.get("intent", ""), model=model, feedback=feedback, continuation=continuation,
            approach=approach, decisions_block=_decisions_block, plan_block=_plan_block,
            workspace_run_id=_ws_id(run), resume_transcript=resume,
            cancel_check=_cancel,
            heartbeat=_hb, completion_check=_converge_check,
            tsd_assertions=_tsd_assertions)
        resume = cs.transcript or None       # every later round continues THIS conversation
        logger.info("code: run=%s round %d done — stopped=%s ops=%d iters=%d",
                    run.id, round_i + 1, cs.stopped, len(cs.operations), cs.iterations)
        read_acc.update(tuple(x) for x in (cs.read_files or []))
        intel_acc.update(getattr(cs, "intel_queried", None) or [])
        # Any round that made real file edits — completed OR capped — resets the no-progress
        # streak. Without this the counter was only reset inside the max_iterations branch, so a
        # productive completed/redrive round between two empty capped rounds left it intact and
        # the "two CONSECUTIVE capped zero-edit rounds" abort tripped on NON-consecutive rounds,
        # killing a converging run while the banner still claimed they were consecutive.
        if cs.operations:
            no_progress_rounds = 0
        if cs.plan:
            plan_memory = _format_plan(cs.plan)
        _rp = "\n".join(f"  - {p}" for p in sorted({x[1] for x in read_acc if len(x) > 1}))
        read_paths = _rp[:6000] + ("\n  … (read-list clipped — it is INCOMPLETE)" if len(_rp) > 6000 else "")
        # Durably persist the resume-state so a crash/cancel/API-failure resume rebuilds the
        # continuation above instead of re-exploring (the restore reads run.handoff_json).
        # Committed each round (small JSON) — the code agent's memory is otherwise in-process only.
        if db is not None:
            try:
                _h = dict(run.handoff_json or {})
                _h["code_resume"] = {"plan": plan_memory,
                                     "read_files": [list(x) for x in sorted(read_acc) if len(x) > 1],
                                     "intel_queried": sorted(intel_acc),
                                     "round": round_i + 2}
                run.handoff_json = _h
                db.add(run); db.commit()
            except Exception as e:  # noqa: BLE001 — resume-memory persist must never break the loop
                logger.debug("code_resume persist failed: %s", e)
                db.rollback()
        if cs.stopped == "budget_exceeded":
            # A6 — the per-run token budget guard tripped (core/observability.py
            # get_cumulative_run_tokens vs settings.agentic_token_budget_hard_cap).
            # Treat as a terminal stop for THIS phase, same shape as max_iterations'
            # continuation-budget-exhausted branch, but with its own event/label so
            # an operator can tell "ran out of turns" from "ran out of $$" at a
            # glance and knows which knob to raise before re-running.
            emit_event(db, run.id, "loop_capped",
                       {"continuations": round_i + 1, "budget_exceeded": True, "action":
                        "⛔ Run token budget exhausted — stopping the code phase "
                        "(edits made so far remain on disk; re-run with a raised "
                        "AGENTIC_TOKEN_BUDGET_HARD_CAP to continue)"})
            break
        if cs.stopped == "max_iterations":
            # No-progress abort: TWO consecutive capped rounds with zero file edits means the
            # agent is exploring in circles (~200 turns without one edit — the observed
            # non-convergence mode via AiNxt: thinking dropped + forced temperature=0). Burning
            # the remaining continuation budget will not converge; stop visibly instead so the
            # failure surfaces in minutes, not hours of silent grinding.
            if not cs.operations:            # zero-edit cap; a round with edits already reset it above
                no_progress_rounds += 1
            if no_progress_rounds >= 2:
                emit_event(db, run.id, "loop_capped",
                           {"continuations": round_i + 1, "no_progress": True, "action":
                            "⛔ Two consecutive capped rounds with ZERO file edits — the agent "
                            "is not converging; stopping the code phase (verifying what exists)"})
                break
            if round_i >= max_cont:
                emit_event(db, run.id, "loop_capped",
                           {"continuations": round_i, "action":
                            "⚠ Continuation budget exhausted — change may be incomplete; verifying anyway"})
                break
            # Cap hit mid-change → CONTINUE with MEMORY (plan + already-changed + already-read) so the
            # agent finishes the REMAINING work instead of re-exploring. Built from disk + the carried
            # read-set, so a crash-resume continues identically.
            emit_event(db, run.id, "loop_capped",
                       {"continuation": round_i + 1, "iterations": cs.iterations, "action":
                        f"⚠ Work-step cap hit ({cs.iterations} turns) — continuing (round {round_i + 2}) with memory"})
            continuation = {"round": round_i + 2, "diff_stat": _diff_stat(run),
                            "plan": plan_memory, "read": read_paths,
                            "notes": (cs.final_text or "")[:1200]}
            # feedback rides along: a cap hit mid-fix must NOT amnesia the verify errors /
            # review blockers the round entered to fix (they used to be nulled here — the
            # agent then re-looped the same diff without knowing what was wrong with it).
            continue
        # Agent stopped on its OWN ("completed"/cancelled). If it declared completed but its own
        # submit_plan lists files it never touched, drive ONE completion round (diff_stats_gate,
        # finally wired) so a multi-file plan can't freeze after a single edit. One-shot: a
        # legitimately-dropped planned file can't loop; the build gate is the ultimate check.
        # Coverage/stub gaps must be judged against the CUMULATIVE DISK edits (all rounds) — NOT the
        # last round's in-memory ops, which are empty on a resumed / all-done round. Judging the
        # empty in-memory set made a COMPLETE change look "unimplemented" and drove pointless
        # completion rounds against the agent's correct "no change needed". Carry the agent's own
        # submitted plan onto the disk change-set so the file-coverage check still has it.
        gaps = ""
        if cs.stopped == "completed":
            _dcs = _disk_change_set(db, run)
            setattr(_dcs, "plan", getattr(cs, "plan", None) or {})
            gaps = _plan_gap_feedback(_dcs)
            # R4 self-check (FEWEST ITERATIONS): verify the ratified plan's CONCRETE deliverables against the
            # agent's OWN diff BEFORE review — an unmet predicate is fed back as a precise definition-of-done,
            # closing the gap inside code_change instead of a review round later. Extract once (cached in art),
            # fail-open. The deterministic review gate (R4 ENFORCE) is the backstop if a gap survives the cap.
            if getattr(settings, "agentic_acceptance_predicates", False):
                try:
                    from app.agents import acceptance_predicates as AP
                    _ap_preds = art.get("acceptance_predicates")
                    if _ap_preds is None:
                        _ap_preds = await AP.extract_predicates(_plan_block)
                        art["acceptance_predicates"] = _ap_preds
                    _ap_miss = AP.unmet(AP.check_predicates(_ap_preds, _fidelity_diff_summary(run, _dcs)))
                    # Bare-token predicates are naming-sensitive (is_bare_token contract) —
                    # imperative consumers treat them as advisory, same as the in-loop nudge.
                    _ap_miss = [r for r in _ap_miss if not AP.is_bare_token(r.predicate)]
                    if _ap_miss:
                        gaps = (gaps + "\n" + AP.feedback_block(_ap_miss)).strip()
                except Exception as e:  # noqa: BLE001 — self-check must never break code_change
                    logger.debug("acceptance self-check failed: %s", e)
        if gaps and gap_redrives < 2 and round_i < max_cont:
            gap_redrives += 1
            emit_event(db, run.id, "plan_incomplete",
                       {"continuation": round_i + 1, "action":
                        "⚠ Plan not fully implemented — driving a completion round", "detail": gaps[:300]})
            continuation = {"round": round_i + 2, "diff_stat": _diff_stat(run),
                            "plan": plan_memory, "read": read_paths, "gaps": gaps,
                            "notes": (cs.final_text or "")[:1200]}
            continue
        # Review-completeness floor: when this phase is fixing reviewer findings, the agent must not
        # declare "done" while a flagged file was never touched. Without this it fixed 1-3 items and
        # stopped (ops=2,1,3 against 5-9 blockers/round) → the re-review re-raised the rest and the
        # loop never converged. Deterministic file-touch check; the re-review still judges CORRECTNESS.
        skipped = ""
        if _review_fix and cs.stopped == "completed":
            rv = art.get("review") or {}
            # reviewer_gap items are excluded from the fix list (_review_feedback_errors)
            # — the floor must not then demand the agent touch files for findings it was
            # never asked to fix.
            flagged = [i.get("file") for i in (rv.get("items") or [])
                       if i.get("file") and not i.get("reviewer_gap")]
            skipped = ", ".join(_untouched_review_files(
                flagged, [op.path for op in _disk_change_set(db, run).operations])[:15])
        if skipped and review_redrives < 2 and round_i < max_cont:
            review_redrives += 1
            emit_event(db, run.id, "review_incomplete",
                       {"continuation": round_i + 1, "action":
                        "⚠ Reviewer-flagged files left untouched — driving a completion round",
                        "detail": skipped[:300]})
            continuation = {"round": round_i + 2, "diff_stat": _diff_stat(run),
                            "plan": plan_memory, "read": read_paths,
                            "gaps": ("You declared done but left these REVIEWER-FLAGGED files UNTOUCHED: "
                                     f"{skipped}. Open each one and apply the required fix now — address "
                                     "EVERY flagged finding in this pass, do not stop until all are done."),
                            "notes": (cs.final_text or "")[:1200]}
            continue
        break                                         # finished clean (or cancelled, or re-drive spent)
    art["change_set"] = cs
    # SDLC review gaps 7/8/9/11 — the FULL cross-round intel-token set (not just this
    # round's cs.intel_queried), so a symbol queried in round 1 but edited in round 3
    # (a common shape: explore broadly, then implement) still counts as "checked".
    art["intel_queried_all"] = sorted(intel_acc | set(getattr(cs, "intel_queried", None) or []))
    # Author-memory for the NEXT entry into this phase (verify-fail retry / review loop-back):
    # the fixer continues this conversation instead of re-deriving the change from summaries.
    art["code_transcript"] = cs.transcript or art.get("code_transcript")
    emit_event(db, run.id, "change_set", {"ops": len(cs.operations), "stopped": cs.stopped})
    # In-loop deterministic self-correction (Slice 15) — a cheap compile→fix BEFORE the authoritative
    # gate, so simple compile errors don't burn a full code-agent + full-build retry round. Gated
    # (default off), fail-open, and NON-DESTRUCTIVE: it restores the agent's edits if it can't reach a
    # clean compile, so it can only hand `_phase_verify` an equal-or-better state — never a worse one.
    if settings.use_self_correction:
        await _inloop_self_correct(db, run, art)


async def _inloop_self_correct(db, run: AgenticRun, art: dict) -> None:
    """Cheap, NON-DESTRUCTIVE in-loop compile→fix before the authoritative ``_phase_verify``.

    Reuses the dormant self-correction module (Slice 15): compile the agent's touched files with the
    SAME local verifier (scoped — ``app_blast_radius=False``), and on a non-zero build feed the stderr
    to the lightweight LLM fixer for up to ``self_correction_max_iterations`` passes. Properties:

    * **Degrades gracefully** — if there is no local toolchain (``select_verifier()`` is ``deferred``)
      it is a no-op; the authoritative gate (flagged ``unverified`` → CI) still runs exactly as today.
    * **Bounded to the agent's own files** — only paths the agent already touched are rewritten; the
      fixer can fix an existing edit but cannot introduce new, unreviewed files.
    * **Restore-on-failure** — if it can't reach a clean compile the original edits are written back, so
      review/verify always see the agent's output, never a half-applied fix.

    Never raises (the authoritative ``_phase_verify`` remains the source of truth)."""
    from types import SimpleNamespace
    from app.agents.self_correction import generate_fix_via_llm, self_correct

    verifier = select_verifier()
    if verifier.name != "local":
        logger.info("self-correct: no local toolchain (verifier=%s) — deferring to authoritative gate",
                    verifier.name)
        return

    ws = _ws_id(run)
    repo_ids = list(run.selected_repo_ids or [])
    # Snapshot the touched files, repo-addressed (key = "<repo_id>/<repo-relative-path>"). This is both
    # the self-correct seed and the restore point. Skip deletes / unreadable / malformed ops.
    snapshot: dict[str, str] = {}
    for op in _disk_change_set(db, run).operations:
        rid, path, content = getattr(op, "repo_id", None), getattr(op, "path", None), getattr(op, "content", None)
        if getattr(op, "op", None) == "delete" or rid is None or path is None or content is None:
            continue                          # fail-open: skip deletes / unreadable / malformed ops
        snapshot[f"{rid}/{path}"] = content
    if not snapshot:
        return

    def _route(key: str) -> tuple[str, str] | None:
        # Match the longest selected repo id so "<rid>/<rel>" splits unambiguously.
        for rid in sorted(repo_ids, key=len, reverse=True):
            if key.startswith(rid + "/"):
                return rid, key[len(rid) + 1:]
        return None

    def _write(files: dict[str, str]) -> None:
        for key, content in files.items():
            if key not in snapshot or not isinstance(content, str):
                continue                      # bound to the agent's own touched files
            routed = _route(key)
            if not routed:
                continue
            rid, rel = routed
            try:
                # preserve the file's own EOL — an LF write onto a CRLF source turns the
                # whole file into one giant diff (same defect class as the edit tool)
                workspace_local.write_preserving_eol(workspace_local.repo_dir(ws, rid) / rel, content)
            except OSError as e:              # noqa: PERF203 — best-effort
                logger.warning("self-correct: write failed %s: %s", key, e)

    def run_sandbox(files: dict[str, str]):
        _write(files)
        outcome = verifier.verify(db, ws, _disk_change_set(db, run), app_blast_radius=False)
        ok = outcome.status == "verified"
        stderr = "" if ok else "\n".join(verification_plan.format_errors(outcome))
        return SimpleNamespace(exit_code=0 if ok else 1, stdout="", stderr=stderr)

    try:
        result = await self_correct(
            dict(snapshot), generate_fix=generate_fix_via_llm, run_sandbox=run_sandbox,
            max_iterations=settings.self_correction_max_iterations)
    except Exception as e:                    # noqa: BLE001 — self_correct is already fail-open; belt-and-braces
        logger.warning("self-correct: loop raised (fail-open, restoring): %s", e)
        _write(snapshot)
        return

    if result.success:
        _write(result.final_code)             # keep the clean state (already on disk; explicit for safety)
        action = f"✅ In-loop self-correction reached a clean compile in {result.iterations} pass(es)"
    else:
        _write(snapshot)                      # NON-DESTRUCTIVE restore — original edits go to the gate
        action = (f"⚠ In-loop self-correction did not converge ({result.iterations} pass(es)) — "
                  "kept the original edits for the authoritative gate")
    if settings.agentic_progress_ledger:      # durable, advisory trace (mirrors the verify ledger)
        run.progress_ledger_json = {**(run.progress_ledger_json or {}),
                                    "inloop_self_correct": {"success": result.success,
                                                            "iterations": result.iterations}}
        db.add(run)
    emit_event(db, run.id, "self_correction",
               {"success": result.success, "iterations": result.iterations, "action": action})


def _phase_verify(db, run: AgenticRun, art: dict, *, app_blast_radius: bool = True) -> str:
    """AUTHORITATIVE verification through the selected backend, against the DISK
    change-set (ground truth — covers continuations/resumes). Returns the status:
    'verified' | 'needs_fix' | 'unverified'. Stores parsed file:line errors in art.

    ``app_blast_radius`` False (Phase A) → compile schema + install core to ~/.m2 only;
    True (Phase B) → also full-build app consumers so a broken caller fails the gate."""
    verifier = select_verifier()
    outcome = verifier.verify(db, _ws_id(run), _disk_change_set(db, run),
                              app_blast_radius=app_blast_radius)
    errors = verification_plan.format_errors(outcome)
    modules = getattr(outcome, "module_results", {}) or {}
    art["verification"] = {"status": outcome.status, "gates": outcome.gates,
                           "errors": errors, "reason": outcome.reason, "backend": verifier.name,
                           "modules": modules}
    # Anti-oscillation log (Tier 2): append each failure's signature so the next code attempt
    # can see what already failed and avoid re-introducing a regressed fix. Bounded; on `art`,
    # which survives the in-process retry loop (resets on crash-resume — best-effort).
    if outcome.status == "needs_fix":
        log = art.get("attempt_log") or []
        log.append({"errors": (errors or [])[:6]})
        art["attempt_log"] = log[-5:]
    built = sum(1 for m in modules.values() if m.get("status") == "built")
    failed = sum(1 for m in modules.values() if m.get("status") == "failed")
    action = {
        "verified":   f"✅ Verification passed — {built} module(s) built",
        "needs_fix":  f"❌ Verification failed — {failed} module(s) failed, {len(errors)} error(s)",
        "unverified": f"⚠ Could not verify — {outcome.reason}",
    }.get(outcome.status, outcome.status)
    emit_event(db, run.id, "verification", {**art["verification"], "action": action})
    return outcome.status


_REVERIFY_ACTION = {
    "verified":   "✅ Re-verify passed — the change still builds",
    "needs_fix":  "❌ Re-verify failed — the change no longer builds cleanly",
    "unverified": "⚠ Re-verify could not complete a build",
    "expired":    "⚠ Re-verify unavailable — the build workspace was cleaned up; re-run the change",
    "error":      "⚠ Re-verify errored",
}


def reverify_run(db, run_id: str) -> dict:
    """On-demand RE-VERIFICATION of an already-generated / approved change.

    Re-runs the authoritative build verification against the run's EXISTING
    on-disk workspace and reports pass/fail — WITHOUT re-running code generation,
    review, freeze, or push, and WITHOUT changing the run's phase/status. Lets a
    human confirm "does the approved change still build?" without driving the
    whole pipeline again.

    The only persisted effect is a ``handoff_json["last_reverify"]`` record plus a
    ``verification`` (from :func:`_phase_verify`) + ``reverify_done`` event, so the
    UI badge/log updates. Idempotent; safe to call repeatedly. Returns the
    ``last_reverify`` payload.
    """
    from app.models.base import utcnow

    run = db.get(AgenticRun, run_id)
    if run is None:
        return {"status": "error", "reason": "run not found"}

    def _record(status: str, reason: str = "", extra: dict | None = None) -> dict:
        payload = {"status": status, "at": utcnow().isoformat()}
        if reason:
            payload["reason"] = reason
        if extra:
            payload.update(extra)
        h = dict(run.handoff_json or {})
        h["last_reverify"] = payload
        run.handoff_json = h
        emit_event(db, run.id, "reverify_done",
                   {**payload, "action": _REVERIFY_ACTION.get(status, status)})
        db.commit()
        return payload

    ws = _ws_id(run)
    # The workspace (with the change applied) must still be on disk. After the TTL
    # GC it's gone — and we must NOT silently report "verified" off an empty
    # change-set. Re-running the change is the only way to re-verify then.
    if not workspace_local.run_dir(ws).exists():
        return _record("expired", _REVERIFY_ACTION["expired"])

    # Guard the false-positive: if the workspace exists but holds no change (e.g.
    # the tree was reset), build_plan would be empty and the verifier would return
    # "verified" off nothing. Treat "no change on disk" as not-verifiable instead.
    try:
        cs = _disk_change_set(db, run)
    except Exception as exc:  # noqa: BLE001
        logger.exception("reverify_run: change-set read failed run=%s", run_id)
        return _record("error", f"could not read the workspace change-set: {exc}")
    if not getattr(cs, "operations", None):
        return _record("expired",
                       "no change found on disk to build (the workspace may have been "
                       "reset) — re-run the change to verify it again")

    try:
        art: dict = {}
        status = _phase_verify(db, run, art)   # real mvn build + emits `verification`
    except Exception as exc:  # noqa: BLE001
        logger.exception("reverify_run: build failed run=%s", run_id)
        return _record("error", f"verification crashed: {exc}")

    v = art.get("verification", {}) or {}
    modules = v.get("modules", {}) or {}
    built = sum(1 for m in modules.values() if m.get("status") == "built")
    failed = sum(1 for m in modules.values() if m.get("status") == "failed")
    return _record(status, v.get("reason") or "",
                   {"gates": v.get("gates", {}),
                    "modules_built": built, "modules_failed": failed})


# A blocking finding in one of these categories is treated as a MUST-NOT-SHIP blocker even when
# the reviewer graded its `severity` lower. Rationale: the adversarial reviewer is the SAME model
# as the implementer, and it tends to under-grade its OWN security/auth holes (observed: an
# unauthenticated cancel endpoint marked `warning`, not `blocker`). Category is a more robust
# signal than the model's severity self-assessment for this class. Shared with the push gate
# (`api/agentic.py::_unresolved_blockers`) so detection and enforcement stay consistent.
SENSITIVE_BLOCK_CATEGORIES = {"security", "auth", "authentication", "authorization",
                              "financial", "regulatory", "compliance"}


def is_must_block(category: str | None, severity: str | None) -> bool:
    """A finding is must-not-ship if the reviewer called it blocker-severity OR it sits in a
    sensitive category (security/auth/financial/regulatory). Used for both the in-loop
    has_blocker flag and the push-gate's blocker filter."""
    return (severity == "blocker") or ((category or "").lower() in SENSITIVE_BLOCK_CATEGORIES)


# WS3b — shared validator/helper widening. A file like ValidatorCommons/…Util/…Base whose methods are
# called by many message validators; editing it (e.g. tightening a shared validateHead allow-list)
# silently changes behaviour for message types the change never targeted — the exact backward-compat
# risk we saw widen prodType across ReqHbt/ReqBalEnq/… on fa4631e3.
_SHARED_HELPER_RE = re.compile(r"(Commons|Utils?|Helper|Base|Support|Shared)\.(java|kt)$", re.I)


def _is_shared_helper(path: str) -> bool:
    base = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return bool(_SHARED_HELPER_RE.search(base))


def _shared_validator_widening(db, run: AgenticRun, cs) -> list[dict]:
    """Deterministic WS3b detector: a changed SHARED helper referenced by ≥2 OTHER ``*Validator`` files
    has widened behaviour beyond the change's target. Returns SELF-HEAL review findings (category
    'correctness' / severity 'error' → loops the code agent back, but NOT must-block so it never
    escalates to a human). Best-effort + FS-bounded — only walks when a shared helper actually changed."""
    findings: list[dict] = []
    try:
        ops = [o for o in (getattr(cs, "operations", None) or [])
               if getattr(o, "op", "") in ("add", "modify") and _is_shared_helper(getattr(o, "path", "") or "")]
        if not ops:
            return []
        ws = _ws_id(run)
        changed_bases = {(getattr(o, "path", "") or "").rsplit("/", 1)[-1] for o in ops}
        for o in ops:
            cls = (getattr(o, "path", "") or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if not cls:
                continue
            try:
                rdir = workspace_local.repo_dir(ws, o.repo_id)
            except Exception:  # noqa: BLE001
                continue
            callers: set[str] = set()
            for pat in ("*Validator.java", "*Validator.kt"):
                for fp in rdir.rglob(pat):
                    sp = str(fp).replace("\\", "/")
                    if "/src/test/" in sp or "/target/" in sp or fp.name in changed_bases:
                        continue
                    try:
                        if cls in fp.read_text(encoding="utf-8", errors="ignore"):
                            callers.add(fp.name)
                    except Exception:  # noqa: BLE001
                        continue
                    if len(callers) >= 6:
                        break
            if len(callers) >= 2:
                shown = ", ".join(sorted(callers)[:5])
                findings.append({
                    "category": "correctness", "severity": "error", "file": o.path, "line": None,
                    "why": (f"shared helper {cls} is called by {len(callers)} other validators ({shown}) — "
                            "editing it widens behaviour for message types this change did not target "
                            "(backward-compat risk)"),
                    "suggested_fix": (f"Do NOT widen the shared {cls}. Isolate the new rule to the "
                                      "message-specific validator (an overload / new method on the targeted "
                                      "Req*Validator), OR widen any new allow-list to include EVERY value the "
                                      "other callers already accept, so untouched message types behave exactly "
                                      "as before."),
                    "shared_heal": True})
            if len(findings) >= 5:
                break
    except Exception as e:  # noqa: BLE001 — detector must never break the verdict
        logger.debug("shared-validator widening detector failed: %s", e)
    return findings


async def _plan_fidelity_call(db, run: AgenticRun, plan_block: str, cs_disk):
    """The plan-fidelity gate as a standalone awaitable so it can overlap the reviewer's LLM call
    under ``agentic_parallel_review``. A pure extraction of the call the gate try/except below owns —
    the caller keeps the fail-open handling and the deterministic merge, so the verdict is identical."""
    from app.agents import plan_fidelity
    return await plan_fidelity.check_plan_fidelity(
        plan_text=plan_block,
        success_criteria=_success_criteria(db, run.change_request_id),
        planned_files=_planned_files(db, run.change_request_id,
                                     exclude_schema=((run.kind or "") == "code")),
        touched_files=[op.path for op in (cs_disk.operations or [])],
        # REAL full diff (fail-open to the content-head summary) — the 18k/3k-per-file summary
        # dropped every NEW file on Test 8 and the judge reported the whole feature "missing".
        diff_summary=_fidelity_diff_summary(run, cs_disk),
        # D2 — the agent's declared planned→actual consolidations (submit_plan.reconciliation):
        # verified behaviourally at the actual path instead of flagged as dropped deliverables.
        reconciliations=(getattr(cs_disk, "plan", None) or {}).get("reconciliation"))


def _declared_error_codes(db, change_request_id: str) -> list[dict]:
    """The plan's declared error codes (code/entity/td_bd/description), read from the
    change-request context's structured ``proposals``. These live ONLY in ``proposals`` —
    ratification drops them from ChangeAnalysis, so the code agent never sees them as a
    plan item; the contract gate re-injects them as a hard emit requirement. Fail-open."""
    try:
        from app.models.change_request_context import ChangeRequestContext
        row = (db.query(ChangeRequestContext)
               .filter(ChangeRequestContext.change_request_id == change_request_id).first())
        codes = ((row.proposals or {}).get("error_codes") if row else None) or []
        return [c for c in codes if isinstance(c, dict) and c.get("code")]
    except Exception:  # noqa: BLE001 — best-effort; the gate fails open on []
        return []


def _reconcile_schema_coverage(db, change_request_id: str, xsd_scope) -> dict:
    """Compare the plan's schema CHANGE-LIST (the .xsd/.xjb files per_file_change_list
    names) against what Phase A actually FROZE (xsd_scope created ∪ edited). A plan
    schema file Phase A never froze is one the CODE phase is forced to author — which,
    with the code-phase schema write-lock, means blocked or improvised. This is the
    cbabbf9c failure: the plan listed ApiName + 4 split XSDs, Phase A froze only
    network-common, and codegen created the 5. Surfaced at the (cheap) XSD gate so a human
    binds each file (create + approve, or confirm reuse) before code generation.
    Deterministic + advisory. Fail-open."""
    def _base(p: str) -> str:
        return (p or "").rsplit("/", 1)[-1].strip().lower()
    try:
        ca = _latest_ratified_analysis(db, change_request_id)   # the change-list is the RATIFIED plan's
        plan_schema = set()
        for p, _pf in _plan_file_entries(ca.technical_analysis if ca else None):
            b = _base(p)
            if b.endswith((".xsd", ".xjb")):
                plan_schema.add(b)
        frozen = {_base(p) for p in (getattr(xsd_scope, "created", None) or [])}
        frozen |= {_base(s.rsplit(":", 1)[-1]) for s in (getattr(xsd_scope, "edits_applied", None) or [])}
        return {"plan_schema": sorted(plan_schema), "frozen": sorted(frozen),
                "unfrozen": sorted(plan_schema - frozen)}
    except Exception:  # noqa: BLE001 — advisory; fail-open
        return {"plan_schema": [], "frozen": [], "unfrozen": []}


async def _collect_gate_suspects(db, run: AgenticRun, cs) -> list[dict]:
    """Run the deterministic gates in SHADOW and return their blocker findings as
    ADVISORY suspects for the goal-verifier to confirm or dismiss by reading the code
    (the gates are high-recall/low-precision; the LLM supplies precision). Fail-open:
    any error → drop that gate's suspects, never break the review."""
    suspects: list[dict] = []
    ws_id = _ws_id(run)

    def _corpus(rids):
        out = []
        for rid in rids:
            root = workspace_local.repo_dir(ws_id, rid)
            for i, p in enumerate(root.glob("**/src/main/java/**/*.java")):
                if i >= 4000:
                    break
                try:
                    out.append((f"{rid}/{p.relative_to(root)}", p.read_text(errors="ignore")))
                except Exception:  # noqa: BLE001
                    pass
        return out

    ops = [op for op in (getattr(cs, "operations", None) or [])
           if op.path.endswith(".java") and "src/test/" not in op.path and getattr(op, "op", "") != "delete"]
    try:
        from app.agents import di_wiring_gate as DIW
        if ops:
            changed = {f"{op.repo_id}/{op.path}" for op in ops}
            new = {f"{op.repo_id}/{op.path}" for op in ops if getattr(op, "op", "") == "add"}
            di = DIW.run_di_gate(changed, _corpus({op.repo_id for op in ops}), new_paths=new,
                                 diff_text=agentic_review._render_diff(ws_id, cs, cap=None))
            suspects += [{"check": f.check, "key": f.key, "detail": f.detail}
                         for f in di.findings if f.severity == "blocker"]
    except Exception as e:  # noqa: BLE001
        logger.debug("goal_verifier di-gate suspects failed: %s", e)
    try:
        from app.agents import contract_gate as CG
        codes = _declared_error_codes(db, run.change_request_id)
        if codes:
            corpus_text = "\n".join(t for _, t in _corpus({op.repo_id for op in (getattr(cs, "operations", None) or [])}))
            cg = CG.run_contract_gate(agentic_review._render_diff(ws_id, cs, cap=None), codes,
                                      corpus_text=corpus_text)
            suspects += [{"check": f.check, "key": f.key, "detail": f.detail}
                         for f in cg.findings if f.severity == "blocker"]
    except Exception as e:  # noqa: BLE001
        logger.debug("goal_verifier contract-gate suspects failed: %s", e)
    return suspects


def _persist_gv_findings(db, run_id: str, result, model: str, rounds: int) -> None:
    """Round-tag the verifier's gaps as ReviewFinding rows so prior-round threading and
    the UI keep working (same table the legacy reviewer writes). Best-effort."""
    if db is None:
        return
    try:
        from app.models.agentic import ReviewFinding
        from app.models.base import generate_uuid
        for g in result.gaps:
            db.add(ReviewFinding(
                id=generate_uuid(), run_id=run_id, round=rounds,
                category="correctness", severity="blocker", blocking=True,
                file=(g.location or None), line=None, why=(g.detail or "")[:2000],
                suggested_fix="", reviewer_model=model))
    except Exception as e:  # noqa: BLE001 — persistence is best-effort
        logger.debug("goal_verifier finding persist failed: %s", e)


async def _phase_review_goal_verifier(db, run: AgenticRun, art: dict, model, *,
                                      rounds: int, cs_disk, plan_block: str,
                                      directives: list[str], prior: list[dict]) -> bool:
    """Grok-style goal-verifier review (settings.agentic_reviewer_mode='goal_verifier').

    Returns ``blocking`` (loop back to CODE_CHANGE) and sets ``art['review']`` in the
    shape the legacy caller consumes, so no caller change is needed. Convergence: a
    path:line gap FINGERPRINT that repeats across rounds STOPS the loop (routes to the
    human gate) instead of riding the round cap. ``BLOCKED`` (contradiction/unverifiable)
    also routes to the human, not a code retry."""
    from app.agents import agentic_goal_verifier as GV
    from app.agents.goal_verifier_core import Outcome, record_stall

    led = dict(getattr(run, "progress_ledger_json", None) or {})
    prior_gaps = led.get("gv_gaps") or (
        "\n".join(f"- ({p.get('severity')}) {str(p.get('why') or '')[:240]}"
                  + (f" [{p.get('file')}]" if p.get("file") else "") for p in prior[:20]) or None)
    suspects = await _collect_gate_suspects(db, run, cs_disk)
    result = await GV.run_goal_verifier(
        db, run_id=run.id, ctx=art["ctx"], change_set=cs_disk, intent=art.get("intent", ""),
        plan_block=plan_block, directives=directives, prior_gaps=prior_gaps,
        gate_suspects=suspects, reviewer_model=model,
        cancel_check=lambda: S.check_cancel(db.get(AgenticRun, run.id)) or _lease_lost_set(art),
        workspace_run_id=_ws_id(run), progress=_heartbeat(db, run, art))

    _persist_gv_findings(db, run.id, result, model, rounds)
    # M3 (grok cap-bonus parity): while a strategist restructure is in flight the stall guard is
    # RELAXED (threshold 2 → 5) so the "one structural change" gets a fair chance instead of being
    # auto-paused after 2 rounds. The flag is set where the strategist fires (_phase_code); clear it
    # once the restructure produces progress (a fresh fingerprint resets the streak) or the goal
    # resolves — otherwise it would stay relaxed forever.
    strategist_active = bool(led.get("gv_strategist_active"))
    new_ct, stalled = record_stall(led.get("gv_fingerprint"), int(led.get("gv_stall") or 0),
                                   result.fingerprint, strategist_active=strategist_active)
    led["gv_fingerprint"], led["gv_stall"], led["gv_gaps"] = result.fingerprint, new_ct, result.gaps_summary
    if new_ct <= 1 or result.outcome in (Outcome.ACHIEVED, Outcome.FAIL_OPEN_ACHIEVED, Outcome.BLOCKED):
        led.pop("gv_strategist_active", None)
    run.progress_ledger_json = led

    achieved = result.outcome in (Outcome.ACHIEVED, Outcome.FAIL_OPEN_ACHIEVED)
    needs_human = (result.outcome == Outcome.BLOCKED) or stalled
    has_blocker = not achieved
    # loop back to code ONLY for a fixable NOT_ACHIEVED that hasn't stalled; BLOCKED/stall
    # return blocking=False + has_blocker=True → the caller freezes to AWAITING_HUMAN_APPROVAL.
    blocking = (result.outcome == Outcome.NOT_ACHIEVED) and not needs_human
    items = [{"category": "correctness", "severity": "blocker", "file": g.location or "",
              "line": None, "why": g.detail, "blocking": True,
              "reviewer_gap": needs_human and not stalled} for g in result.gaps]
    art["review"] = {"blocking": blocking, "findings": len(result.gaps), "items": items,
                     "has_blocker": has_blocker, "reviewer_model": model, "notes": [],
                     "mode": "goal_verifier", "outcome": result.outcome.value,
                     "blocking_kind": result.blocking.value, "stalled": stalled,
                     "plan_fidelity_gaps": 0, "escalated": needs_human, "dropped_must_block": 0}
    emit_event(db, run.id, "review",
               {"mode": "goal_verifier", "outcome": result.outcome.value, "refuted": result.refuted,
                "findings": len(result.gaps), "has_blocker": has_blocker, "stalled": stalled,
                "blocking_kind": result.blocking.value, "panel": len(result.votes),
                "reviewer_model": model})
    if needs_human:
        emit_event(db, run.id, "review_gaps",
                   {"count": len(result.gaps),
                    "action": ("⚠ Verifier made NO progress across rounds (same gap fingerprint) — "
                               "held for human adjudication, not sent back to the code agent" if stalled
                               else "⚠ Verifier needs a human decision (contradiction/unverifiable), "
                                    "not a code retry")})
    return blocking


async def _phase_review(db, run: AgenticRun, art: dict, model) -> bool:
    rounds = (run.attempts_json or {}).get("review", 0) + 1
    # DISK is ground truth: BOTH the adversarial reviewer and the fidelity gate must judge ALL edits
    # across every continuation round — NOT art["change_set"], which is only the LAST round's
    # in-memory ops (empty on a resumed / all-done round). Reviewing the in-memory set handed the
    # reviewer an incomplete/empty diff and falsely flagged the change "incomplete" → review↔code
    # loop. Carry the agent's own submitted plan onto the disk set so plan rendering still has it.
    _cs_disk = _disk_change_set(db, run)
    setattr(_cs_disk, "plan", getattr(art.get("change_set"), "plan", None) or {})
    # The reviewer must judge the diff against what it was SUPPOSED to do: the ratified plan
    # plus the code agent's own submitted plan — so it can catch a half-baked change, not just bugs.
    _plan = _analysis_plan_block(db, run.change_request_id)
    _agent_plan = _format_plan(getattr(_cs_disk, "plan", None) or {})
    try:
        _criteria = _success_criteria(db, run.change_request_id)   # R6 — ratified ACCEPTANCE criteria (fail-open)
    except Exception:                                              # noqa: BLE001
        _criteria = ""
    plan_block = "\n\n".join(p for p in (
        ("RATIFIED PLAN:\n" + _plan) if _plan else "",
        # R6: anchor the reviewer to the explicit acceptance criteria — framed as a FLOOR, not a ceiling, so
        # it judges against a fixed spec (less flip-flop) WITHOUT narrowing it away from other defects.
        ("ACCEPTANCE CRITERIA — the change MUST satisfy EACH of these; this is the FLOOR, not the ceiling: "
         "verify every one, AND keep flagging any OTHER correctness / security / regression / edge-case "
         "issue you find:\n" + _criteria) if _criteria else "",
        ("IMPLEMENTER'S OWN SUBMITTED PLAN:\n" + _agent_plan) if _agent_plan else "") if p)
    # Run the independent, read-only plan-fidelity gate CONCURRENTLY with the reviewer when
    # agentic_parallel_review is on — overlaps two LLM round-trips. Latency only: the merge below is
    # deterministic, so the verdict is byte-identical to the sequential default (the off path).
    _pf_task = (asyncio.ensure_future(_plan_fidelity_call(db, run, plan_block, _cs_disk))
                if settings.agentic_parallel_review else None)
    # B1 inputs — the ratified plan's critical-decision directives, individually verdicted.
    _directives: list[str] = []
    try:
        # Ratified analysis only — these are verdicted as "the ratified plan's directives".
        _ca = _latest_ratified_analysis(db, run.change_request_id)
        _directives = critical_directives(_ca.technical_analysis if _ca else None)
    except Exception:  # noqa: BLE001 — directive enrichment must never break review
        _directives = []
    # B2 inputs — the previous round's blocking findings: verify-fixed-first, no re-derive.
    _prior: list[dict] = []
    if rounds > 1:
        try:
            from app.models.agentic import ReviewFinding
            # Reviewer-gap findings ([Dn] NOT-VERIFIED synthesis, the unparseable marker, AND
            # any contentless blocking finding with no anchor) are persisted blocking=True for
            # the push gate, but they describe the REVIEWER's verdict deficiency — feeding them
            # back as "prior blockers to re-verify as fixed" asks the reviewer to check whether
            # the AUTHOR fixed the reviewer. Use the SAME classifier the fix-list exclusion uses
            # (_is_reviewer_gap) — a divergent sentinel-string match here let an anchor-less
            # blocker be dropped from the fix list yet re-fed as a prior blocker, re-opening the
            # 215ead25 reviewer-checks-reviewer loop. It reads .why/.blocking/.file, all present
            # on the ReviewFinding ORM row.
            _prior = [{"severity": f.severity, "why": f.why, "file": f.file}
                      for f in (db.query(ReviewFinding)
                                .filter(ReviewFinding.run_id == run.id,
                                        ReviewFinding.round == rounds - 1,
                                        ReviewFinding.blocking.is_(True))
                                .limit(40).all())
                      if not agentic_review._is_reviewer_gap(f)]
        except Exception:  # noqa: BLE001
            _prior = []
    # ── Reviewer mode branch (grok-style goal-verifier vs the legacy gate loop) ──────
    # The goal-verifier path returns here with a legacy-compatible art['review']; the
    # deterministic gates below run only in the legacy path (they feed the verifier as
    # advisory suspects instead). Set AGENTIC_REVIEWER_MODE=legacy to fall back.
    if (getattr(settings, "agentic_reviewer_mode", "goal_verifier") or "goal_verifier") == "goal_verifier":
        return await _phase_review_goal_verifier(
            db, run, art, model, rounds=rounds, cs_disk=_cs_disk,
            plan_block=plan_block, directives=_directives, prior=_prior)
    # RESUME WITH MEMORY (§3): a crash / cancel / laptop-sleep lease loss kills the worker mid-review,
    # and the reviewer's in-flight file-discovery (grep/read) dies with it — so a fresh resume re-runs
    # the whole read-only sweep from turn 1 (the observed seq-290 waste). We durably checkpoint the
    # reviewer's read-set to handoff_json each iteration (below) and — on a resume of the SAME round —
    # hand it back so the reviewer skips re-discovery. Round-gated: a review_resume from an EARLIER round
    # (already routed through code_change and back) is stale and ignored — prior_blockers carry that
    # round's memory instead. This is the read-only counterpart of the code phase's code_resume.
    _rr = (getattr(run, "handoff_json", None) or {}).get("review_resume")
    _resume_reads = (_rr.get("read_files") or []) if (_rr and _rr.get("round") == rounds) else None
    if _resume_reads:
        emit_event(db, run.id, "review_resumed",
                   {"round": rounds, "read_files": len(_resume_reads),
                    "action": "⏩ Resuming review WITH memory — already-explored files restored; "
                              "not re-discovering the change from scratch"})
    _rr_persisted = {"n": -1}     # dedupe: only write when the read-set actually grew this round
    # The checkpoint must ACCUMULATE across attempts. run_review hands the restored paths to the
    # reviewer as prompt text only — run_agent_loop starts a fresh context and `_seed_read_files`
    # runs only when initial_messages is passed, which review never does — so ctx.read_files
    # begins empty on a resume. Overwriting with it would shrink a 40-file memory to the handful
    # the resumed reviewer re-opened, and a second interruption would then resume with almost
    # nothing. The code phase gets this right by seeding a cumulative read_acc from code_resume.
    _rr_seen = {tuple(x) for x in (_resume_reads or []) if isinstance(x, (list, tuple)) and len(x) > 1}

    def _review_progress(read_files: list) -> None:
        # Per-iteration read-set checkpoint (see RESUME WITH MEMORY above). Fail-open: a persist
        # error must never break the review. Committed on the drive session — the same mid-loop
        # commit pattern the code phase's heartbeat already uses.
        if db is None or not read_files:
            return
        _rr_seen.update(tuple(x) for x in read_files if len(x) > 1)
        if len(_rr_seen) <= _rr_persisted["n"]:
            return
        try:
            _h = dict(run.handoff_json or {})
            _h["review_resume"] = {"read_files": [list(x) for x in sorted(_rr_seen)][:400],
                                   "round": rounds}
            run.handoff_json = _h
            db.add(run); db.commit()
            _rr_persisted["n"] = len(_rr_seen)
        except Exception as e:  # noqa: BLE001 — resume-memory persist must never break the review
            logger.debug("review_resume persist failed: %s", e)
            db.rollback()
    try:
        rf = await agentic_review.run_review(
            db, run_id=run.id, ctx=art["ctx"], change_set=_cs_disk,
            xsd_scope=art.get("xsd_scope"), intent=art.get("intent", ""), plan_block=plan_block,
            round=rounds, workspace_run_id=_ws_id(run),
            directives=_directives, prior_blockers=_prior,
            # The reviewer must know schema is frozen, so it stops prescribing .xsd edits the
            # implementer cannot make. Unconditionally true here: REVIEW is only ever reached
            # via VERIFICATION ← CODE_CHANGE, and run_code_change always runs with
            # code_phase=True — a "full" run's code phase is just as schema-locked as a
            # standalone "code" run. (An xsd-kind run parks at approval and never gets here.)
            code_phase=True,
            resume_read_files=_resume_reads, progress=_review_progress)
    except BaseException:                       # noqa: BLE001 — cleanup then re-raise
        if _pf_task is not None:
            _pf_task.cancel()                   # don't leak the concurrent gate if the reviewer errors
        raise
    # Round complete — the verdict is in hand, so the interrupted-round resume memory is spent. Clear it
    # so the NEXT round (which carries its own prior_blockers) can't replay a stale read-set as if the
    # round were still mid-flight. A crash BEFORE this point leaves review_resume in place → next drive resumes.
    if db is not None and (run.handoff_json or {}).get("review_resume"):
        try:
            _h = dict(run.handoff_json or {}); _h.pop("review_resume", None)
            run.handoff_json = _h
            db.add(run); db.commit()
        except Exception as e:  # noqa: BLE001 — clear must never break the verdict
            logger.debug("review_resume clear failed: %s", e)
            db.rollback()
    # Store the actual BLOCKING findings (not just a count) so the loop-back to CODE_CHANGE can
    # hand them to the implementer — without this the review→fix loop is blind and re-loops the
    # same diff until the round budget is spent (shipping the half-baked change).
    # SORT blockers first so they ALWAYS make the 15-item cap — Codex P0 fix: previously a
    # blocker at position 16+ in the unsorted list was silently dropped from `items`, after
    # which the push gate (which filters items by severity) saw no blocker and shipped the
    # change. The blocker safety property is preserved when has_blocker is derived from
    # `items` AFTER the slice — i.e. from what was actually persisted to the manifest's
    # review snapshot, not from a transient count over the un-sliced findings.
    # must-block findings (blocker severity OR sensitive category) sort to the FRONT so the
    # 15-cap never drops one (Codex P0). has_blocker is then derived from what's persisted.
    blocking_sorted = sorted([f for f in rf.findings if f.blocking],
                             key=lambda f: 0 if is_must_block(f.category, f.severity) else 1)
    # The sort makes the 15-cap safe only while must-blocks number ≤15; 215ead25 had 21.
    # Overflow must-blocks are still persisted (agentic_review._persist) and re-fetched
    # as next round's prior_blockers, but the CURRENT fix list would silently omit them —
    # the incremental-discovery pathology. Count every must-block dropped at ANY slice
    # and surface the count to the fix list + the freeze banner (never silent).
    _dropped_mb = 0

    def _cap_items(lst: list[dict]) -> list[dict]:
        nonlocal _dropped_mb
        s = sorted(lst, key=lambda it: 0 if is_must_block(it.get("category"), it.get("severity")) else 1)
        _dropped_mb += sum(1 for it in s[15:] if is_must_block(it.get("category"), it.get("severity")))
        return s[:15]

    # reviewer_gap items (verdict deficiencies that survived the in-review judge retry) stay
    # in `items` so the push gate holds and the human sees them — but they are excluded from
    # the code agent's fix list (_review_feedback_errors): the author can't fix the reviewer.
    items = _cap_items([{"category": f.category, "why": f.why, "suggested_fix": f.suggested_fix,
                         "file": f.file, "line": f.line, "severity": f.severity,
                         "done_when": getattr(f, "done_when", "") or None,
                         "reviewer_gap": agentic_review._is_reviewer_gap(f) or None}
                        for f in blocking_sorted])
    has_blocker = any(is_must_block(it.get("category"), it.get("severity")) for it in items)
    # Transparency: surface the NON-blocking findings too. The reviewer used to report a findings
    # COUNT (e.g. "3 findings") while `items` held only blockers — so a reviewer that found 3 real
    # issues showed an empty list. These `notes` make every finding visible for human review without
    # blocking the run.
    notes = [{"category": f.category, "why": f.why, "suggested_fix": f.suggested_fix,
              "file": f.file, "line": f.line, "severity": f.severity}
             for f in rf.findings if not f.blocking][:15]
    # Plan-FIDELITY gate (additive to the adversarial reviewer): did the change DELIVER the RATIFIED
    # plan — every promised file + behaviour — and is the hardest requirement REAL, not faked? The
    # reviewer grades correctness of what IS present; this catches what's MISSING/FAKED, checked
    # against the RATIFIED plan (not the agent's own submitted plan, which can itself drop a file).
    # A blocking gap merges into the verdict so the run loops back to finish it. Fail-open.
    blocking = rf.blocking
    pf_gaps = 0
    try:
        # Uses _cs_disk (the cumulative disk truth computed at the top of this function) — never the
        # last round's in-memory ops, which made earlier-round files look "missing" and looped. When
        # parallel, _pf_task already ran it alongside the reviewer; otherwise compute it now.
        pf = await _pf_task if _pf_task is not None else await _plan_fidelity_call(db, run, plan_block, _cs_disk)
        pf_gaps = len(pf["findings"])
        # Plan-fidelity behavioural gaps are an LLM opinion → ADVISORY by default (agentic_plan_fidelity_advisory):
        # surfaced to the human + still drive bounded loop-back via `beh_gap` below, but NOT a must-block item,
        # so an LLM "X is missing" hallucination can never block the push. Real reviewer blockers still block.
        _pf_advisory = getattr(settings, "agentic_plan_fidelity_advisory", True)
        for f in pf["findings"]:
            items.append({"category": "completeness", "why": f"{f['item']} — {f['detail']}",
                          "suggested_fix": "Deliver this planned item — OR, if its logic lives in a "
                                           "differently-named file or it genuinely needs no edit, re-call "
                                           "submit_plan with a reconciliation entry {planned_path, "
                                           "actual_path, why}: that is what clears this finding.",
                          "file": f["item"] if f.get("kind") == "missing_file" else "",
                          "line": None,
                          # Only the LLM BEHAVIOURAL opinion is downgraded to advisory; the DETERMINISTIC
                          # file-coverage miss (`missing_file`) keeps its own severity.
                          "severity": ("warning" if (_pf_advisory and f.get("kind") != "missing_file")
                                       else f["severity"])})
        if pf["findings"]:
            # must-block findings to the FRONT so the 15-cap never drops one (mirrors the reviewer sort).
            items = _cap_items(items)
            has_blocker = any(is_must_block(it.get("category"), it.get("severity")) for it in items)
            emit_event(db, run.id, "plan_fidelity",
                       {"gaps": pf_gaps, "has_gap": pf["has_gap"],
                        "missing_files": pf["missing_files"][:10],
                        "action": (f"🔎 Plan-fidelity: {pf_gaps} gap(s) — looping back to finish"
                                   if pf["has_gap"] else f"🔎 Plan-fidelity: {pf_gaps} note(s)")})
        # R1′ — three-tier loop control. Real reviewer blockers always loop back (unchanged). An
        # LLM-only behavioural-completeness opinion (`has_gap` with no reviewer blocker) is the
        # NON-DETERMINISTIC driver behind the observed divergence (it returns blocker-severity gaps that
        # fluctuate round to round even with a green build and no missing files). Bound it: after
        # `agentic_max_behavioral_rounds` it becomes ADVISORY and the run proceeds to the human approval
        # gate — still flagged via `has_blocker`, so a genuine faked/missing behaviour is ESCALATED to a
        # human, never silently shipped, but the loop stops spinning on a verdict it can't satisfy.
        beh_gap = pf["has_gap"]
        # getattr-guard the flags (config skew across environments — same pattern as the round caps).
        if (getattr(settings, "agentic_behavioral_gap_advisory", True) and beh_gap and not rf.blocking
                and rounds > getattr(settings, "agentic_max_behavioral_rounds", 2)):
            beh_gap = False
            emit_event(db, run.id, "plan_fidelity_advisory",
                       {"rounds": rounds, "gaps": pf_gaps,
                        "action": "ℹ Behavioural completeness gaps persist but the build and all planned "
                                  "files are satisfied — surfacing to human approval instead of looping further"})
        # A DETERMINISTIC un-hedged file-coverage miss (missing_file/blocker) drives the self-heal
        # loop-back too — not only the LLM behavioural opinion. Unlike `beh_gap` it is NOT downgraded
        # after the behavioural-round budget: a plan-required file the diff never touched is a hard
        # fact, not an opinion. It participates in the R5 stall path below (loop back to finish the
        # file for K rounds, THEN escalate to the human gate), so a dropped deliverable is either
        # completed or surfaced to a human — never silently shipped, never looped forever.
        _coverage_block = any(f.get("kind") == "missing_file" and f.get("severity") == "blocker"
                              for f in pf["findings"])
        # Stale-gap loop-breaker: if this round's fidelity gaps are IDENTICAL to last round's AND
        # the workspace is byte-identical (per-op content hashes), the loop-back provably changed
        # nothing — looping again just burns rounds re-litigating the same verdict (the Test-8
        # phantom-gap spin). Escalate to the human gate instead. Reviewer blockers (rf.blocking)
        # are deliberately NOT suppressed — a real unfixed blocker keeps its full fix budget.
        try:
            import hashlib as _hl
            _gap_sig = sorted(f"{f.get('kind')}|{f.get('item')}" for f in pf["findings"]
                              if f.get("severity") == "blocker")
            _ws_sig = sorted(f"{o.path}|{getattr(o, 'content_hash', '') or ''}"
                             for o in (_cs_disk.operations or []))
            _fp = _hl.sha256(("\n".join(_gap_sig) + "\n##WS##\n" + "\n".join(_ws_sig)).encode()).hexdigest()
            _prev_fp = (run.handoff_json or {}).get("pf_fp")
            if _gap_sig and _fp == _prev_fp and (beh_gap or _coverage_block):
                beh_gap = False
                _coverage_block = False
                emit_event(db, run.id, "plan_fidelity_stale",
                           {"rounds": rounds, "gaps": len(_gap_sig),
                            "action": "ℹ Fidelity gaps are identical to last round and the workspace "
                                      "is unchanged — the loop-back is not converging; surfacing to "
                                      "human approval instead of looping again"})
            _h = dict(run.handoff_json or {})
            _h["pf_fp"] = _fp
            run.handoff_json = _h
        except Exception as e:  # noqa: BLE001 — loop-breaker must never break the verdict
            logger.debug("plan_fidelity stale-gap check failed: %s", e)
        blocking = rf.blocking or beh_gap or _coverage_block
    except Exception as e:  # noqa: BLE001 — the gate must never break the run
        logger.warning("plan_fidelity gate failed (%s) — keeping reviewer verdict", e)

    # R4 — DETERMINISTIC acceptance predicates (tier-1 completeness). The pure checker verifies the
    # ratified plan's concrete deliverables against the REAL diff (zero false-positive by construction). In
    # ENFORCE mode an UNMET predicate is a RELIABLE blocker — verified by code, not opinion — that loops the
    # run back with PRECISE feedback and blocks the push until satisfied: the deterministic replacement for
    # the LLM completeness blocker that §8.5c correctly demoted to advisory. Shadow (enforce off) only
    # measures. Fail-open: any extractor/checker failure → no predicates → no gate, verdict unchanged.
    if getattr(settings, "agentic_acceptance_predicates", False):
        try:
            from app.agents import acceptance_predicates as AP
            _preds = art.get("acceptance_predicates") or []      # extracted in _phase_code; pure CHECK here
            _apres = AP.check_predicates(_preds, _fidelity_diff_summary(run, _cs_disk)) if _preds else []
            _apmiss = AP.unmet(_apres)
            _apenf = getattr(settings, "agentic_acceptance_predicates_enforce", False)
            _apsum = AP.summarize(_apres)
            art["acceptance_feedback"] = ""   # cleared every round — a stale block must not outlive its miss
            if _apmiss and _apenf:
                # is_bare_token contract: a single-identifier `contains` check false-unmets
                # on a correct implementation under a different (valid) name. The in-loop
                # nudge already treats those as advisory; the ENFORCE gate must too, or a
                # rename-level false positive becomes a must-block order to inject a literal
                # token into correct code. Structural misses keep their full blocking force;
                # bare-token misses stay VISIBLE as warning items (human sees them, agent is
                # told they are naming-sensitive) but cannot block the push or spend a round.
                _ap_hard = [r for r in _apmiss if not AP.is_bare_token(r.predicate)]
                for _r in _ap_hard:          # unmet → deterministic must-block + precise feedback
                    items.append({"category": "completeness", "why": f"{_r.desc} — {_r.evidence}",
                                  "suggested_fix": "Deliver exactly this — it is verified by code, not opinion.",
                                  "file": (_r.predicate.get("file") or _r.predicate.get("path") or ""),
                                  "line": None, "severity": "blocker"})
                for _r in _apmiss:
                    if AP.is_bare_token(_r.predicate):
                        items.append({"category": "completeness",
                                      "why": f"{_r.desc} — {_r.evidence} (advisory — naming-"
                                             "sensitive token check; verify by behaviour, "
                                             "not this literal)",
                                      "suggested_fix": "If this behaviour exists under a different "
                                                       "name, no change is needed; otherwise deliver it.",
                                      "file": (_r.predicate.get("file") or _r.predicate.get("path") or ""),
                                      "line": None, "severity": "warning"})
                items = _cap_items(items)
                has_blocker = any(is_must_block(it.get("category"), it.get("severity")) for it in items)
                if _ap_hard:
                    blocking = True
                    art["acceptance_feedback"] = AP.feedback_block(_ap_hard)
            emit_event(db, run.id, "acceptance_predicates",
                       {**_apsum, "enforce": _apenf, "llm_has_blocker": has_blocker,
                        "action": (f"{'🔒 BLOCK' if (_apmiss and _apenf) else '🧪 shadow'}: acceptance predicates "
                                   f"{_apsum['satisfied']}/{_apsum['total']} satisfied · {_apsum['unmet']} unmet "
                                   f"· {_apsum['unknown']} unknown")})
        except Exception as e:  # noqa: BLE001 — never break the verdict
            logger.debug("acceptance predicates failed: %s", e)

    # WS-CG — DETERMINISTIC CONTRACT gate (LLM-free). Cross-references the FINAL diff against itself + the
    # plan's declared error codes for two runtime-bug classes the reviewer/fidelity/acceptance gates miss
    # because they reason over TEXT: (a) a field READ via .get() that nothing writes, and (b) a switch-side
    # error code declared-but-never-emitted. Both survived six clean review rounds on cbabbf9c. Shadow by
    # default (measure + surface each finding); enforce → deterministic must-block + precise loop-back.
    # C3 extends the gate with static safety checks (publish-before-persist ordering, unplanned shared-file
    # behaviour edits, undeclared money legs, promised-but-unbound config keys) fed by the ratified plan.
    # Fail-open: any checker error → no gate, verdict unchanged.
    if getattr(settings, "agentic_contract_gate", True):
        try:
            from app.agents import contract_gate as CG
            _cg_diff = agentic_review._render_diff(_ws_id(run), _cs_disk, cap=None)   # deterministic contract gate: needs the whole diff, not the reviewer's capped prompt slice
            # Widen the field-write scope to the FULL text of MODIFIED files (new files are already
            # fully in the diff) so a field the change READS that is WRITTEN by unchanged code in the
            # same file is not falsely flagged. Best-effort per file; an unreadable file widens nothing.
            _cg_ws = []
            for _op in (getattr(_cs_disk, "operations", None) or []):
                if getattr(_op, "op", "") == "modify":
                    try:
                        _cg_ws.append((workspace_local.repo_dir(_ws_id(run), _op.repo_id) / _op.path)
                                      .read_text(errors="ignore"))
                    except Exception:  # noqa: BLE001 — widening is best-effort
                        pass
            # Error-code emission is checked ON ANY PATH: declared codes are typically
            # emitted via central NAMED CONSTANTS (e.g. `UdirErrorCodes.TXN_NOT_FOUND`,
            # defined `= "U16"` in an UNCHANGED constants file). Scan the changed repos'
            # existing main-source so a correctly-emitted code isn't flagged "never
            # emitted" just because the raw literal isn't in the diff. Bounded like the
            # DI gate (precision over coverage on a huge repo); fail-open per file.
            _cg_corpus: list[str] = []
            for _rid in {op.repo_id for op in (getattr(_cs_disk, "operations", None) or [])}:
                _root = workspace_local.repo_dir(_ws_id(run), _rid)
                for _i, _p in enumerate(_root.glob("**/src/main/java/**/*.java")):
                    if _i >= 4000:
                        break
                    try:
                        _cg_corpus.append(_p.read_text(errors="ignore"))
                    except Exception:  # noqa: BLE001 — an unreadable file widens nothing
                        pass
            # Finding #4 follow-up — widen check_field_consistency's write-
            # scope from "modified files only" to "the full repo-wide Java
            # corpus" (the SAME _cg_corpus already computed above for
            # check_error_code_emission's corpus_text). This is the exact
            # prerequisite ADR-0004 names for eventually enabling
            # agentic_contract_gate_enforce: a field read by the change but
            # WRITTEN by unchanged code anywhere in the repo (not only in a
            # file the diff touched) is no longer indistinguishable from
            # "nothing writes this field". Flag-gated so it can be shadow-
            # measured independently of the enforce decision — see
            # docs/ARCHITECTURE_REVIEW_REMEDIATION.md §A18 and ADR-0004.
            _cg_write_scope = _cg_ws
            if getattr(settings, "agentic_contract_gate_widen_writer_scope", True):
                _cg_write_scope = _cg_ws + _cg_corpus
            _cg = CG.run_contract_gate(_cg_diff, _declared_error_codes(db, run.change_request_id),
                                       extra_write_text="\n".join(_cg_write_scope),
                                       corpus_text="\n".join(_cg_corpus),
                                       planned_paths={_pf["path"] for _pf in
                                                      _planned_files(db, run.change_request_id, exclude_schema=True)},
                                       directives_text="\n".join(_directives),
                                       plan_text=plan_block)
            _cg_enf = getattr(settings, "agentic_contract_gate_enforce", False)
            if _cg.has_blocker and _cg_enf:
                for _f in _cg.findings:
                    if _f.severity != "blocker":   # C3 warnings stay advisory even under enforce
                        continue
                    items.append({"category": "correctness", "why": f"[{_f.check}] {_f.key}: {_f.detail}",
                                  "suggested_fix": getattr(_f, "suggested_fix", "") or
                                                   ("Write the field on the producing path, or read the field the "
                                                    "producer actually writes." if _f.check == "field_consistency"
                                                    else f'Emit "{_f.key}" on its trigger path (not a substring/placeholder).'),
                                  "file": getattr(_f, "file", "") or "", "line": None, "severity": "blocker"})
                items = _cap_items(items)
                has_blocker = any(is_must_block(it.get("category"), it.get("severity")) for it in items)
                blocking = True
            emit_event(db, run.id, "contract_gate",
                       {**_cg.summary(), "enforce": _cg_enf,
                        # Finding #4 follow-up — records whether THIS round used the
                        # widened (repo-wide) write-scope, so an operator comparing
                        # `contract_gate` events before/after can attribute a drop in
                        # field_consistency findings to the scope widening rather
                        # than guessing. See agentic_contract_gate_widen_writer_scope.
                        "widened_writer_scope": getattr(
                            settings, "agentic_contract_gate_widen_writer_scope", True),
                        "keys": [f"{_f.check}:{_f.key}" for _f in _cg.findings][:15],
                        "action": (f"{'🔒 BLOCK' if (_cg.has_blocker and _cg_enf) else '🧪 shadow'}: contract gate "
                                   f"{_cg.summary()['blockers']} blocker(s)"
                                   + (" — " + ", ".join(f"{_f.check}:{_f.key}" for _f in _cg.findings[:6])
                                      if _cg.findings else ""))})
        except Exception as e:  # noqa: BLE001 — never break the verdict
            logger.debug("contract gate failed: %s", e)

    # Static DI-WIRING gate (Phase 1 context-load) — deterministic, LLM-free. The verify phase never
    # boots a Spring context (`mvn install -DskipTests`), so boot-time wiring failures are invisible
    # to it; this cross-references every injection point the change touched against the bean
    # definitions that actually exist (stereotypes, @Bean methods, legacy XML). Delta-scoped +
    # shadow-first like the contract gate. Fail-open: any error → no gate, verdict unchanged.
    if getattr(settings, "agentic_di_gate", True):
        try:
            from app.agents import di_wiring_gate as DIW
            _di_corpus: list[tuple[str, str]] = []
            _di_cfg: list[tuple[str, str]] = []
            _di_xml: list[tuple[str, str]] = []
            _di_changed: set[str] = set()
            _di_new: set[str] = set()
            _di_ops = [op for op in (getattr(_cs_disk, "operations", None) or [])
                       if op.path.endswith(".java") and "src/test/" not in op.path
                       and getattr(op, "op", "") != "delete"]
            for _op in _di_ops:
                _di_changed.add(f"{_op.repo_id}/{_op.path}")
                if getattr(_op, "op", "") == "add":
                    _di_new.add(f"{_op.repo_id}/{_op.path}")
            _di_capped = False    # corpus bound hit → absence claims below are unsound
            for _rid in {op.repo_id for op in _di_ops}:
                _root = workspace_local.repo_dir(_ws_id(run), _rid)
                for _i, _p in enumerate(_root.glob("**/src/main/java/**/*.java")):
                    if _i >= 4000:                        # bounded corpus — precision over coverage
                        _di_capped = True
                        break
                    try:
                        _di_corpus.append((f"{_rid}/{_p.relative_to(_root)}",
                                           _p.read_text(errors="ignore")))
                    except Exception:  # noqa: BLE001 — an unreadable file widens nothing
                        pass
                for _pat in ("**/src/main/resources/application*.yml",
                             "**/src/main/resources/application*.yaml",
                             "**/src/main/resources/application*.properties",
                             "**/src/main/resources/bootstrap*.yml",
                             "**/src/main/resources/bootstrap*.properties"):
                    for _p in _root.glob(_pat):
                        try:
                            _di_cfg.append((f"{_rid}/{_p.relative_to(_root)}",
                                            _p.read_text(errors="ignore")))
                        except Exception:  # noqa: BLE001
                            pass
                for _i, _p in enumerate(_root.glob("**/src/main/resources/**/*.xml")):
                    if _i >= 300:
                        _di_capped = True
                        break
                    try:
                        _di_xml.append((f"{_rid}/{_p.relative_to(_root)}",
                                        _p.read_text(errors="ignore")))
                    except Exception:  # noqa: BLE001
                        pass
            if _di_changed:
                _di = DIW.run_di_gate(
                    _di_changed, _di_corpus, config_files=_di_cfg, xml_files=_di_xml,
                    new_paths=_di_new,
                    diff_text=agentic_review._render_diff(_ws_id(run), _cs_disk, cap=None))
                _di_enf = getattr(settings, "agentic_di_gate_enforce", False)
                if _di.has_blocker and _di_enf:
                    _promoted = False
                    for _f in _di.findings:
                        if _f.severity != "blocker":      # warnings stay advisory under enforce
                            continue
                        # Absence claims ("no bean exists") over a CAPPED corpus are unsound —
                        # the bean may live in file 4001 / XML 301. Demote those to advisory
                        # instead of blocking a fix loop on a phantom. Presence-based blockers
                        # (injection cycles, found in files actually read) still enforce.
                        if _di_capped and _f.check == "missing_bean":
                            items.append({"category": "correctness",
                                          "why": (f"[{_f.check}] {_f.key}: {_f.detail} "
                                                  "[⚠ corpus was capped — bean may exist in an "
                                                  "unscanned file; verify before acting]"),
                                          "suggested_fix": _f.suggested_fix,
                                          "file": _f.file or "", "line": None, "severity": "warning"})
                            continue
                        _promoted = True
                        items.append({"category": "correctness",
                                      "why": f"[{_f.check}] {_f.key}: {_f.detail}",
                                      "suggested_fix": _f.suggested_fix,
                                      "file": _f.file or "", "line": None, "severity": "blocker"})
                    items = _cap_items(items)
                    has_blocker = any(is_must_block(it.get("category"), it.get("severity")) for it in items)
                    if _promoted:
                        blocking = True
                emit_event(db, run.id, "di_wiring_gate",
                           {**_di.summary(), "enforce": _di_enf, "corpus_capped": _di_capped,
                            "keys": [f"{_f.check}:{_f.key}" for _f in _di.findings][:15],
                            "action": (f"{'🔒 BLOCK' if (_di.has_blocker and _di_enf) else '🧪 shadow'}: "
                                       f"DI wiring gate {_di.summary()['blockers']} blocker(s)"
                                       + (" — " + ", ".join(f"{_f.check}:{_f.key}" for _f in _di.findings[:6])
                                          if _di.findings else ""))})
        except Exception as e:  # noqa: BLE001 — never break the verdict
            logger.debug("di wiring gate failed: %s", e)

    # Cross-module analysis gate (SDLC review gaps 7/8/9/11, deterministic, LLM-free) —
    # the platform's cross-module tools (callers/impact_analysis/symbol_graph) are advisory
    # in-loop (intel_gate_reason only demands SOME structural intel was queried this run,
    # not that it was ABOUT the symbol being edited). This gate cross-references every
    # changed Java method DEFINITION against the run's full intel_queried token set — a
    # signature-shaped edit to a method never checked with callers()/impact_analysis()/
    # symbol_graph() is a blocking finding. Shadow-first, same rollout discipline as the
    # contract/DI gates above. Fail-open: any error → no gate, verdict unchanged.
    if getattr(settings, "agentic_cross_module_gate", True):
        try:
            from app.agents import cross_module_gate as XMG
            _xmg_diff = agentic_review._render_diff(_ws_id(run), _cs_disk, cap=None)
            _xmg = XMG.run_cross_module_gate(
                _xmg_diff, art.get("intel_queried_all") or [],
                min_name_len=getattr(settings, "agentic_cross_module_gate_min_name_len", 4))
            _xmg_enf = getattr(settings, "agentic_cross_module_gate_enforce", False)
            if _xmg.has_blocker and _xmg_enf:
                for _f in _xmg.findings:
                    if _f.severity != "blocker":
                        continue
                    items.append({"category": "correctness", "why": f"[{_f.check}] {_f.key}: {_f.detail}",
                                  "suggested_fix": _f.suggested_fix,
                                  "file": _f.file or "", "line": None, "severity": "blocker"})
                items = _cap_items(items)
                has_blocker = any(is_must_block(it.get("category"), it.get("severity")) for it in items)
                blocking = True
            emit_event(db, run.id, "cross_module_gate",
                       {**_xmg.summary(), "enforce": _xmg_enf,
                        "intel_queried_count": len(art.get("intel_queried_all") or []),
                        "keys": [f"{_f.check}:{_f.key}" for _f in _xmg.findings][:15],
                        "action": (f"{'🔒 BLOCK' if (_xmg.has_blocker and _xmg_enf) else '🧪 shadow'}: "
                                   f"cross-module gate {_xmg.summary()['blockers']} blocker(s)"
                                   + (" — " + ", ".join(f"{_f.check}:{_f.key}" for _f in _xmg.findings[:6])
                                      if _xmg.findings else ""))})
        except Exception as e:  # noqa: BLE001 — never break the verdict
            logger.debug("cross-module gate failed: %s", e)

    # WS3b — shared-validator widening (deterministic, SELF-HEAL). A changed shared helper called by ≥2
    # other validators has widened behaviour for message types this change never targeted. Loop the code
    # agent back with a precise remediation directive: category 'correctness'/severity 'error' → blocking
    # but NOT must-block, so it self-heals in-loop and is NEVER escalated to a human (if still unhealed
    # after the review-round budget it downgrades to an advisory banner). Fail-open.
    try:
        _sv = _shared_validator_widening(db, run, _cs_disk)
        if _sv:
            items.extend(_sv)
            items = _cap_items(items)
            blocking = True
            emit_event(db, run.id, "shared_validator_widening",
                       {"findings": len(_sv), "files": [f.get("file") for f in _sv][:5],
                        "action": (f"🔁 {len(_sv)} shared-validator widening finding(s) — looping back to "
                                   "isolate the change (self-heal; not escalated to a human)")})
    except Exception as e:  # noqa: BLE001 — never break the verdict
        logger.debug("shared-validator widening gate failed: %s", e)

    # Document↔CODE consistency (post-codegen) — runs only on an otherwise-CLEAN round so it audits the
    # FINAL diff. The CODE is the source of truth now: a TSD over-claim (a column/config/error-code the
    # code doesn't build) means the DOC is wrong → reconcile the TSD to the code and re-persist it
    # (non-blocking). A `code_missing` finding (code lacks a plan+TSD-required behaviour) is a real gap →
    # re-block so the agent fixes it. Fail-open: never break a code run on the checker.
    if not blocking and getattr(settings, "agentic_doc_code_gate", True):
        try:
            from app.agents import doc_code_consistency as DCC
            _tsd = _latest_tsd(db, run.change_request_id)
            if _tsd is not None and (getattr(_tsd, "content", None) or "").strip():
                _diff = _fidelity_diff_summary(run, _cs_disk)
                _dc = await DCC.check_doc_against_code(
                    tsd_content=_tsd.content, diff_text=_diff,
                    plan_contract=_analysis_plan_block(db, run.change_request_id))
                _gaps = DCC.code_gap_findings(_dc)
                _fabr = DCC.doc_fabrication_findings(_dc)
                for _g in _gaps:                              # real code gap → review/blocking path
                    items.append({"category": "correctness",
                                  "why": f"code missing vs TSD/plan: {_g.get('item')} — {_g.get('detail')}",
                                  "suggested_fix": "Implement the behaviour the TSD and ratified plan require.",
                                  "file": "", "line": None, "severity": "blocker"})
                if _gaps:
                    items = _cap_items(items)
                    has_blocker = any(is_must_block(it.get("category"), it.get("severity")) for it in items)
                    blocking = True
                _reconciled = False
                if _fabr:                                    # doc over-claim → reconcile TSD to code
                    _new = await DCC.reconcile_doc_to_code(
                        tsd_content=_tsd.content, diff_text=_diff,
                        instruction=DCC.repair_instruction(_fabr))
                    if _new and _new.strip() and _new != _tsd.content:
                        _tsd.content, _tsd.version = _new, (_tsd.version or 1) + 1
                        if db is not None:
                            db.add(_tsd)                     # outer phase commit persists it
                        _reconciled = True
                if _gaps or _fabr:
                    emit_event(db, run.id, "doc_code_reconciled",
                               {"code_gaps": len(_gaps), "doc_overclaims": len(_fabr), "reconciled": _reconciled,
                                "overclaims": [{"kind": f.get("kind"), "item": f.get("item")} for f in _fabr][:10],
                                "action": (f"📝 TSD reconciled to code — {len(_fabr)} over-claim(s) corrected"
                                           if _reconciled else
                                           (f"🔒 {len(_gaps)} code gap(s) vs TSD → loop back" if _gaps
                                            else "TSD matches code"))})
        except Exception as e:  # noqa: BLE001 — never break the verdict
            logger.debug("doc_code consistency gate failed: %s", e)

    # TSD-derived test coverage gate (SDLC review gap 10, shadow-first) — extracts checkable
    # assertions from the APPROVED TSD (API contracts, error codes, state transitions,
    # validation rules, config behaviour) and measures how many are referenced by a
    # `tsd-ref:` marker in this change's test files. Runs only on an otherwise-clean round
    # (same guard as doc_code consistency) so it measures the FINAL diff. Fail-open: never
    # break a code run on the checker.
    #
    # The AUTHORING side now exists (SDLC-A22, 2026-08-25): the assertion list is
    # handed to the code agent earlier in this phase with the exact `tsd-ref`
    # marker to copy for each, and the grading below reuses that same list. Before
    # that, the agent was asked for markers it had to invent, so this number
    # measured luck rather than test quality and could not responsibly be
    # enforced. It still defaults to shadow — the flip should follow observed
    # coverage on real runs, the same evidence-gating discipline ADR-0004 applies
    # to the contract gate.
    if not blocking and getattr(settings, "agentic_tsd_test_coverage_gate", True):
        try:
            from app.agents import tsd_test_generator as TTG
            _tsd_for_tests = _latest_tsd(db, run.change_request_id)
            _behavioural_ops = [op for op in (getattr(_cs_disk, "operations", None) or [])
                                if getattr(op, "op", "") in ("add", "modify")
                                and verification_plan._is_behavioural_src(getattr(op, "path", "") or "")]
            if (_tsd_for_tests is not None and (getattr(_tsd_for_tests, "content", None) or "").strip()
                    and _behavioural_ops):
                _test_sources = [op.content for op in (getattr(_cs_disk, "operations", None) or [])
                                 if getattr(op, "content", None)
                                 and verification_plan._is_test_path(getattr(op, "path", "") or "")]
                # Reuse the assertions already extracted for the AUTHORING prompt
                # (SDLC-A22) when they are available on this run's artefacts —
                # one LLM call per phase instead of two, and, more importantly,
                # it guarantees the agent is graded against EXACTLY the list it
                # was shown. Grading against a second, independently-extracted
                # list would reintroduce the mismatch this pass fixed.
                _assertions = art.get("tsd_assertions") or []
                if not _assertions:
                    _assertions = await TTG.extract_tsd_assertions(_tsd_for_tests.content)
                _cov = TTG.assertion_coverage(_assertions, _test_sources)
                _min_ratio = float(getattr(settings, "agentic_tsd_test_coverage_min_ratio", 0.5))
                _cov_enf = getattr(settings, "agentic_tsd_test_coverage_gate_enforce", False)
                _below = _cov["total"] > 0 and _cov["coverage_ratio"] < _min_ratio
                if _below and _cov_enf:
                    _missing = "; ".join(a.get("title") or a.get("description", "")
                                         for a in _cov["uncovered"][:5])
                    items.append({"category": "correctness",
                                  "why": (f"TSD test coverage {_cov['covered']}/{_cov['total']} "
                                          f"({_cov['coverage_ratio']:.0%}) is below the required "
                                          f"{_min_ratio:.0%} — uncovered: {_missing}"),
                                  "suggested_fix": ('Add a test per uncovered TSD assertion, citing it '
                                                    'with a "// tsd-ref: <section-or-id>" comment so '
                                                    'coverage is measurable.'),
                                  "file": "", "line": None, "severity": "blocker"})
                    items = _cap_items(items)
                    has_blocker = any(is_must_block(it.get("category"), it.get("severity")) for it in items)
                    blocking = True
                emit_event(db, run.id, "tsd_test_coverage_gate",
                           {"total": _cov["total"], "covered": _cov["covered"],
                            "coverage_ratio": round(_cov["coverage_ratio"], 3), "enforce": _cov_enf,
                            "min_ratio": _min_ratio,
                            "action": (f"{'🔒 BLOCK' if (_below and _cov_enf) else '🧪 shadow'}: "
                                       f"TSD test coverage {_cov['covered']}/{_cov['total']} "
                                       f"({_cov['coverage_ratio']:.0%})")})
        except Exception as e:  # noqa: BLE001 — never break the verdict
            logger.debug("tsd test coverage gate failed: %s", e)

    # R5 + R2 — converge-or-escalate by finding IDENTITY. Track the SET of OPEN must-block findings by a
    # stable key (file basename + category) across rounds in the durable progress ledger, and escalate to
    # the human gate only when the SAME blocker has stayed open across the window. Distinct new blockers
    # each round mean the agent IS making progress (resolving old, surfacing new) — so we keep going; this
    # is more precise than a raw count, which would escalate prematurely on churn. A finding is NEVER
    # auto-marked "resolved" here — the reviewer re-judges the current code every round, so no real issue
    # is dropped; identity is used ONLY to detect a STUCK blocker. Blockers stay flagged via has_blocker,
    # so escalation surfaces them to the human; it never silently ships them. Absolute caps still apply.
    escalated = False
    if blocking and getattr(settings, "agentic_max_stall_rounds", 0) > 0:
        def _fkey(it):   # basename + category — stable enough across rounds, tolerant of path-prefix churn
            f = (it.get("file") or "").rsplit("/", 1)[-1].lower()
            return f"{f}|{(it.get('category') or '').lower()}"
        open_keys = sorted({_fkey(it) for it in items if is_must_block(it.get("category"), it.get("severity"))})
        # Blocker-key history is persisted to progress_ledger_json so a resumed run
        # keeps its stall window (this is default-on stall escalation, gated only by
        # agentic_max_stall_rounds > 0 — NOT by agentic_progress_ledger). The column
        # is created unconditionally by migration 0090, so this write is always safe.
        led = dict(getattr(run, "progress_ledger_json", None) or {})
        hist = (list(led.get("blocker_keys") or []) + [open_keys])[-6:]         # durable across resume
        led["blocker_keys"] = hist
        run.progress_ledger_json = led
        if db is not None:
            db.add(run)
        K = settings.agentic_max_stall_rounds
        recent = hist[-K:]                          # the SAME blocker present in ALL of the last K rounds
        stuck = set.intersection(*(set(r) for r in recent)) if len(recent) >= K else set()
        if stuck:
            blocking = False
            escalated = True
            emit_event(db, run.id, "review_stalled",
                       {"stuck_blockers": sorted(stuck), "rounds": K,
                        "action": f"⚠ {len(stuck)} blocking finding(s) unresolved across {K} round(s) "
                                  "— escalating to human review instead of looping further"})

    # Parse-stall loop-breaker: an unparseable reviewer verdict synthesizes [Dn] NOT-VERIFIED
    # blockers — PLUMBING findings the code agent cannot fix, so redispatching it burns full
    # code+review rounds changing nothing (run b060dc2a spent its entire blocker budget this
    # way). One unparseable round gets one loop-back (the in-review salvage usually clears
    # it); two in a row escalate to the human gate like any other stall.
    _unparseable = any(f.why == agentic_review.UNPARSEABLE_WHY for f in rf.findings)
    art["review_parse_failures"] = (art.get("review_parse_failures", 0) + 1) if _unparseable else 0
    if _unparseable and art["review_parse_failures"] >= 2 and blocking:
        blocking = False
        escalated = True
        emit_event(db, run.id, "review_stalled",
                   {"parse_failures": art["review_parse_failures"],
                    "action": "⚠ Reviewer verdict unparseable for 2 consecutive rounds — a "
                              "review-plumbing failure, not a code failure; escalating to human "
                              "review instead of redispatching the code agent"})

    # "Couldn't check ≠ checked & fine": a reviewer-plumbing failure (unparseable verdict, salvage
    # failed) leaves reviewer_gaps but — with no critical directives / gates firing — no blocking
    # `items`, so has_blocker would be False and the run would freeze PUSH-READY, indistinguishable
    # from a genuinely clean review. HOLD the push for human adjudication instead. The push gate
    # (_unresolved_blockers) keys off a must-block ITEM, not the flag alone, so we inject one; we do
    # NOT set `blocking` (a missing verdict is not code work — it must NOT redispatch the code agent,
    # it parks at the approval gate with the push blocked).
    if getattr(rf, "reviewer_gaps", None) and not has_blocker:
        items = list(items) + [{
            "category": "review", "severity": "blocker", "file": None, "line": None, "blocking": True,
            "why": ("Reviewer verdict could not be parsed/verified this round — a MISSING review, "
                    "not a clean one. Adjudicate manually before shipping."),
        }]
        has_blocker = True
    art["review"] = {"blocking": blocking, "findings": len(rf.findings) + pf_gaps,
                     "reviewer_model": rf.reviewer_model, "items": items, "notes": notes,
                     "has_blocker": has_blocker, "plan_fidelity_gaps": pf_gaps, "escalated": escalated,
                     "dropped_must_block": _dropped_mb}
    emit_event(db, run.id, "review", {"blocking": blocking, "findings": len(rf.findings) + pf_gaps,
                                      "has_blocker": has_blocker, "non_blocking": len(notes),
                                      "reviewer_model": rf.reviewer_model,
                                      "reviewer_gaps": len(getattr(rf, "reviewer_gaps", []) or [])})
    if getattr(rf, "reviewer_gaps", None):
        emit_event(db, run.id, "review_gaps",
                   {"count": len(rf.reviewer_gaps),
                    "action": (f"⚠ {len(rf.reviewer_gaps)} verdict deficienc(ies) survived the "
                               "corrective judge retry — held for human adjudication, NOT sent "
                               "to the code agent")})
    return blocking


def _phase_freeze(db, run: AgenticRun, art: dict) -> None:
    # Prefer the branch PROVISIONING created (handoff-persisted, resume-safe): the
    # tree already lives on it, so the manifest must name the same branch.
    art["branch"] = (art.get("branch") or (run.handoff_json or {}).get("feature_branch")
                     or agentic_push.branch_name(_title(db, run)))
    per_repo = [{"repo_id": rid, "base_commit_sha": sha, "shared_branch_name": art["branch"]}
                for rid, sha in art["repo_base_sha"].items()]
    man = M.build_manifest(
        selected_repo_ids=run.selected_repo_ids, per_repo=per_repo, change_set=_disk_change_set(db, run),
        verification=art.get("verification", {}), review=art.get("review", {}),
        plan=_analysis_plan_block(db, run.change_request_id))   # ratified spec → re-approve if it changes
    M.freeze_manifest(db, run.id, man, _capture_diffs(db, run))   # durable changes artifact
    run.manifest_hash = man["manifest_hash"]
    emit_event(db, run.id, "manifest_frozen", {"manifest_hash": man["manifest_hash"], "branch": art["branch"]})


def _phase_freeze_xsd(db, run: AgenticRun, art: dict, *, build_status: str = "verified") -> None:
    """Phase A (kind='xsd') gate: freeze an XSD-only manifest so the human approves
    an exact hash, and persist the Phase A→B handoff (XsdScope + full XSD contents).

    The change-set here is naturally XSD-only (Phase A only edits .xsd/.xjb). Phase B
    re-freezes the FULL manifest (XSD + Java) from the shared tree at its own gate, so
    the single push carries both — this manifest is purely the schema-review gate.

    ``build_status`` (the authoritative schema build result) words the handoff-ready
    banner: a needs_fix schema must not read "approve to start code generation"."""
    art["branch"] = (art.get("branch") or (run.handoff_json or {}).get("feature_branch")
                     or agentic_push.branch_name(_title(db, run)))
    per_repo = [{"repo_id": rid, "base_commit_sha": sha, "shared_branch_name": art["branch"]}
                for rid, sha in art["repo_base_sha"].items()]
    cs = _disk_change_set(db, run)
    man = M.build_manifest(
        selected_repo_ids=run.selected_repo_ids, per_repo=per_repo, change_set=cs,
        verification=art.get("verification", {}), review={})
    M.freeze_manifest(db, run.id, man, _capture_diffs(db, run))   # durable changes artifact
    run.manifest_hash = man["manifest_hash"]
    scope = art.get("xsd_scope")
    xsd_files = [{"repo_id": op.repo_id, "path": op.path, "content": op.content}
                 for op in cs.operations if op.content is not None]
    # MERGE (don't overwrite) so the approach_decision recorded at the gate survives.
    h = dict(run.handoff_json or {})
    h["xsd_scope"] = agentic_subagents.xsd_scope_to_dict(scope) if scope else {}
    h["xsd_files"] = xsd_files
    run.handoff_json = h
    emit_event(db, run.id, "manifest_frozen",
               {"manifest_hash": man["manifest_hash"], "branch": art["branch"], "kind": "xsd",
                "accepted_risk": bool(h.get("accepted_risk"))})
    if build_status == "needs_fix":
        emit_event(db, run.id, "xsd_handoff_ready",
                   {"files": len(xsd_files), "build_status": build_status,
                    "action": f"⚠ {len(xsd_files)} schema file(s) frozen, but the schema does NOT build "
                              "— fix via 'request XSD changes' before approving"})
    else:
        emit_event(db, run.id, "xsd_handoff_ready",
                   {"files": len(xsd_files), "build_status": build_status,
                    "action": f"📦 XSDs ready for review — {len(xsd_files)} file(s); approve to start code generation"})


def _post_xsd_advance(db, run: AgenticRun, art: dict) -> None:
    """After the XSD apply pass: if the refine pass stopped to CONVERSE (the human's
    requested change was disruptive → safer alternatives proposed), gate for their
    decision. Otherwise Phase A (xsd) stops for human schema review; a "full" run
    (the standalone quick-start codegen console — no BRD/TSD in its flow) chains
    straight to code without the ADR-0005 TSD gate, which is scoped to the
    Phase-A→Phase-B contract this run type never has."""
    scope = art.get("xsd_scope")
    rp = getattr(scope, "revision_proposal", None) if scope else None
    if rp:
        original = ((run.handoff_json or {}).get("xsd_change_request") or {}).get("feedback", "")
        emit_event(db, run.id, "revision_proposal",
                   {**rp, "original_request": original, "action":
                    "⚠ Your requested change is disruptive — pick a safer alternative, or accept the risk"})
        S.advance(db, run, P.AWAITING_APPROACH_DECISION)
        return
    if (run.kind or "full") == "xsd":
        # Authoritative schema build BEFORE the human approves: compile the changed
        # XSD/domain module(s) (JAXB generate + javac) and `mvn install` core to ~/.m2
        # so the regenerated artifact is resolvable by Phase B. Scoped to core
        # (app_blast_radius=False) — consumer rebuild is Phase B's gate.
        status = _phase_verify(db, run, art, app_blast_radius=False)
        # GATE on the result — it was previously DISCARDED, so a schema that fails
        # JAXB-generate/javac/install still froze and read "ready to approve", and the
        # human could approve a non-compiling contract that Phase B then generates code
        # against. Only a REAL failure (needs_fix) blocks; `unverified` (no toolchain /
        # can't build) is a can't-check, not a failure, and does NOT gate — the same
        # addressee rule as the Phase-B verify path. Write the flag every pass so a later
        # passing build (after request-xsd-changes) clears it.
        build_failed = status == "needs_fix"
        h = dict(run.handoff_json or {}); h["xsd_build_failed"] = build_failed; run.handoff_json = h
        _phase_freeze_xsd(db, run, art, build_status=status)
        if build_failed:
            errs = (art.get("verification") or {}).get("errors") or []
            emit_event(db, run.id, "xsd_build_failed",
                       {"errors": errs[:10], "status": status,
                        "action": "⛔ The generated schema does NOT build (JAXB generate/compile) — "
                                  "approval is BLOCKED. Fix it via 'request XSD changes', or approve "
                                  "with override to accept a non-building schema."})
        S.advance(db, run, P.AWAITING_XSD_APPROVAL)
    else:
        # A "full" run is the standalone quick-start codegen console (POST /agentic/quick-start,
        # the "Agentic Codegen" admin page) — "chat the change, edit real code, no BRD/TSD needed"
        # by design (see that endpoint's own docstring). It has no BRD/TSD step anywhere in its
        # flow, so unlike the Phase-B path it must NOT go through the ADR-0005 TSD approval gate:
        # `_tsd_approval_gate` would find `ts is None` for every such run and, once enforcement is
        # turned on, permanently wedge it at AWAITING_TSD_APPROVAL with no TSD a human could ever
        # approve. Skip straight to CODE_CHANGE, the same way the `kind == "xsd"` branch above
        # never touches this gate either — the gate exists for the Phase-A→Phase-B contract, not
        # for a run that was never given a TSD to begin with.
        S.advance(db, run, P.CODE_CHANGE)


def _title(db, run) -> str:
    from app.models.change_request import ChangeRequest
    cr = db.get(ChangeRequest, run.change_request_id)
    return (cr.title or cr.initial_prompt or "change") if cr else "change"


# ── Driver ────────────────────────────────────────────────────────────────────

_CTX_PHASES = {P.CONTEXT_READY.value, P.XSD_DISCOVERY.value, P.CODE_CHANGE.value,
               P.VERIFICATION.value, P.REVIEW.value, P.AWAITING_HUMAN_APPROVAL.value,
               # Change-Analysis (S2): the analysis loop resumes at ANALYZING after
               # the clarifications gate in a fresh worker → ctx must be rebuilt.
               P.ANALYZING.value}
_CHANGESET_PHASES = {P.VERIFICATION.value, P.REVIEW.value, P.AWAITING_HUMAN_APPROVAL.value}


def _is_transient(e: Exception) -> bool:
    """Infra/network errors that should PAUSE-and-resume rather than fail the run —
    a network blip, API timeout/5xx, rate limit, or a dropped connection (like Claude
    Code continuing after a network issue). Real logic/build errors are NOT transient."""
    name = type(e).__name__.lower()
    if any(k in name for k in ("connection", "timeout", "ratelimit", "internalserver",
                               "serviceunavailable", "overloaded", "apiconnection", "apitimeout")):
        return True
    s = (str(e) or "").lower()
    return any(k in s for k in ("timed out", "timeout", "connection reset", "connection aborted",
                                "temporarily unavailable", "network is unreachable", "overloaded",
                                "rate limit", "could not connect", "connection refused"))


def _error_code(e: Exception) -> str:
    """Coarse failure category for ops triage (stored on the run). Lets an operator
    tell 'Anthropic rate-limited' from 'git clone failed' from 'verify timed out'
    without grepping the coding-log JSONL."""
    name = type(e).__name__.lower()
    s = (str(e) or "").lower()
    if "rate" in name + s and "limit" in name + s:
        return "llm_rate_limit"
    if any(k in name for k in ("apiconnection", "apitimeout", "overloaded", "internalserver",
                               "serviceunavailable")) or "anthropic" in s or "overloaded" in s:
        return "llm_unavailable"
    if "clone" in s or "ls-remote" in s:
        return "git_clone_failed"
    if "push" in s or "git-guard" in s or "gitguard" in s:
        return "git_push_failed"
    if "workspace" in name or "insufficient workspace disk" in s:
        return "workspace_error"
    if "timed out" in s or "timeout" in name:
        return "timeout"
    return "internal_error"


def _rehydrate_art(db, run: AgenticRun, art: dict) -> None:
    """RESUME-safety (§3): a recovered run runs in a FRESH worker with an empty
    ``art``, but phases past workspace depend on art['ctx'] / art['repo_base_sha'] /
    art['change_set']. Rebuild them idempotently from the persisted clone + DB so a
    resume continues instead of KeyError-ing. No-op on the normal path (phases
    populate art themselves; every fill below is guarded on a missing key)."""
    # Phase B (code): restore the schema decisions Phase A made (advisory context for
    # the code agent) from the parent's persisted handoff — no re-discovery, no gap.
    if getattr(run, "kind", "full") == "code" and "xsd_scope" not in art and getattr(run, "parent_run_id", None):
        parent = db.get(AgenticRun, run.parent_run_id)
        if parent and parent.handoff_json:
            art["xsd_scope"] = agentic_subagents.xsd_scope_from_dict(parent.handoff_json.get("xsd_scope"))
    if run.phase in (_CTX_PHASES | {P.WORKSPACE_READY.value}) and "repo_base_sha" not in art:
        try:
            art["repo_base_sha"] = {rid: workspace_local.read_base_sha(_ws_id(run), rid)
                                    for rid in (run.selected_repo_ids or [])}
        except Exception as e:  # noqa: BLE001
            logger.warning("rehydrate base_sha failed for %s: %s", run.id, e)
            art["repo_base_sha"] = {}
    if run.phase in _CTX_PHASES and "ctx" not in art:
        art["ctx"] = context_assembler.assemble_context_pack(
            db, change_request_id=run.change_request_id, selected_repo_ids=run.selected_repo_ids,
            repo_base_sha=art.get("repo_base_sha"), run_id=run.id, intent=art.get("intent", ""),
            tsd_version_locked=getattr(run, "tsd_version_locked", None))
    if run.phase in _CHANGESET_PHASES and "change_set" not in art:
        art["change_set"] = _disk_change_set(db, run)


def _start_heartbeat(run_id: str, owner: str):
    """Background daemon that renews the lease every ttl/3 for the WHOLE drive
    lifetime — covering blocking work (a multi-minute `mvn` verify, a slow/retried
    LLM call) where the per-iteration renew never gets a chance to fire. Without it
    the 300s lease expires mid-build and the recovery beat double-drives the run on
    the same workspace. Uses its own short-lived DB sessions (thread-safe). Returns
    ``(stop, lost)`` threading.Events — set ``stop`` to end the thread; ``lost`` is
    SET BY the thread when a renew shows this driver no longer owns the lease (the
    run was reclaimed while we were frozen/blocked). The drive loop and the phase
    cancel_checks treat ``lost`` as an abort signal: a stale driver that keeps
    going emits events concurrently with the new driver and collides on the
    (run_id, seq) unique constraint — the zombie double-drive that FAILED a healthy
    run (seq 625, run 97eb4aaa)."""
    import threading
    from app.core.database import SessionLocal
    from app.models.base import utcnow

    import time as _time
    stop = threading.Event()
    lost = threading.Event()
    period = max(20, settings.agentic_lease_ttl_seconds // 3)
    max_lifetime = 3600 * 4

    def _beat():
        started = _time.monotonic()
        while not stop.wait(period):
            if _time.monotonic() - started > max_lifetime:
                logger.warning("heartbeat thread max lifetime reached for run %s", run_id)
                break
            s = SessionLocal()
            try:
                renewed = S.renew_lease(s, run_id, owner)
                if not renewed and not stop.is_set():
                    # rowcount 0 ⇒ we no longer own the lease (reclaimed/terminal). A
                    # second driver may now be on this run — signal THIS driver to
                    # abort (it is the zombie); merely warning let it keep emitting
                    # events until the seq collision failed the run (§3).
                    lost.set()
                    logger.warning("heartbeat: run=%s lease NO LONGER OWNED by %s — another worker may be "
                                   "driving it; signalling this driver to abort", run_id, owner)
                r = s.get(AgenticRun, run_id)
                if r is not None:
                    r.last_heartbeat_at = utcnow()
                s.commit()
            except Exception:  # noqa: BLE001 — a heartbeat blip must never kill the run
                # WARNING, not debug: silent renew failures are how a frozen driver's lease
                # expires invisibly (run 97eb4aaa left no trace of WHY its renewals stopped).
                logger.warning("heartbeat: run=%s renew failed (will retry)", run_id, exc_info=True)
                s.rollback()
            finally:
                s.close()

    t = threading.Thread(target=_beat, name=f"hb-{run_id[:8]}", daemon=True)
    t.start()
    return stop, lost


def _lease_lost_set(art: dict) -> bool:
    """True when this driver's heartbeat discovered the lease was reclaimed — used by the
    phase cancel_checks so a superseded (zombie) driver stops mid-phase instead of racing
    the new driver on event seq / workspace writes."""
    ev = art.get("_lease_lost")
    return bool(ev is not None and ev.is_set())


async def drive_run(db, run_id: str, *, owner: str = "orchestrator", model=None,
                    intent: str = "") -> dict:
    """Drive a run from its current phase to ``awaiting_human_approval`` (or a
    terminal state). Lease-guarded; cooperative cancel at each phase boundary."""
    import uuid as _uuid
    # Unique per-driver owner: with every worker sharing the literal "orchestrator", a
    # superseded driver's renew/release still matched the row — a zombie could silently
    # co-own (and stomp) the lease of the driver that legitimately reclaimed the run.
    owner = f"{owner}:{_uuid.uuid4().hex[:8]}"
    if not S.acquire_lease(db, run_id, owner):
        logger.info("drive_run: lease NOT acquired for run=%s (held by another worker)", run_id)
        return {"acquired": False}
    db.commit()
    run0 = db.get(AgenticRun, run_id)
    logger.info("drive_run: lease acquired run=%s phase=%s kind=%s",
                run_id, getattr(run0, "phase", "?"), getattr(run0, "kind", "?"))
    # Tag every LLM call made during this drive with the owning change/run/kind so the Usage
    # dashboard can attribute spend per-change → per-phase. Reset in the finally so the worker's
    # async context doesn't leak this onto an unrelated task.
    from app.core.observability import set_usage_context, reset_usage_context
    _usage_tok = set_usage_context(
        change_request_id=getattr(run0, "change_request_id", None),
        run_id=run_id, kind=getattr(run0, "kind", None))
    # First COMMITTED signal that a worker has the run. Phase bodies commit their
    # events at the phase boundary (the agent-loop phases commit live; the workspace
    # phase did not), so without this a long first phase — clone + LLM-backed indexing
    # — leaves the UI frozen on the pre-committed `run_created`. This makes "worker is
    # working" distinguishable from "task never ran" (§3).
    emit_event(db, run_id, "drive_started",
               {"phase": getattr(run0, "phase", None),
                "action": "⚙ Worker picked up the run — starting"})
    db.commit()
    _hb_stop, _lease_lost = _start_heartbeat(run_id, owner)
    if not intent:                                       # re-dispatch (resume/recovery) → recover the goal
        run0 = db.get(AgenticRun, run_id)
        if run0 and run0.change_request_id:
            from app.models.change_request import ChangeRequest
            cr = db.get(ChangeRequest, run0.change_request_id)
            # Same authority order as the initial dispatch (api/agentic.py — enhanced_prompt
            # first): recovering from initial_prompt alone silently downgraded every
            # resume/recovery/human-gate redispatch to the raw first-draft ask.
            intent = ((cr.enhanced_prompt if cr else "") or (cr.initial_prompt if cr else "")
                      or (cr.title if cr else "") or "")
    art: dict = {"intent": intent, "_owner": owner, "_lease_lost": _lease_lost}
    handoff_push = False
    try:
        import time as _time
        # Worker-host dependency preflight (human-readable). The API preflights at start,
        # but a worker on a DIFFERENT host — or a dependency that broke after start (git /
        # GITLAB_TOKEN / LLM key gone) — is caught here: record a PLAIN-LANGUAGE reason on
        # the run instead of failing deep in a phase with a cryptic trace. We ARE the
        # worker, so don't ping ourselves (include_worker=False).
        from app.agents import codegen_preflight
        _problems = codegen_preflight.check_dependencies(include_worker=False)
        if _problems:
            _msg = "Code generation cannot run on this worker:\n" + "\n".join(f"• {p}" for p in _problems)
            logger.error("drive_run: run=%s PREFLIGHT FAILED —\n%s", run_id, _msg)
            run = db.get(AgenticRun, run_id)
            run.error_code = "dependency_missing"
            emit_event(db, run_id, "preflight_failed",
                       {"problems": _problems, "action": "⛔ " + _problems[0]})
            S.mark_terminal(db, run, AgenticStatus.FAILED, error=_msg)
            db.commit()
            return {"acquired": True, "phase": run.phase, "status": run.status}
        _max_phase_iters = 50
        for _phase_iter in range(_max_phase_iters):
            run = db.get(AgenticRun, run_id)
            db.refresh(run)
            if run.status in {s.value for s in TERMINAL_STATUSES}:
                logger.info("drive_run: run=%s already terminal (status=%s phase=%s) — stopping loop",
                            run_id, run.status, run.phase)
                break
            if S.check_cancel(run):
                logger.info("drive_run: run=%s cancel_requested at phase=%s — marking cancelled", run_id, run.phase)
                S.mark_terminal(db, run, AgenticStatus.CANCELLED)
                db.commit()
                break
            if _lease_lost.is_set():
                # We were reclaimed (frozen past the TTL, then woke up). Another driver now
                # owns this run — abort WITHOUT touching the row: continuing would emit
                # events in parallel with the new driver and fail the run on the
                # (run_id, seq) unique constraint (observed: seq 625, run 97eb4aaa).
                logger.error("drive_run: run=%s LEASE LOST mid-drive (owner=%s superseded) — "
                             "aborting this driver; the new owner continues the run", run_id, owner)
                return {"acquired": False, "lease_lost": True}
            if run.phase in (P.AWAITING_HUMAN_APPROVAL.value, P.AWAITING_XSD_APPROVAL.value,
                             P.AWAITING_APPROACH_DECISION.value, P.AWAITING_VERIFY_DECISION.value,
                             P.AWAITING_CLARIFICATIONS.value, P.AWAITING_PLAN_APPROVAL.value,
                             P.AWAITING_CODE_DECISION.value):
                logger.info("drive_run: run=%s parked at human gate phase=%s — releasing lease, awaiting decision",
                            run_id, run.phase)
                break                                    # wait for the human (approach / XSD / code / analysis gate)
            if run.phase in (P.PUSHING.value, P.REBASE_REVERIFY.value):
                # Approval already happened — the push is driven by push_run (a separate
                # task), NOT the phase loop (_step has no pushing handler). Hand off: drop
                # our lease (in `finally`) and dispatch the push so a resumed/recovered
                # pushing run completes instead of RuntimeError-ing on an unknown phase.
                logger.info("drive_run: run=%s phase=%s → handing off to push task", run_id, run.phase)
                handoff_push = True
                break
            # Resume-safety: a post-workspace phase whose clone is GONE from disk (the
            # hourly GC collects a parent tree once its child goes terminal — a resume
            # 2 minutes later then drives file tools against a void: every read_file /
            # glob "fails", the agent burns iterations rediscovering it, and parks at a
            # gate). Restart from PENDING so _phase_workspace re-provisions (re-clone +
            # approved-XSD restore) before the working phase re-enters.
            if run.phase != P.PENDING.value and any(
                    not workspace_local.repo_dir(_ws_id(run), rid).exists()
                    for rid in (run.selected_repo_ids or [])):
                emit_event(db, run.id, "workspace_missing",
                           {"phase": run.phase,
                            "action": f"📦 Workspace clone(s) missing at phase {run.phase} — "
                                      "re-provisioning, then resuming"})
                run.phase = P.PENDING.value
                db.commit()
            _rehydrate_art(db, run, art)                 # resume-safe: rebuild art a fresh worker lacks
            _phase_before = run.phase
            _step_t0 = _time.monotonic()
            await _step(db, run, art, model)
            if _lease_lost.is_set():
                # The lease was reclaimed WHILE this phase ran (we froze past the TTL and a new
                # driver took over). _step post-processing may have queued an advance / mark_terminal
                # on the row we no longer own — and mark_terminal does NOT commit, so it is still
                # pending here. Roll it back so this superseded (zombie) driver never persists a
                # terminal/phase transition onto the healthy new owner's run — committing FAILED
                # here is the exact double-drive that killed a healthy run (§3). The new owner
                # continues; we exit quietly (lease release in `finally` is a no-op — we don't own it).
                logger.error("drive_run: run=%s LEASE LOST during phase %s — discarding this driver's "
                             "phase writes; the new owner continues the run", run_id, _phase_before)
                db.rollback()
                return {"acquired": False, "lease_lost": True}
            run = db.get(AgenticRun, run_id)
            logger.info("drive_run: run=%s phase %s → %s in %dms (iter %d/%d)",
                        run_id, _phase_before, run.phase, int((_time.monotonic() - _step_t0) * 1000),
                        _phase_iter + 1, _max_phase_iters)
            S.renew_lease(db, run_id, owner)
            db.commit()
        else:
            logger.error("drive_run exceeded %d phase iterations — failing run %s", _max_phase_iters, run_id)
            run = db.get(AgenticRun, run_id)
            if run:
                S.mark_terminal(db, run, AgenticStatus.FAILED, error=f"exceeded {_max_phase_iters} phase iterations")
                db.commit()
    except Exception as e:                               # noqa: BLE001
        # The failing statement may have left the Postgres transaction aborted
        # (InFailedSqlTransaction). Roll back FIRST so the recovery writes below
        # — mark_terminal/emit_event and the paused-transient commit — run on a
        # clean connection instead of re-raising and leaving the run non-terminal
        # (which the recovery beat would then re-drive → crash loop).
        try:
            db.rollback()
        except Exception:                                # noqa: BLE001
            pass
        if _lease_lost.is_set():
            # We were superseded mid-phase and then RAISED — most often this driver's own
            # emit_event hitting the (run_id, seq) unique constraint against the NEW driver's
            # events. The recovery path below would mark_terminal(FAILED) on the row, but we no
            # longer own it — that FAILS the healthy run the new owner is driving. The tx is
            # already rolled back above; exit quietly and let the new owner finish (§3).
            logger.error("drive_run: run=%s raised while LEASE LOST (%s: %s) — swallowing; the new "
                         "owner drives the run", run_id, type(e).__name__, str(e)[:200])
            return {"acquired": False, "lease_lost": True}
        run = db.get(AgenticRun, run_id)
        _phase_now = getattr(run, "phase", "?") if run else "?"
        if run and run.status not in {s.value for s in TERMINAL_STATUSES}:
            tries = (run.attempts_json or {}).get("transient_resume", 0)
            if _is_transient(e) and tries < settings.agentic_max_transient_resumes:
                # Network/API/infra blip — PAUSE, don't fail. Leave the run active
                # (lease freed in `finally`) and re-dispatch with backoff so it resumes
                # from its persisted phase + on-disk edits when the issue clears.
                countdown = min(30 * (tries + 1), 300)
                logger.warning("drive_run: run=%s TRANSIENT error at phase=%s (%s: %s) — pausing, "
                               "auto-resume attempt %d/%d in %ds",
                               run_id, _phase_now, type(e).__name__, str(e)[:200],
                               tries + 1, settings.agentic_max_transient_resumes, countdown)
                attempts = dict(run.attempts_json or {}); attempts["transient_resume"] = tries + 1
                run.attempts_json = attempts
                emit_event(db, run.id, "paused_transient",
                           {"error": repr(e)[:300], "try": tries + 1, "action":
                            f"⏸ Paused (transient/network: {type(e).__name__}) — auto-resuming (attempt {tries + 1})"})
                db.commit()
                from app.services.celery_tasks import agentic_drive_task
                agentic_drive_task.apply_async((run_id, intent), countdown=countdown)
            else:
                logger.exception("drive_run: run=%s FAILED at phase=%s error_code=%s — %s",
                                 run_id, _phase_now, _error_code(e), repr(e)[:300])
                run.error_code = _error_code(e)
                S.mark_terminal(db, run, AgenticStatus.FAILED, error=repr(e)[:500])
        else:
            logger.exception("drive_run: run=%s raised after already terminal (status=%s) — %s",
                             run_id, getattr(run, "status", "?"), repr(e)[:300])
        db.commit()
    finally:
        _hb_stop.set()                                   # stop the background heartbeat
        reset_usage_context(_usage_tok)
        # Lease release must always succeed — if the except handler itself left
        # the transaction dirty, rollback and retry once so the lease is freed
        # (else it lingers until expiry and blocks re-drive). A still-failing
        # release is non-fatal: the lease lapses and the recovery beat re-arms it.
        try:
            S.release_lease(db, run_id, owner)
            db.commit()
        except Exception:                                # noqa: BLE001
            db.rollback()
            try:
                S.release_lease(db, run_id, owner)
                db.commit()
            except Exception:                            # noqa: BLE001
                logger.exception("drive_run: run=%s lease release failed; will lapse to recovery", run_id)
    if handoff_push:
        # Lease released above → push_run can take it. Dispatch the push task.
        from app.services.celery_tasks import agentic_push_task
        agentic_push_task.delay(run_id)
    run = db.get(AgenticRun, run_id)
    logger.info("drive_run: exit run=%s phase=%s status=%s handoff_push=%s",
                run_id, run.phase, run.status, handoff_push)
    return {"acquired": True, "phase": run.phase, "status": run.status}


async def _step(db, run: AgenticRun, art: dict, model) -> None:
    phase = run.phase
    logger.info("drive_run: step run=%s phase=%s kind=%s", run.id, phase, getattr(run, "kind", "?"))
    if (run.kind or "").startswith("gov_"):
        # Governance review stages (EA/InfoSec) share the run machinery (lease,
        # events, transcripts, recovery) but have their own phase bodies.
        from app.agents import governance_orchestrator as G
        await G.step(db, run, art, model)
        return
    if phase == P.PENDING.value:
        _phase_workspace(db, run, art); S.advance(db, run, P.WORKSPACE_READY)
    elif phase == P.WORKSPACE_READY.value:
        _phase_context(db, run, art); S.advance(db, run, P.CONTEXT_READY)
    elif phase == P.CONTEXT_READY.value:
        if (run.kind or "full") == "code":
            # Phase B: schema already approved — skip XSD, but still pass the TSD gate
            # (ADR-0005) before code generation starts.
            if _tsd_approval_gate(db, run):
                S.advance(db, run, P.CODE_CHANGE)
            else:
                S.advance(db, run, P.AWAITING_TSD_APPROVAL)
                emit_event(db, run.id, "tsd_approval_needed",
                           {"action": "❓ The TSD is not approved yet — approve it, then resume this run"})
        elif (run.kind or "full") == "analysis":
            S.advance(db, run, P.ANALYZING)             # S2: Change-Analysis stage (read-only)
        else:
            S.advance(db, run, P.XSD_DISCOVERY)          # milestone gate
    elif phase == P.ANALYZING.value:
        # Resume of a plan-first run that pivoted to capture scope-signals (see the plan
        # branch below): the plan is already persisted and the scope questions are now
        # answered — go straight to ratification instead of re-running the expensive
        # analysis agent purely to re-derive a plan the scope answers don't change.
        if (run.handoff_json or {}).get("scope_signals_pending"):
            h = dict(run.handoff_json or {}); h.pop("scope_signals_pending", None)
            run.handoff_json = h
            _summary = None
            try:
                from app.models.change_analysis import ChangeAnalysis
                _ca = (db.query(ChangeAnalysis)
                       .filter(ChangeAnalysis.change_request_id == run.change_request_id)
                       .order_by(ChangeAnalysis.version.desc()).first())
                _summary = (getattr(_ca, "functional_plan", None) or {}).get("summary") if _ca else None
            except Exception:  # noqa: BLE001 — summary is cosmetic
                pass
            emit_event(db, run.id, "plan_proposed",
                       {"summary": _summary,
                        "action": "📋 Implementation plan ready — PM ratifies the functional plan, "
                                  "tech-lead the technical analysis"})
            S.advance(db, run, P.AWAITING_PLAN_APPROVAL)
            return
        proposal = await _phase_analysis(db, run, art, model)
        if not proposal:
            run.error_code = "analysis_no_proposal"
            emit_event(db, run.id, "analysis_no_proposal",
                       {"action": "⚠ Analysis produced neither clarifications nor a plan — re-run."})
            S.mark_terminal(db, run, AgenticStatus.FAILED,
                            error="change analysis produced neither clarifications nor a plan")
        elif proposal.get("kind") == "clarifications":
            # v1 — append the PM scope-signal questions so they're captured through
            # the live agentic clarification stage (AnalysisPanel → decision ledger),
            # read back at cert-test-case time by get_scope_signals. Deduped by id;
            # fail-open so a builder error never blocks the clarification gate.
            questions = list(proposal.get("questions") or [])
            if settings.capture_scope_signals:
                try:
                    from app.agents.question_generator import build_scope_signal_questions
                    # v3 — hand the builder the cached party-inference so the
                    # multi-select "parties involved" question is pre-checked
                    # with the agent's best guess. Fail-open when the cache
                    # isn't built yet.
                    _party_inf = None
                    try:
                        from app.services.context_cache import get_or_build
                        _ctx = await get_or_build(run.change_request_id, db)
                        _party_inf = getattr(_ctx, "parties_inference", None) if _ctx else None
                    except Exception:  # noqa: BLE001 — inference is optional
                        logger.exception("party inference lookup failed run=%s", run.id)
                    have = {q.get("id") for q in questions if isinstance(q, dict)}
                    questions.extend(sq for sq in build_scope_signal_questions(_party_inf)
                                     if sq["id"] not in have)
                except Exception:
                    logger.exception("scope-signal question injection failed run=%s", run.id)
            proposal = {**proposal, "questions": questions}
            emit_event(db, run.id, "clarifications_requested",
                       {**proposal, "action": "❓ The analysis agent has questions before drafting the plan"})
            S.advance(db, run, P.AWAITING_CLARIFICATIONS)
        else:                                            # a plan proposal
            _persist_change_analysis(db, run, proposal)
            # Plan enforcement audit (conservative) — challenge the plan's hard enforcement claims
            # BEFORE the human ratifies. A sound plan is left byte-identical; an evidence-backed gap is
            # appended (additively) to technical_analysis["enforcement_audit"] for the ratifier. Fail-open.
            if getattr(settings, "agentic_plan_enforcement_audit", True):
                try:
                    from app.agents import plan_audit as PA
                    from app.models.change_analysis import ChangeAnalysis
                    _ca = (db.query(ChangeAnalysis)
                           .filter(ChangeAnalysis.change_request_id == run.change_request_id)
                           .order_by(ChangeAnalysis.version.desc()).first())
                    if _ca is not None:
                        _audit = await PA.audit_plan_enforcement(
                            functional_plan=_ca.functional_plan, technical_analysis=_ca.technical_analysis)
                        if not _audit.get("sound") and PA.annotate_plan(_ca, _audit["findings"]):
                            db.add(_ca); db.commit()
                            emit_event(db, run.id, "plan_enforcement_audit",
                                       {"findings": len(_audit["findings"]),
                                        "items": [{"requirement": f.get("requirement"), "detail": f.get("detail")}
                                                  for f in _audit["findings"]][:10],
                                        "action": (f"🔎 Plan enforcement audit: {len(_audit['findings'])} "
                                                   "enforcement claim(s) rest on an unverified assumption — "
                                                   "review before ratifying")})
                except Exception as e:  # noqa: BLE001 — never break plan persistence on the audit
                    logger.debug("plan enforcement audit failed: %s", e)
            # Four-party flow coverage (deterministic, advisory): every NEW message schema the
            # plan introduces must be ROUTED in flow_spec — who initiates it and how it reaches
            # each party. Surfaced before ratification so the PM reopens rather than ratifying
            # an unrouted API.
            _fgaps = _new_api_flow_gaps(proposal.get("technical_analysis"), proposal.get("flow_spec"))
            if _fgaps:
                emit_event(db, run.id, "plan_flow_coverage",
                           {"missing": _fgaps,
                            "action": ("⚠ New message(s) planned but NOT routed in the flow spec: "
                                       + ", ".join(_fgaps) + " — the plan must state which party "
                                       "initiates each and how it reaches every party of the "
                                       "four-party model. Reopen the plan to add the route, or "
                                       "ratify accepting the gap.")})
            # Party-flow audit (deterministic, advisory): every touched message — existing or
            # new — must carry a party_flows entry with evidence-cited hops. Assumed hops are
            # the honest unknowns: clarification candidates, never blockers. A fully-confirmed
            # flow stays silent — no banner, no question.
            _pf = _party_flow_gaps(proposal.get("technical_analysis"), proposal.get("flow_spec"))
            if _pf.get("missing") or _pf.get("unevidenced") or _pf.get("assumed"):
                _msgs = []
                if _pf["missing"]:
                    _msgs.append("no party flow stated for: " + ", ".join(_pf["missing"]))
                if _pf["unevidenced"]:
                    _msgs.append("hop(s) with no code/doc evidence (and not marked assumed): "
                                 + "; ".join(_pf["unevidenced"][:6]))
                if _pf["assumed"]:
                    _msgs.append("hop(s) the agent could NOT confirm from code or docs: "
                                 + "; ".join(_pf["assumed"][:6]))
                emit_event(db, run.id, "plan_party_flow_audit",
                           {**_pf,
                            "action": ("⚠ Party-flow audit: " + " | ".join(_msgs)
                                       + " — reopen the plan to fix/confirm, or ratify accepting this.")})
            # Cert scope-signals must be captured before ratification even when the agent
            # went STRAIGHT to a plan (skipping clarifications). Otherwise a plan-first
            # analysis run silently defaults cert enforcement to permissive — the 12
            # scope-signal questions only got injected on the clarifications branch, so
            # they evaporated whenever the agent decided to propose a plan directly. If they
            # were not already gathered (by a clarifications round this change), pivot to a
            # scope-signals-only gate. The plan is already persisted, so the resume reuses it
            # (no re-run of the expensive analysis agent — see the ANALYZING guard above).
            _scope_qs = []
            if (settings.capture_scope_signals
                    and not _scope_signals_captured(db, run.change_request_id)):
                try:
                    from app.agents.question_generator import build_scope_signal_questions
                    # v3 — same pattern as the clarifications branch above:
                    # feed the cached party-inference so the "parties involved"
                    # multi-select is pre-checked with the agent's best guess.
                    _party_inf = None
                    try:
                        from app.services.context_cache import get_or_build
                        _ctx = await get_or_build(run.change_request_id, db)
                        _party_inf = getattr(_ctx, "parties_inference", None) if _ctx else None
                    except Exception:  # noqa: BLE001 — inference is optional
                        logger.exception("party inference lookup failed run=%s", run.id)
                    _scope_qs = build_scope_signal_questions(_party_inf)
                except Exception:  # noqa: BLE001 — a build error must not block the plan
                    logger.exception("scope-signal question build failed run=%s", run.id)
            if _scope_qs:
                h = dict(run.handoff_json or {}); h["scope_signals_pending"] = True
                run.handoff_json = h
                emit_event(db, run.id, "clarifications_requested",
                           {"questions": _scope_qs, "scope_signals_only": True,
                            "action": "❓ A few cert-scope questions before you ratify the plan"})
                S.advance(db, run, P.AWAITING_CLARIFICATIONS)
            else:
                emit_event(db, run.id, "plan_proposed",
                           {"summary": proposal.get("summary"),
                            "action": "📋 Implementation plan ready — PM ratifies the functional plan, "
                                      "tech-lead the technical analysis"})
                S.advance(db, run, P.AWAITING_PLAN_APPROVAL)
    elif phase == P.XSD_DISCOVERY.value:
        handoff = run.handoff_json or {}
        gate_on = settings.agentic_approach_gate and (run.kind or "full") in ("xsd", "full")
        if gate_on and not handoff.get("approach_decision") and not handoff.get("xsd_change_request"):
            # Reuse-first decision pass: map flows + propose options; STOP for the human.
            proposal = await _phase_propose(db, run, art, model)
            if proposal and proposal.get("options"):
                emit_event(db, run.id, "approach_proposal",
                           {**proposal, "action":
                            "🧭 Choose how to accommodate this requirement — reuse vs new (recommended marked)"})
                S.advance(db, run, P.AWAITING_APPROACH_DECISION)
            else:
                # Gate is ON but the agent returned NO options. Do NOT silently proceed —
                # that would turn an LLM miss into permission to create. Fail recoverably
                # so a human can re-run; the reuse-first gate must never be skipped.
                run.error_code = "approach_gate_no_options"
                emit_event(db, run.id, "approach_gate_failed",
                           {"action": "⚠ The agent did not return reuse-vs-new options — not proceeding. "
                                      "Re-run so the approach gate can present choices."})
                S.mark_terminal(db, run, AgenticStatus.FAILED,
                                error="reuse-first approach gate produced no options; refusing to auto-generate")
        else:
            await _phase_xsd(db, run, art, model); _post_xsd_advance(db, run, art)
    elif phase == P.CODE_CHANGE.value:
        await _phase_code(db, run, art, model)
        cs = art.get("change_set")
        stopped = getattr(cs, "stopped", "") or ""
        disk_n = _disk_change_count(run)
        _dq = getattr(cs, "decision_request", None)
        if stopped == "awaiting_decision" and _dq:
            # A3 — the agent surfaced a decision a human must own (directive vs code-reality
            # conflict, or a missing critical decision). Park; the answer resumes CODE_CHANGE
            # as a binding ledger directive. Edits made so far stay on disk (disk = truth).
            # Before parking, check whether this is the SAME question a human already
            # answered. Asking a third time is not a decision gap — the agent cannot
            # execute the answer it keeps getting (in the run that motivated this, a
            # schema edit it was structurally forbidden to make), and re-prompting just
            # extends the loop. Name the loop and hand the human the prior answers.
            _repeat: dict = {}
            _kmeta: dict = {}
            try:
                from app.services import decision_ledger as DL
                _key, _kmeta = DL.resolve_question_key(
                    db, run.change_request_id, prefix="code_decision",
                    anchor=str(_dq.get("blocked_item") or _dq.get("question") or ""),
                    question=str(_dq.get("question") or ""), kind="code_decision")
                _repeat = DL.repeat_state(db, run.change_request_id, _key)
                _dq = {**_dq, "question_key": _key, "key_match": _kmeta.get("match"),
                       "prior_answers": _repeat["prior"], "ask_count": _repeat["count"] + 1,
                       "related_questions": _kmeta.get("related") or []}
            except Exception:  # noqa: BLE001 — the repeat check must never block parking
                logger.exception("code-decision repeat check failed run=%s", run.id)

            h = dict(run.handoff_json or {})
            h["code_decision_request"] = _dq
            run.handoff_json = h
            if _repeat.get("is_defect"):
                _dq = {**_dq, "platform_defect": True}
                h = dict(run.handoff_json or {}); h["code_decision_request"] = _dq
                run.handoff_json = h
                emit_event(db, run.id, "code_decision_loop", {
                    **_dq, "disk_changes": disk_n,
                    "action": (f"🔁 The agent is asking a question you already answered "
                               f"{_repeat['count']}× — it cannot act on the answer it keeps "
                               "receiving. This is a platform/scope problem, not a missing "
                               "decision: the answer likely requires an edit the code phase is "
                               "not permitted to make. Your prior answers are attached.")})
            emit_event(db, run.id, "code_decision_needed",
                       {**_dq, "disk_changes": disk_n,
                        "action": ("❓ The code agent needs a decision it must not make itself: "
                                   + str(_dq.get("question") or "")[:180])})
            S.advance(db, run, P.AWAITING_CODE_DECISION)
            return
        # Fix 2 — the code phase needs a change to the human-approved schema. It cannot make
        # one itself, so the exact edit was STAGED rather than refused; park for a human ruling.
        # Checked AFTER the decision gate (an explicit question outranks a staged edit) but
        # BEFORE verification, because verifying against a schema everyone agrees is wrong
        # just burns a build.
        _amends = list(getattr(cs, "schema_amendments", None) or [])
        if _amends:
            # Only amendments a human has NOT already ruled on. Keyed per-proposal rather than a
            # single run-level flag: a re-stage of a decided edit must not re-park the run (that
            # is the loop), but a genuinely different schema problem found later still deserves
            # a gate.
            _decided = set((getattr(run, "handoff_json", None) or {}).get(
                "schema_amendments_decided") or [])
            _amends = [a for a in _amends if _amendment_key(a) not in _decided]
        if _amends:
            try:
                from app.services import schema_amendment as SA
                _amends = SA.describe(run.id, _ws_id(run), art.get("repo_base_sha") or {}, _amends)
            except Exception:  # noqa: BLE001 — provenance is advisory; park regardless
                logger.exception("schema-amendment describe failed run=%s", run.id)
            h = dict(getattr(run, "handoff_json", None) or {})
            h["schema_amendment_request"] = {"amendments": _amends, "disk_changes": disk_n}
            run.handoff_json = h
            _files = sorted({str(a.get("file") or a.get("path") or "?") for a in _amends})
            _own = sum(1 for a in _amends if a.get("origin") == "phase_a")
            emit_event(db, run.id, "schema_amendment_needed", {
                "amendments": _amends, "disk_changes": disk_n,
                "action": ("🧾 The code agent needs a change to the approved schema "
                           f"({', '.join(_files)}) and cannot make it itself. "
                           + (f"{_own} of {len(_amends)} would amend text Phase A added during "
                              "this same change. " if _own else "")
                           + "Review the exact before/after and approve or reject — approving "
                             "applies it verbatim and resumes code generation.")})
            S.advance(db, run, P.AWAITING_SCHEMA_AMENDMENT)
            return
        # An EMPTY code phase is not a finalised change: the agent made zero edits (or was
        # cancelled). Don't flow a no-op change-set to verification→approval as if done — surface it
        # so the human re-runs, instead of being shown an empty MR that looks "finalised". (Running
        # out of context is NOT a failure here — the loop compacts and continues, Claude-Code style.)
        if stopped == "cancelled":
            # A cancel is a STOP, not a "no change" — and it may sit on a COMPLETE change-set on disk
            # (disk_changes>0). Label it accurately instead of the misleading "produced no change".
            run.error_code = "cancelled"
            emit_event(db, run.id, "code_no_change",
                       {"stopped": stopped, "disk_changes": disk_n,
                        "action": (f"⛔ Code generation was CANCELLED before it finished "
                                   f"({disk_n} file(s) were on disk, not shipped). Re-run to continue.")})
            S.mark_terminal(db, run, AgenticStatus.FAILED,
                            error=f"code generation cancelled before it finished ({disk_n} file(s) on disk, not shipped)")
        elif disk_n == 0:
            run.error_code = "code_no_change"
            emit_event(db, run.id, "code_no_change",
                       {"stopped": stopped, "disk_changes": disk_n,
                        "action": "⛔ Code generation produced no change — the agent made no code "
                                  "changes at all. An empty change is NOT shipped as done; re-run or "
                                  "narrow the scope."})
            S.mark_terminal(db, run, AgenticStatus.FAILED,
                            error="code phase produced no change (empty change-set): the agent made no code changes at all")
        else:
            S.advance(db, run, P.VERIFICATION)
    elif phase == P.VERIFICATION.value:
        status = _phase_verify(db, run, art)
        v = art.get("verification", {})
        if status == "verified":
            h = dict(run.handoff_json or {}); h["verified"] = True; h.pop("verify_skipped", None)
            run.handoff_json = h                            # durable: survives reload / REST / audit
            S.advance(db, run, P.REVIEW)
        elif status == "needs_fix" and (run.attempts_json or {}).get("code_change", 0) < settings.agentic_max_code_attempts:
            S.advance(db, run, P.CODE_CHANGE)            # retry (attempts++ in advance)
        else:
            # Auto-retry budget spent (needs_fix after N tries) OR the build couldn't run
            # at all (unverified). Don't give up silently and don't accept silently — PARK
            # for a human decision: retry once more, or skip verification and proceed.
            attempts = (run.attempts_json or {}).get("code_change", 0)
            reason = (v.get("reason") or "; ".join((v.get("errors") or ["(none parsed)"])[:3]))
            emit_event(db, run.id, "verify_decision_needed",
                       {"status": status, "attempts": attempts,
                        "errors": (v.get("errors") or [])[:5], "reason": reason,
                        "action": (f"⛔ Verification failed {attempts}× — your call: retry once more, "
                                   f"or skip verification and proceed (the change will be marked UNVERIFIED)")})
            S.advance(db, run, P.AWAITING_VERIFY_DECISION)   # human gate; status stays ACTIVE
    elif phase == P.REVIEW.value:
        blocking = await _phase_review(db, run, art, model)
        rv = art.get("review") or {}
        rounds = (run.attempts_json or {}).get("review", 0)
        # A blocker-severity finding ("must not ship") earns extra fix rounds beyond the normal
        # cap, so a blocker surfaced in the last normal round still gets a fix pass instead of
        # freezing open. Ordinary blocking findings use the normal cap, then a human adjudicates.
        # getattr-guarded: a worker image that lags the config (a partial/stale deploy where the
        # field isn't on Settings yet) must NOT crash the whole review phase and force a manual
        # resume — fall back to the declared defaults (blocker 10, review 6).
        cap = (getattr(settings, "agentic_max_blocker_rounds", 10) if rv.get("has_blocker")
               else getattr(settings, "agentic_max_review_rounds", 6))
        if blocking and rounds < cap:
            S.advance(db, run, P.CODE_CHANGE)            # blocking findings → fix
        else:
            if rv.get("has_blocker"):
                # Exhausted the fix budget with a blocker still open: freeze (so the diff is
                # inspectable) but flag it LOUDLY — the manifest review carries has_blocker, and
                # the push endpoints refuse to ship it until it's resolved or explicitly overridden.
                # Use the SAME predicate as the push gate (severity OR sensitive category), so the
                # loud event lists exactly what blocks the push — not an empty list when the block
                # came from a sensitive-category finding the reviewer graded below "blocker".
                _blk_all = [i for i in (rv.get("items") or [])
                            if is_must_block(i.get("category"), i.get("severity"))]
                blk = _blk_all[:10]
                # Count what the 10-item event cap hides PLUS what the 15-item items cap
                # already dropped upstream — the banner must never understate the block.
                _blk_more = max(0, len(_blk_all) - 10) + int(rv.get("dropped_must_block") or 0)
                # TIERED verdict — "code is broken" and "code is clean, plan bookkeeping needs a
                # human click" are different situations and must not share the same scary banner.
                # Test 11 parked with ZERO reviewer blockers (only plan-coverage items: logic
                # delivered under a different file name / a file that needs no edit) and the ⛔
                # text read as "code generation failed" to the user. When every open blocker is
                # a completeness/bookkeeping item AND the reviewer found no correctness/security
                # blocker, say so calmly; the push stays gated either way.
                _bookkeeping_only = bool(blk) and all(
                    (i.get("category") or "").lower() == "completeness" for i in blk)
                # Third tier: every open must-block is a REVIEWER-GAP (verdict deficiency
                # that survived the corrective retry) — the code is not implicated, and
                # "⛔ blocking finding still open after the fix budget" would tell the
                # human the code is broken when the REVIEW failed. Push stays gated.
                _gaps_only = bool(blk) and all(i.get("reviewer_gap") for i in blk)
                if _gaps_only:
                    _action = (f"🟡 The reviewer could not produce a usable verdict for {len(blk)} "
                               "item(s) (unverified directives / unparseable output) even after a "
                               "corrective retry — a REVIEW failure, not a code defect. No "
                               "correctness or security finding is open against the code. "
                               "Adjudicate these item(s) to push, or retry to re-run the review.")
                elif _bookkeeping_only:
                    _action = (f"🟡 Code PASSED review and verification — no correctness or security "
                               f"blocker is open. {len(blk)} plan-coverage item(s) need your "
                               "confirmation (logic delivered under a different file name, or a "
                               "planned file that needs no edit). Review them and approve/override "
                               "to push, or retry if you want the plan followed literally.")
                else:
                    _action = ("⛔ A blocking finding is still open after the fix budget — "
                               "push is blocked. Resolve it (start over / retry) or explicitly "
                               "override to push anyway."
                               + (f" (showing {len(blk)} of {len(blk) + _blk_more} open blocking "
                                  "finding(s) — the persisted review findings hold the full set)"
                                  if _blk_more else ""))
                rv["bookkeeping_only"] = _bookkeeping_only    # carried into the frozen manifest for the UI
                rv["reviewer_gaps_only"] = _gaps_only
                emit_event(db, run.id, "review_blocked",
                           {"rounds": rounds, "blockers": len(blk) + _blk_more, "items": blk,
                            "bookkeeping_only": _bookkeeping_only,
                            "reviewer_gaps_only": _gaps_only, "action": _action})
            _phase_freeze(db, run, art)
            S.advance(db, run, P.AWAITING_HUMAN_APPROVAL)
    else:
        raise RuntimeError(f"orchestrator has no handler for phase {phase!r}")


# ── Post-approval push (the one remote write, §12/§22) ────────────────────────

async def push_run(db, run_id: str, *, owner: str = "orchestrator") -> dict:
    """Resume an APPROVED run: preflight, push the new branch under the git-guard,
    raise the MR. The only remote mutation in the whole pipeline."""
    from app.models.agentic import ChangeManifest
    import uuid as _uuid
    owner = f"{owner}:{_uuid.uuid4().hex[:8]}"   # unique per driver — see drive_run
    logger.info("push_run: start run=%s owner=%s", run_id, owner)
    run = db.get(AgenticRun, run_id)
    if run is not None and (run.kind or "").startswith("gov_"):
        # Governance stage: the "push" is a fix-commit append onto the parent's
        # EXISTING feature branch — its own preflight/idempotency/guard contract.
        from app.agents import governance_orchestrator as G
        return await G.push_stage_fixes(db, run_id)
    if S.check_cancel(run):
        # A cancel arrived while this run was wedged/crash-looped (e.g. a dead worker's
        # lease had expired and the recovery sweep re-dispatched us before honouring the
        # flag). Terminate here instead of pushing — this is the one check that stops the
        # cancel/recover crash-loop, since every re-dispatch re-enters push_run.
        # honour_cancel handles the re-opened deferred-push case (phase='completed' has
        # no legal edge to CANCELLED — it closes back to COMPLETED instead of raising).
        logger.info("push_run: run=%s cancel_requested — honouring cancel instead of pushing", run_id)
        S.honour_cancel(db, run)
        db.commit()
        return {"pushed": False, "reason": "cancelled"}
    man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run_id)
           .order_by(ChangeManifest.created_at.desc()).first())
    if man is None or man.approved_at is None:
        logger.info("push_run: run=%s NOT pushable — manifest missing/unapproved", run_id)
        return {"pushed": False, "reason": "manifest not approved"}
    # Deferred-push parents carry any APPROVED governance-stage fixes as an overlay:
    # the merged op list keeps every pushed byte pinned to a human-approved hash
    # (parent gate or stage gate). No-op when no approved stage fixes exist.
    from app.agents import governance_orchestrator as _G
    man = _G.overlay_stage_fixes(db, run, man)
    # …and a FAILED/CANCELLED stage's never-approved fixer edits are reverted to
    # the approved-state text, or the preflight below would hash-fail on files the
    # human never signed off — wedging the approved change terminally.
    _G.restore_unapproved_stage_edits(db, run)
    if not S.acquire_lease(db, run_id, owner):
        logger.info("push_run: run=%s lease NOT acquired (busy)", run_id)
        return {"pushed": False, "reason": "not acquired"}
    S.advance(db, run, P.PUSHING)
    db.commit()
    _hb_stop, _push_lease_lost = _start_heartbeat(run_id, owner)   # keep the lease alive through a slow push

    branch = man.per_repo[0]["shared_branch_name"] if man.per_repo else None
    ws_id = _ws_id(run)                                  # Phase B pushes Phase A's shared tree
    # Full application state going into the push — the exact data needed to reconstruct
    # any shown-vs-pushed question after the fact: what hash was approved, when, and
    # what each repo's branch currently holds.
    from app.models.agentic import AgenticRunRepo as _ARR
    _rows = db.query(_ARR).filter(_ARR.run_id == run_id).all()
    logger.info(
        "push_run: run=%s STATE phase=%s manifest=%s approved_at=%s ops=%d ws=%s repos=[%s]",
        run_id, run.phase, man.manifest_hash[:12], man.approved_at,
        len(man.operations or []), ws_id,
        "; ".join(f"{r.repo_id[:8]}:{r.push_state or 'pending'}"
                  f"@{(r.pushed_manifest_hash or '?')[:12]} on {r.branch or '-'}"
                  for r in _rows) or "none")
    # A prior FAILED push can leave its own commit on the tree (checkout -B + commit ran
    # before the push was rejected), advancing HEAD past the recorded base → the preflight
    # below reads that as drift and loops in rebase_reverify forever. Undo it first.
    _undo_leftover_push_commit(run_id, man, ws_id)
    # Same-change re-runs derive the SAME title-based branch; if it already exists on the
    # remote (an earlier run of this change pushed it), the git-guard would deny the push
    # ("must be brand-new" — it never force-pushes/overwrites). Append a short run suffix
    # so we push a fresh branch instead — ONE name for all repos (shared-branch invariant).
    branch = _brand_new_branch(db, run_id, man, branch, ws_id)

    # ACTUAL workspace HEAD per repo — NOT the manifest's recorded base SHA. Comparing
    # the manifest to itself would be tautological and could never catch a workspace
    # that was rebased/checked out to a different commit between approval and push.
    current_base_sha: dict[str, str] = {}
    for pr in (man.per_repo or []):
        try:
            current_base_sha[pr["repo_id"]] = workspace_local.read_base_sha(ws_id, pr["repo_id"])
        except Exception:  # noqa: BLE001 — unreadable HEAD ⇒ leave absent ⇒ preflight drift-fails
            current_base_sha[pr["repo_id"]] = None

    def _read(repo_id, path):
        p = workspace_local.repo_dir(ws_id, repo_id) / path
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None

    manifest_dict = {"per_repo": man.per_repo, "operations": man.operations}
    logger.info("push_run: run=%s branch=%s repos=%d — preflight", run_id, branch, len(man.per_repo or []))
    ok, reasons = M.push_preflight(manifest_dict, current_base_sha=current_base_sha, read_content=_read)
    if not ok:
        # The leftover-commit phantom was already undone above (_undo_leftover_push_commit), so a
        # remaining "base SHA drift" here is GENUINE — the base branch really moved since approval.
        # The old code bounced to rebase_reverify, a phase with NO working handler (push_run can't
        # even advance INTO pushing from it), so the run wedged there forever. Fail cleanly with an
        # actionable, retryable message instead — never a silent dead-end.
        drift = any("base SHA" in r for r in reasons)
        logger.warning("push_run: run=%s preflight FAILED (drift=%s): %s", run_id, drift, "; ".join(reasons))
        msg = (("the base branch moved since this change was approved (the recorded base no longer "
                "matches the repo) — re-run Phase B on the current base, then approve and push again")
               if drift else "; ".join(reasons))
        _commit_terminal(db, run_id, msg, error_code=("BASE_DRIFT" if drift else "PREFLIGHT_FAILED"))
        emit_event(db, run_id, "push_preflight_failed", {"reasons": reasons, "drift": drift})
        db.commit()
        _hb_stop.set(); S.release_lease(db, run_id, owner); db.commit()
        return {"pushed": False, "reason": "; ".join(reasons)}

    # NO outer git-guard policy here: `_git_push_branch` self-guards each repo —
    # it injects the token (git remote set-url, which the guard DENIES) with NO
    # policy active, sets a tight policy around ONLY `git push origin <branch>`,
    # then scrubs the token. An outer policy would (wrongly) deny that credential
    # injection — `git remote` is on the deny list — and fail the approved push.
    try:
        emit_event(db, run_id, "push_started", {"branch": branch})
        logger.info("push_run: run=%s pushing branch=%s", run_id, branch)
        result = _push_all(db, run_id, man, branch, ws_id)
        S.mark_terminal(db, run, AgenticStatus.COMPLETED)
        emit_event(db, run_id, "completed", {"branch": branch, **result})
        db.commit()
        logger.info("push_run: run=%s COMPLETED branch=%s pushed=%s repos=%s mrs=%s",
                    run_id, branch, result.get("pushed"), result.get("repos"), result.get("mr_urls"))
        return {"pushed": result.get("pushed", False), "branch": branch, **result}
    except Exception as e:                              # noqa: BLE001 — a push failure is recorded, not swallowed
        logger.exception("push failed for run %s", run_id)
        from app.agents.infra_errors import classify_infra_error
        real = str(e).strip()                          # the ACTUAL git/runtime error — surfaced verbatim
        issue = classify_infra_error(real)
        # Show what git ACTUALLY said (summarised), plus a short hint when we recognise the cause —
        # never a canned message that hides the real error. FAILED stays retryable, so a retry after
        # the operator fixes the underlying issue just works (§18).
        msg = f"Push failed: {real[:400]}"
        if issue:
            msg += f"  —  likely cause: {issue.problem}. {issue.fix}"
        _commit_terminal(db, run_id, msg, error_code=(issue.code if issue else "PUSH_FAILED"))
        emit_event(db, run_id, "push_failed",
                   {"error": real[:400], "code": (issue.code if issue else "PUSH_FAILED"),
                    "hint": (issue.fix if issue else None), "retryable": True})
        db.commit()
        return {"pushed": False, "error": real[:300]}
    finally:
        _hb_stop.set()
        S.release_lease(db, run_id, owner)
        db.commit()


def _commit_terminal(db, run_id: str, error: str, error_code: str | None = None) -> None:
    """Persist FAILED + error RESILIENTLY. A concurrent driver (lease race) can deadlock the
    UPDATE on the agentic_runs row and roll our write back — which is exactly how a run gets
    wedged in a non-terminal phase (e.g. rebase_reverify) instead of failing cleanly. Roll
    back and retry once on a fresh fetch so the terminal state sticks even under contention."""
    for _attempt in (1, 2):
        try:
            run = db.get(AgenticRun, run_id)
            if run is None:
                return
            S.mark_terminal(db, run, AgenticStatus.FAILED, error[:500])
            if error_code is not None:
                run.error_code = error_code
            db.commit()
            return
        except Exception:  # noqa: BLE001 — deadlock / rolled-back tx; retry once on a fresh one
            db.rollback()
    logger.warning("push_run: run=%s could not persist terminal state after a retry", run_id)


def _undo_leftover_push_commit(run_id: str, man, ws_id: str) -> None:
    """Recover from a prior FAILED push that left its commit behind. ``_git_push_branch``
    does ``checkout -B`` + commit BEFORE the ``git push`` (which the guard can reject), so
    a rejected push leaves HEAD one commit past the recorded base. The push preflight then
    reads that as base-SHA "drift" and bounces the run to rebase_reverify — where every
    re-push re-detects the same leftover and loops forever.

    Detect that EXACT leftover (HEAD's parent == the manifest's recorded base AND the
    commit is our own ``agentic:`` commit) and ``reset --mixed`` back to base, KEEPING the
    approved edits in the working tree so the re-push commits + pushes them cleanly.
    Genuine drift (HEAD diverged any other way) is left alone for rebase_reverify."""
    for pr in (man.per_repo or []):
        repo_id, base = pr["repo_id"], pr.get("base_commit_sha")
        if not base:
            continue
        rd = workspace_local.repo_dir(ws_id, repo_id)
        head = adapter.run_command(rd, ["git", "rev-parse", "HEAD"]).stdout.strip()
        if not head or head == base:
            continue
        parent = adapter.run_command(rd, ["git", "rev-parse", "HEAD^"]).stdout.strip()
        subject = adapter.run_command(rd, ["git", "log", "-1", "--format=%s"]).stdout.strip()
        if parent == base and subject.startswith("agentic:"):
            adapter.run_command(rd, ["git", "reset", "--mixed", base])
            logger.info("push_run: run=%s repo=%s — reset leftover push commit %s → base %s (kept edits)",
                        run_id, repo_id, head[:8], base[:8])


def reset_workspace_to_recorded_base(db, run_id: str) -> dict:
    """Aggressive 'fast-path' recovery for BASE_DRIFT: for every repo in this run's latest
    manifest, ``git reset --mixed`` the workspace back to the manifest's RECORDED base SHA,
    keeping the working-tree files. Discards any leftover local commits (from a successful
    prior push, an aborted attempt, or anything outside the narrow agentic:-subject undo
    above) so the next push preflight finds HEAD at the recorded base.

    Why this is safe to expose as an explicit action: the file content is preserved (--mixed)
    and the push goes through the GitLab commits API on file content — local git history is
    not what gets pushed. The shared-branch invariant + ``_brand_new_branch`` ensure we never
    force-push or overwrite an existing remote branch.

    Why it's NOT the auto-default (which is why the narrow targeted undo above stays in place):
    pushes against the RECORDED base, which may be older than the current upstream main if
    someone else committed there since this run was approved. The MR will then have merge
    conflicts. Use ``rerun_code_gen`` when you need a fresh base from current upstream; use
    this when you know upstream is unchanged and just want the stale local HEAD cleared."""
    from app.models.agentic import ChangeManifest
    run = db.get(AgenticRun, run_id)
    if run is None:
        return {"reset": 0, "reason": "run not found"}
    man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run_id)
           .order_by(ChangeManifest.created_at.desc()).first())
    if not man or not man.per_repo:
        return {"reset": 0, "reason": "no manifest"}
    ws_id = _ws_id(run)
    count = 0
    for pr in man.per_repo:
        repo_id, base = pr["repo_id"], pr.get("base_commit_sha")
        if not base:
            continue
        rd = workspace_local.repo_dir(ws_id, repo_id)
        head = adapter.run_command(rd, ["git", "rev-parse", "HEAD"]).stdout.strip()
        if head == base:
            continue   # already at base — nothing to do for this repo
        adapter.run_command(rd, ["git", "reset", "--mixed", base])
        logger.info("reset_workspace_to_recorded_base: run=%s repo=%s — HEAD %s → %s (files kept)",
                    run_id, repo_id, head[:8], base[:8])
        count += 1
    return {"reset": count, "repos": len(man.per_repo)}


def _brand_new_branch(db, run_id: str, man, base_branch: str | None, ws_id: str) -> str | None:
    """Pick a BRAND-NEW shared branch for the push. The git-guard only allows pushing a
    branch that doesn't yet exist on the remote (it never force-pushes/overwrites — §22).
    Branch names are title-derived and shared across repos, so a re-run of the SAME change
    collides with the earlier run's branch and gets denied. If ``base_branch`` already
    exists on ANY in-scope repo's remote, append a short run suffix so the push targets a
    fresh branch — ONE consistent name for every repo (the shared-branch invariant).

    Best-effort: a probe failure falls back to ``base_branch`` (the push then behaves as
    before). No policy is active here, so the token set/scrub for ls-remote is allowed."""
    if not base_branch:
        return base_branch
    from app.models.code_repo import CodeRepo

    repos = [(pr["repo_id"], db.get(CodeRepo, pr["repo_id"])) for pr in (man.per_repo or [])]

    def _exists_on_any_remote(candidate: str) -> bool:
        for repo_id, repo in repos:
            if repo is None:
                continue
            gl = repo.gitlab_url or settings.gitlab_url
            rd = workspace_local.repo_dir(ws_id, repo_id)
            try:
                workspace_local.set_remote(ws_id, repo_id,
                                           workspace_local.build_clone_url(gl, repo.gitlab_repo, settings.gitlab_token))
                ls = adapter.run_command(rd, ["git", "ls-remote", "--heads", "origin", candidate])
            finally:
                workspace_local.set_remote(ws_id, repo_id,
                                           workspace_local.build_clone_url(gl, repo.gitlab_repo, ""))
            if ls.ok and (ls.stdout or "").strip():
                return True
        return False

    try:
        if not _exists_on_any_remote(base_branch):
            return base_branch
        suffixed = f"{base_branch}-{run_id[:8]}"
        if _exists_on_any_remote(suffixed):           # astronomically unlikely (run_id is unique)
            suffixed = f"{base_branch}-{run_id[:12]}"
        logger.info("push: branch %s already on remote (same-change re-run) — using brand-new %s",
                    base_branch, suffixed)
        return suffixed
    except Exception:  # noqa: BLE001 — probe is best-effort; fall back to the base name
        logger.warning("push: brand-new-branch probe failed for run=%s; using %s", run_id, base_branch)
        return base_branch


def _push_all(db, run_id: str, man, branch: str, ws_id: str | None = None) -> dict:
    """Forge-AGNOSTIC push (§12/§22): generic git commits the verified working
    tree (add/modify/delete) onto the new branch and pushes it under the
    git-guard — the one remote write. Opening a merge request is the ONLY
    forge-specific step, and it's optional/best-effort (the branch is already on
    the remote for a human to PR however the forge does it).

    ``ws_id`` is the run whose clone holds the tree (Phase B → its Phase-A parent);
    defaults to ``run_id``. Push-state rows are still recorded against ``run_id``."""
    from app.models.code_repo import CodeRepo
    from app.models.agentic import AgenticRunRepo

    ws_id = ws_id or run_id
    if not (settings.gitlab_url and settings.gitlab_token):
        emit_event(db, run_id, "push_skipped", {"reason": "no git credentials configured"})
        return {"pushed": False, "skipped": True}

    pushed: list[str] = []
    mrs: list[str] = []
    targets: list[dict] = []     # per-repo {repo_id, repo, branch, commit, mr_url} shown to the human
    for pr in man.per_repo:
        repo = db.get(CodeRepo, pr["repo_id"])
        rr = (db.query(AgenticRunRepo)
              .filter(AgenticRunRepo.run_id == run_id, AgenticRunRepo.repo_id == pr["repo_id"]).first())
        # IDEMPOTENT: a resume/recovery re-dispatch must NOT re-commit or re-push (which
        # would add an empty commit / hit the git-guard's "branch exists" denial).
        # BUT "pushed once" is NOT "pushed": if the manifest was re-frozen after this
        # repo was pushed (fix rounds after an early approve+push), the branch on git
        # holds an OLDER approved state — skipping here made the re-consented push a
        # silent no-op that reported success. Skip only when the pushed content IS the
        # current manifest. NULL pushed_manifest_hash = legacy row (unknown) → skip,
        # never surprise-re-push historical runs.
        if rr is not None and rr.push_state == "pushed":
            stale = (rr.pushed_manifest_hash is not None
                     and rr.pushed_manifest_hash != man.manifest_hash)
            if not stale:
                logger.info(
                    "push: run=%s repo=%s SKIP — already pushed on %s at %s manifest (%s)",
                    run_id, pr["repo_id"], rr.branch,
                    "the CURRENT" if rr.pushed_manifest_hash else "an UNKNOWN (legacy row)",
                    (rr.pushed_manifest_hash or "?")[:12])
                if rr.mr_url:
                    mrs.append(rr.mr_url)
                targets.append({"repo_id": pr["repo_id"], "repo": getattr(repo, "gitlab_repo", None),
                                "branch": rr.branch, "commit": rr.base_commit_sha, "mr_url": rr.mr_url})
                pushed.append(pr["repo_id"])
                continue
            logger.warning(
                "push: run=%s repo=%s STALE — %s holds manifest %s, current is %s → re-pushing as %s",
                run_id, pr["repo_id"], rr.branch, rr.pushed_manifest_hash[:12],
                man.manifest_hash[:12], branch)
            emit_event(db, run_id, "push_superseding",
                       {"repo_id": pr["repo_id"], "old_branch": rr.branch, "new_branch": branch,
                        "action": (f"⚠ Branch {rr.branch} holds an OLDER approved state — "
                                   f"re-pushing the current changes as {branch}")})
        gl = repo.gitlab_url or settings.gitlab_url
        clean = workspace_local.build_clone_url(gl, repo.gitlab_repo, "")
        # The push needs WRITE access — use the dedicated write-scoped push token
        # when set, else fall back to the read/index token (back-compat).
        _push_tok = settings.gitlab_push_token or settings.gitlab_token
        auth = workspace_local.build_clone_url(gl, repo.gitlab_repo, _push_tok)
        # Stage exactly the approved files for this repo (manifest operations).
        paths = [op["path"] for op in (man.operations or [])
                 if op.get("repo_id") == pr["repo_id"] and op.get("path")]

        commit = _git_push_branch(ws_id, pr["repo_id"], branch, pr["base_commit_sha"], auth, clean, paths)
        mr_url = _maybe_open_mr(repo, branch)
        if mr_url:
            mrs.append(mr_url)

        if rr is None:
            rr = AgenticRunRepo(run_id=run_id, repo_id=pr["repo_id"]); db.add(rr)
        rr.branch, rr.base_commit_sha, rr.mr_url, rr.push_state = branch, pr["base_commit_sha"], mr_url, "pushed"
        rr.pushed_manifest_hash = man.manifest_hash   # bind push state to the approved content
        logger.info("push: run=%s repo=%s PUSHED branch=%s commit=%s manifest=%s mr=%s",
                    run_id, pr["repo_id"], branch, commit[:8], man.manifest_hash[:12],
                    mr_url or "-")
        # Persist EACH repo's pushed state immediately: if the worker dies mid-loop,
        # the next attempt's idempotency check (push_state=="pushed") skips this repo
        # instead of re-pushing it. Shrinks the partial-failure window to one repo.
        db.commit()
        pushed.append(pr["repo_id"])
        targets.append({"repo_id": pr["repo_id"], "repo": getattr(repo, "gitlab_repo", None),
                        "branch": branch, "commit": commit, "mr_url": mr_url})
    return {"pushed": True, "repos": pushed, "mr_urls": mrs, "targets": targets}


def _run_git(repo_dir, *args):
    res = adapter.run_command(repo_dir, ["git", *args])
    if not res.ok:
        raise RuntimeError(f"git {args[0]} failed: {(res.stderr or res.stdout)[-300:]}")
    return res


# Bounded retry for the ONE remote write (`git push`). A transient failure — a
# network blip, a GitLab 5xx, a rate-limit — usually clears on a retry. A read-only
# or wrong-scope token is a hard 403 that won't, so after the attempts we raise a
# clear, actionable error instead of a bare "git push failed".
_PUSH_ATTEMPTS = 3
_PUSH_BACKOFF_S = 2
_AUTH_ERR_RE = re.compile(
    r"\b403\b|forbidden|not allowed to push|unauthor|insufficient|write_repository|read[- ]only",
    re.IGNORECASE,
)


def _push_with_retry(repo_dir, branch: str, run_id: str, repo_id: str, commit: str) -> None:
    """`git push origin <branch>` with bounded retries + backoff. Raises on final
    failure, appending a token-scope hint when the error looks like a permissions/403
    (the read-only-token case). Idempotent: re-pushing the same commit is a no-op."""
    last = ""
    for attempt in range(1, _PUSH_ATTEMPTS + 1):
        res = adapter.run_command(repo_dir, ["git", "push", "origin", branch])
        if res.ok:
            logger.info("push: ws=%s repo=%s PUSHED %s @ %s (attempt %d/%d)",
                        run_id, repo_id, branch, commit[:8], attempt, _PUSH_ATTEMPTS)
            return
        last = ((res.stderr or "") + (res.stdout or "")).strip()[-300:]
        logger.warning("push: ws=%s repo=%s push attempt %d/%d failed: %s",
                       run_id, repo_id, attempt, _PUSH_ATTEMPTS, last)
        if attempt < _PUSH_ATTEMPTS:
            time.sleep(_PUSH_BACKOFF_S * attempt)
    hint = ""
    if _AUTH_ERR_RE.search(last):
        hint = (" — the GitLab token looks read-only / missing the 'write_repository' "
                "scope; grant push access to the project and retry")
    raise RuntimeError(f"git push failed after {_PUSH_ATTEMPTS} attempts: {last}{hint}")


def _git_push_branch(run_id: str, repo_id: str, branch: str, base_sha: str,
                     auth_url: str, clean_url: str, paths: list[str] | None = None,
                     *, commit_subject: str | None = None,
                     allow_existing_branch: bool = False) -> str:
    """Commit ONLY the approved change-set (the manifest's ``paths``) onto ``branch``,
    push it, and return the pushed commit SHA. We stage exactly the reviewed files —
    never ``git add -A``, which would sweep in build output (target/), generated
    sources, and our internal ``.lease`` marker — and never create an empty commit.
    The token is injected for the single guarded push, then scrubbed.

    ``commit_subject``/``allow_existing_branch`` are the governance fix-push variant:
    a ``governance(...):`` commit appended (fast-forward, non-force) to the run's
    EXISTING feature branch. Defaults preserve the codegen behaviour byte-identically."""
    rd = workspace_local.repo_dir(run_id, repo_id)
    logger.info("push: ws=%s repo=%s → checkout -B %s (staging %d path(s))",
                run_id, repo_id, branch, len(paths or []))
    _run_git(rd, "checkout", "-B", branch)
    if paths:
        # `add -A -- <path>` stages adds/modifies AND deletions, scoped to the change-set.
        _run_git(rd, "add", "-A", "--", *paths)
    # Commit ONLY if something is actually staged → no empty commits on a re-push.
    if adapter.run_command(rd, ["git", "diff", "--cached", "--quiet"]).exit_code != 0:
        _run_git(rd, "-c", "user.email=agentic@npci.local", "-c", "user.name=the Authority Agentic",
                 "commit", "-m", commit_subject or f"agentic: {branch}")
        logger.info("push: ws=%s repo=%s committed change-set", run_id, repo_id)
    else:
        logger.info("push: ws=%s repo=%s nothing staged — no new commit (re-push)", run_id, repo_id)
    commit = _run_git(rd, "rev-parse", "HEAD").stdout.strip()

    workspace_local.set_remote(run_id, repo_id, auth_url)        # transient creds (no policy yet)
    try:
        ls = adapter.run_command(rd, ["git", "ls-remote", "--heads", "origin", branch])
        exists = bool((ls.stdout or "").strip())
        policy = git_guard.GitGuardPolicy(run_branch=branch, base_sha=base_sha, branch_exists_on_remote=exists,
                                          allow_existing_branch=allow_existing_branch)
        tok = git_guard.set_policy(policy)
        try:
            logger.info("push: ws=%s repo=%s → git push origin %s (commit=%s, branch_on_remote=%s)",
                        run_id, repo_id, branch, commit[:8], exists)
            _push_with_retry(rd, branch, run_id, repo_id, commit)   # THE one guarded remote write (retried)
        finally:
            git_guard.reset_policy(tok)
    finally:
        workspace_local.set_remote(run_id, repo_id, clean_url)  # re-scrub the token
    return commit


def _maybe_open_mr(repo, branch: str) -> str | None:
    """Open a merge request — the ONLY forge-specific step, best-effort. Failure
    does NOT fail the run: the branch is already pushed for a human to PR."""
    try:
        from app.services import git_integrator as GI
        project = GI._get_gitlab_project(repo.gitlab_repo)
        mr_url, _iid = GI._real_create_mr(project, branch, repo.gitlab_branch, "agentic")
        return mr_url
    except Exception as e:  # noqa: BLE001
        logger.warning("MR creation failed (branch is pushed): %s", e)
        return None


# ── Events replay (the WebSocket subscriber reads this — §3) ───────────────────

def events_for(db, run_id: str, after_seq: int = -1) -> list[dict]:
    """Ordered events for a run after ``after_seq`` — the durable source of truth
    a (re)connecting WebSocket replays. A dropped socket loses nothing."""
    from app.models.agentic import AgenticEvent
    rows = (db.query(AgenticEvent).filter(AgenticEvent.run_id == run_id, AgenticEvent.seq > after_seq)
            .order_by(AgenticEvent.seq).all())
    return [{"seq": r.seq, "kind": r.kind, "payload": r.payload, "ts": r.ts.isoformat() if r.ts else None}
            for r in rows]
