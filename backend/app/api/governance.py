# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance reviews — admin skill management + stage flow endpoints.

Two admin-uploaded skill files (EA + InfoSec rulebooks) drive the two
sequential governance review stages that run between code approval and
Build & Deploy. Skills are append-only versioned rows (`governance_skills`);
active = highest version per type. Upload validates the rule contract
loudly — a skill that cannot be parsed into an unambiguous rule list is
rejected here, never reinterpreted at review time.
"""
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.agents.governance_skills import SKILL_TYPES, validate_skill
from app.core.config import settings
from app.core.deps import AdminUser, AgenticUser, CurrentUser, DbDep
from app.models.governance_skill import GovernanceSkill

logger = logging.getLogger(__name__)
router = APIRouter(tags=["governance"])

MAX_SKILL_BYTES = 256 * 1024  # 256 KB — same ceiling as the Authority policy doc


# ── Admin: skill management ──────────────────────────────────────────────────

class SkillVersionMeta(BaseModel):
    skill_type: str
    version: int
    checksum: str
    filename: str | None = None
    rule_count: int
    # How the enforceable units were derived: "rule_headings" (## RULE lines),
    # "sections" (standard SKILL.md ## headings), or "whole_document" (prose).
    mode: str = ""
    # The skill SLOT this row belongs to (0118). A type holds several slots
    # side by side; every ENABLED slot executes in the stage.
    name: str = ""
    enabled: bool = True
    description: str = ""
    uploaded_by: str | None = None
    created_at: str | None = None
    # Bundle facts (0117): markdown-only skills report is_bundle=False, 0 scripts.
    is_bundle: bool = False
    file_count: int = 0
    script_count: int = 0
    smoke_status: str | None = None      # pending|green|failed; None = nothing to prove
    safety_warnings: int = 0
    # Where the execution contract came from: "manifest" (uploaded form field),
    # "skill_md" (the bundle's own frontmatter — self-describing, preferred),
    # or "none" (no contract). Drives `gating`:
    #   deterministic — validator scripts form a hard, uncappable floor;
    #   agent_driven  — scripts run only as the LLM chooses (no floor);
    #   reasoning     — script-less skill (EA's normal case).
    contract_source: str = "none"
    gating: str = ""
    advisory: str | None = None          # loud note when gating is weaker than expected
    # Set when an upload CREATES a NEW slot while other enabled slots of the same
    # type already exist — so re-uploading an existing skill under a changed
    # frontmatter name doesn't silently run BOTH rulebooks (the 0118 upgrade trap).
    slot_warning: str | None = None
    is_new_slot: bool = False


class SkillVersionDetail(SkillVersionMeta):
    content: str
    rules: list[dict]


def _stype_or_404(stype: str) -> str:
    if stype not in SKILL_TYPES:
        raise HTTPException(status_code=404, detail=f"unknown skill type {stype!r} (expected one of {SKILL_TYPES})")
    return stype


def _slot_name(frontmatter_name: str, filename: str | None) -> str:
    """The skill SLOT an upload lands in: the SKILL.md frontmatter ``name``
    (slugified), else the upload filename stem, else 'default'. The org ships
    several skills per type (four InfoSec skills); each keeps its own slot and
    every enabled slot executes in the stage."""
    import re
    base = (frontmatter_name or "").strip()
    if not base and filename:
        base = filename.rsplit("/", 1)[-1]
        for ext in (".zip", ".tar.gz", ".tgz", ".tar", ".md"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
    slug = re.sub(r"[^a-z0-9._-]+", "-", base.lower()).strip("-")[:120]
    return slug or "default"


def _slot_rows(db, stype: str) -> list[GovernanceSkill]:
    """One row per slot — each slot's highest version (enabled or not, so the
    admin UI can show and toggle disabled slots too)."""
    rows = (db.query(GovernanceSkill)
            .filter(GovernanceSkill.skill_type == stype)
            .order_by(GovernanceSkill.version.desc()).all())
    newest: dict[str, GovernanceSkill] = {}
    for r in rows:
        newest.setdefault(getattr(r, "name", None) or "default", r)
    return [newest[k] for k in sorted(newest)]


def _meta(row: GovernanceSkill) -> SkillVersionMeta:
    rules = row.rules_json or []
    # Frontmatter name/description were validated at upload; re-derive cheaply.
    from app.agents.governance_skills import parse_frontmatter, parse_skill
    fm, _ = parse_frontmatter(row.content or "")
    try:
        mode = parse_skill(row.content or "")["mode"]
    except ValueError:
        mode = ""          # legacy/broken row — never 500 a listing over it
    manifest = getattr(row, "manifest_json", None) or []
    em_scripts = ((getattr(row, "exec_manifest_json", None) or {}).get("scripts") or [])
    return SkillVersionMeta(
        skill_type=row.skill_type,
        version=row.version,
        checksum=row.checksum,
        filename=row.filename,
        rule_count=len(rules),
        mode=mode,
        name=getattr(row, "name", None) or fm.get("name", "") or "default",
        enabled=bool(getattr(row, "enabled", True)),
        description=fm.get("description", ""),
        uploaded_by=row.uploaded_by,
        created_at=row.created_at.isoformat() if row.created_at else None,
        is_bundle=bool(getattr(row, "bundle_bytes", None)),
        file_count=len(manifest),
        script_count=len(em_scripts) if getattr(row, "bundle_bytes", None) else 0,
        smoke_status=getattr(row, "smoke_status", None),
        safety_warnings=len(getattr(row, "safety_warnings_json", None) or []),
        **_gating_meta(row, em_scripts),
    )


def _gating_meta(row: GovernanceSkill, em_scripts: list) -> dict:
    """Derive contract_source / gating / advisory for the response — so a skill
    that will NOT deterministically gate says so out loud (the silent-degradation
    risk: a security bundle whose scanners run agent-driven only)."""
    source = ((getattr(row, "provenance_json", None) or {}).get("contract_source")) or "none"
    has_validator = any((s or {}).get("role") == "validator" for s in em_scripts)
    if not getattr(row, "bundle_bytes", None) or not em_scripts:
        gating = "reasoning"          # script-less skill (EA's normal case)
    elif has_validator:
        gating = "deterministic"      # validator scripts form the hard floor
    else:
        gating = "agent_driven"       # scripts run only as the LLM chooses
    advisory = None
    if gating == "agent_driven":
        advisory = (f"This bundle ships {len(em_scripts)} script(s) but declares no "
                    "validator, so they run only when the review agent chooses to — "
                    "there is NO deterministic gate. Add a metadata.governance block "
                    "to SKILL.md (or an exec_manifest) marking the gating script(s) "
                    "role: validator to enable the hard floor.")
    return {"contract_source": source, "gating": gating, "advisory": advisory}


def _slot_warning(db, stype: str, name: str) -> tuple[bool, str | None]:
    """(is_new_slot, warning). A NEW slot created while OTHER enabled slots of the
    type already exist means the stage will run BOTH rulebooks — the 0118 upgrade
    trap: re-uploading an existing skill whose frontmatter name changed forks a
    slot instead of superseding, leaving the old one active. Warn loudly."""
    existing = {n for (n,) in
                db.query(GovernanceSkill.name)
                .filter(GovernanceSkill.skill_type == stype,
                        GovernanceSkill.enabled.is_(True)).distinct().all()}
    is_new = name not in existing
    others = sorted(existing - {name})
    if is_new and others:
        return True, (
            f"This upload created a NEW slot '{name}'. The {stype} stage already runs "
            f"{len(others)} other enabled slot(s): {', '.join(others)}. It will now run "
            f"ALL of them together. If this upload was meant to REPLACE one of those, "
            f"disable the old slot in Admin → Governance Skills.")
    return is_new, None


def active_skill(db, stype: str) -> GovernanceSkill | None:
    return (db.query(GovernanceSkill)
            .filter(GovernanceSkill.skill_type == stype)
            .order_by(GovernanceSkill.version.desc())
            .first())


@router.get("/admin/governance-skills")
def list_skills(db: DbDep, _: AdminUser):
    """Per type: the primary active skill (back-compat, null when none enabled)
    plus EVERY slot (``<stype>_skills``) — a type holds several skills side by
    side and the stage executes all enabled slots."""
    out = {}
    for stype in SKILL_TYPES:
        slots = _slot_rows(db, stype)
        enabled = [r for r in slots if getattr(r, "enabled", True)]
        primary = max(enabled, key=lambda r: r.version) if enabled else None
        out[stype] = _meta(primary).model_dump() if primary else None
        out[f"{stype}_skills"] = [_meta(r).model_dump() for r in slots]
    return out


class SlotToggle(BaseModel):
    enabled: bool


@router.post("/admin/governance-skills/{stype}/slots/{name}/enabled")
def set_slot_enabled(stype: str, name: str, body: SlotToggle, db: DbDep,
                     current: CurrentUser):
    """Retire (or re-enable) a skill SLOT without touching the append-only audit
    rows — a disabled slot simply stops executing in the stage. Disabling the
    last enabled slot of a type is allowed but leaves the stage unable to start
    (the start endpoint fails loud), so the response says so."""
    _stype_or_404(stype)
    _require_skill_owner(stype, current)
    rows = (db.query(GovernanceSkill)
            .filter(GovernanceSkill.skill_type == stype,
                    GovernanceSkill.name == name).all())
    if not rows:
        raise HTTPException(status_code=404, detail=f"no slot {name!r} for {stype}")
    for r in rows:
        r.enabled = body.enabled
    db.commit()
    remaining = [r for r in _slot_rows(db, stype) if getattr(r, "enabled", True)]
    warning = None
    if not remaining:
        warning = (f"no enabled {stype} slot remains — governance cannot start "
                   "until one is enabled or uploaded")
    elif not body.enabled:
        # A run is pinned to its skill set at creation (immutable for audit
        # reproducibility), so disabling a slot does NOT stop a stage already
        # executing it — it only affects the NEXT start. Say so, so the operator
        # resets if they meant to kill a misfiring slot mid-flight.
        from app.models.agentic import AgenticRun
        gov_kind = "gov_ea" if stype == "ea" else "gov_is"
        active_now = (db.query(AgenticRun.id)
                      .filter(AgenticRun.kind == gov_kind,
                              AgenticRun.status == "active").first() is not None)
        if active_now:
            warning = (f"a {stype} governance stage is currently running — it stays "
                       f"pinned to the '{name}' slot it started with. Disabling takes "
                       "effect on the NEXT start; use Reset & re-run to apply it now.")
    logger.info("Governance skill slot %s/%s → enabled=%s by %s (%d row(s))",
                stype, name, body.enabled, current.id, len(rows))
    return {"skill_type": stype, "name": name, "enabled": body.enabled,
            "versions_updated": len(rows),
            "enabled_slots_remaining": len(remaining),
            "warning": warning}


@router.get("/admin/governance-skills/{stype}/versions")
def list_skill_versions(stype: str, db: DbDep, _: AdminUser):
    _stype_or_404(stype)
    rows = (db.query(GovernanceSkill)
            .filter(GovernanceSkill.skill_type == stype)
            .order_by(GovernanceSkill.version.desc()).all())
    return {"versions": [_meta(r).model_dump() for r in rows]}


@router.get("/admin/governance-skills/{stype}/versions/{version}", response_model=SkillVersionDetail)
def get_skill_version(stype: str, version: int, db: DbDep, _: AdminUser):
    _stype_or_404(stype)
    row = (db.query(GovernanceSkill)
           .filter(GovernanceSkill.skill_type == stype,
                   GovernanceSkill.version == version).first())
    if row is None:
        raise HTTPException(status_code=404, detail=f"no version {version} for {stype}")
    return SkillVersionDetail(**_meta(row).model_dump(), content=row.content or "",
                              rules=row.rules_json or [])


@router.post("/admin/governance-skills/{stype}/upload", response_model=SkillVersionDetail)
async def upload_skill(stype: str, db: DbDep, current: AdminUser, file: UploadFile = File(...)):
    """Upload a new skill version. Append-only: every upload INSERTs max+1.

    Fails loud on an unparseable rulebook (400 listing every problem) —
    governance rules are enforced completely or not at all, so ambiguity is
    rejected at the door.
    """
    _stype_or_404(stype)
    raw = await file.read()
    if len(raw) > MAX_SKILL_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"File exceeds {MAX_SKILL_BYTES} bytes ceiling")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"File is not valid UTF-8: {e}")
    try:
        parsed = validate_skill(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Skill rejected: {e}")

    prev = active_skill(db, stype)          # global per-type numbering (any slot)
    slot = _slot_name(parsed.get("name", ""), file.filename)
    is_new, warn = _slot_warning(db, stype, slot)   # BEFORE insert (excludes this row)
    row = GovernanceSkill(
        skill_type=stype,
        version=(prev.version + 1) if prev else 1,
        name=slot,
        content=content,
        checksum=parsed["checksum"],
        filename=file.filename,
        rules_json=parsed["rules"],
        uploaded_by=current.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("Governance skill %s v%d slot=%s uploaded by %s (%d bytes, %d rules)%s",
                stype, row.version, slot, current.id, len(raw), len(parsed["rules"]),
                " [NEW SLOT beside existing]" if warn else "")
    return SkillVersionDetail(**{**_meta(row).model_dump(), "is_new_slot": is_new,
                                 "slot_warning": warn}, content=content,
                              rules=row.rules_json or [])


# ── Skill BUNDLES (Agent-Skill shape: SKILL.md + scripts + references) ───────
# Authorization (user decision, no maker-checker): ADMIN always; the InfoSec
# skill additionally accepts INFOSEC_REVIEWER (the existing role). Uploads are
# immediately active; uploaded_by + provenance recorded for audit.

def _require_skill_owner(stype: str, user) -> None:
    from app.models.user import UserRole
    allowed = {UserRole.ADMIN}
    if stype == "infosec":
        allowed.add(UserRole.INFOSEC_REVIEWER)
    if user.role not in allowed:
        raise HTTPException(status_code=403,
                            detail=f"uploading the {stype} governance skill requires "
                                   f"{' or '.join(sorted(r.value for r in allowed))}")


@router.post("/admin/governance-skills/{stype}/bundle", response_model=SkillVersionDetail)
async def upload_skill_bundle(stype: str, db: DbDep, current: CurrentUser,
                              file: UploadFile = File(...),
                              exec_manifest: str | None = Form(None)):
    """Upload a full skill BUNDLE (.zip / .tar.gz): SKILL.md + scripts + references.

    Pipeline: traversal-safe extraction → file classification → STATIC SAFETY GATE
    (privilege / docker / download-exec / secret-env reads REJECT; network-capable
    imports recorded as warnings) → SKILL.md structural validation (same three-mode
    parser as the single-doc path) → exec-manifest contract validation (undeclared
    scripts default to generator and can never gate). Scripted bundles land with
    smoke_status='pending' — they cannot gate a change until the prove-it-runs
    smoke is green (POST .../smoke)."""
    import json as _json

    from app.agents import governance_bundle as GB

    _stype_or_404(stype)
    _require_skill_owner(stype, current)
    raw = await file.read()
    try:
        parsed = GB.parse_bundle(raw, file.filename or "bundle.tar.gz")
        skill_summary = validate_skill(parsed.skill_md_text)       # SKILL.md contract
        # Execution contract precedence (universal-standard compatible):
        #   1. explicit exec_manifest form field (power-user / backward-compat), else
        #   2. SKILL.md's own frontmatter (metadata.governance) — a standard bundle
        #      is then SELF-DESCRIBING and uploads with nothing but the archive, else
        #   3. none → scripts run agent-driven with no deterministic floor.
        if exec_manifest:
            raw_contract, contract_source = _json.loads(exec_manifest), "manifest"
        elif (raw_contract := GB.exec_contract_from_frontmatter(parsed.skill_md_text)):
            contract_source = "skill_md"
        else:
            raw_contract, contract_source = None, "none"
        em = GB.validate_exec_manifest(raw_contract, parsed)
    except (GB.BundleError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Bundle rejected: {e}")

    prev = active_skill(db, stype)          # global per-type numbering (any slot)
    slot = _slot_name(skill_summary.get("name", ""), file.filename)
    is_new, warn = _slot_warning(db, stype, slot)   # BEFORE insert (excludes this row)
    has_scripts = bool(parsed.scripts)
    row = GovernanceSkill(
        skill_type=stype,
        version=(prev.version + 1) if prev else 1,
        name=slot,
        content=parsed.skill_md_text,                # SKILL.md == the injectable doc
        checksum=skill_summary["checksum"],
        filename=parsed.skill_md_path,
        rules_json=skill_summary["rules"],
        bundle_bytes=raw,
        bundle_sha256=GB.bundle_sha256(raw),
        bundle_filename=file.filename,
        manifest_json=parsed.manifest(),
        exec_manifest_json=em,
        safety_warnings_json=parsed.warnings,
        provenance_json={"source": "archive", "filename": file.filename,
                         "contract_source": contract_source},
        smoke_status="pending" if has_scripts else None,
        uploaded_by=current.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("Governance skill BUNDLE %s v%d by %s: %d file(s), %d script(s), %d warning(s)",
                stype, row.version, current.id, len(parsed.files), len(parsed.scripts),
                len(parsed.warnings))
    return SkillVersionDetail(**{**_meta(row).model_dump(), "is_new_slot": is_new,
                                 "slot_warning": warn}, content=row.content,
                              rules=row.rules_json or [])


@router.post("/admin/governance-skills/{stype}/versions/{version}/smoke")
def smoke_skill_bundle(stype: str, version: int, db: DbDep, current: CurrentUser):
    """The prove-it-runs gate (design §7 — WE run the scripts): every script must
    execute in the sandbox and parse per contract; every validator must flag the
    known-bad fixture. Scripted bundles gate nothing until this returns green."""
    import tempfile
    from pathlib import Path

    from app.agents import governance_sandbox as GSB

    _stype_or_404(stype)
    _require_skill_owner(stype, current)
    row = (db.query(GovernanceSkill)
           .filter(GovernanceSkill.skill_type == stype,
                   GovernanceSkill.version == version).first())
    if row is None:
        raise HTTPException(status_code=404, detail=f"no version {version} for {stype}")
    if not row.bundle_bytes:
        raise HTTPException(status_code=409, detail="this version has no bundle (markdown-only skill)")
    if not ((row.exec_manifest_json or {}).get("scripts") or []):
        row.smoke_status = None
        db.commit()
        return {"status": "no_scripts"}
    with tempfile.TemporaryDirectory(prefix="govsmoke-") as td:
        result = GSB.smoke_bundle(row, Path(td))
    row.smoke_status = result["status"]
    row.smoke_detail_json = result
    db.commit()
    logger.info("Governance bundle smoke %s v%d → %s", stype, version, result["status"])
    return result


# ── Governance flow: start the stage sequence + derived status ────────────────

@router.post("/changes/{change_id}/governance/start")
def start_governance(change_id: str, db: DbDep, current_user: AgenticUser):
    """One manual start (user decision #3): kicks off the EA stage; from here the
    sequence runs unattended (EA → fix → gate → InfoSec → fix → gate), chained
    server-side. Idempotent — a second click while a stage is active returns 409;
    a failed stage is retried by starting again (a fresh stage run is created).
    """
    from app.agents import governance_orchestrator as G
    from app.models.agentic import AgenticRun, AgenticRunRepo

    if not getattr(settings, "governance_reviews_enabled", False):
        raise HTTPException(409, "governance reviews are disabled (governance_reviews_enabled=false)")
    parent = G.approved_parent_run(db, change_id)
    if parent is None:
        raise HTTPException(409, "no approved agentic code change for this change — approve it first")
    pushed = (db.query(AgenticRunRepo.run_id)
              .filter(AgenticRunRepo.run_id == parent.id,
                      AgenticRunRepo.push_state == "pushed").first() is not None)
    deferred = bool((parent.handoff_json or {}).get("push_deferred"))
    if parent.status != "completed" or not (pushed or deferred):
        raise HTTPException(409, "the approved run is not finished — approve & push (or push later) first")
    # Fail loud only when a rulebook is MISSING (decision #6): starting a sequence
    # whose second stage cannot run would strand the change mid-pipeline.
    problems, warnings = [], []
    for stype in SKILL_TYPES:
        try:
            # Smoke is ADVISORY (user decision): a scripted bundle whose prove-it-runs
            # smoke is not green STILL runs — we WARN rather than block. The stage
            # surfaces a persistent "smoke failed" banner, and any validator that
            # genuinely can't run becomes a must-block finding in the floor.
            for row in G.active_skills(db, stype):
                if getattr(row, "bundle_bytes", None) and \
                        ((row.exec_manifest_json or {}).get("scripts") or []) and \
                        row.smoke_status != "green":
                    warnings.append(f"the {stype} skill '{getattr(row, 'name', 'default')}' "
                                    f"v{row.version} has scripts but its "
                                    f"smoke check is {row.smoke_status or 'pending'} — it will run, "
                                    "but its automated findings may be unreliable")
        except RuntimeError as e:
            problems.append(str(e))     # skill missing / unparseable — still hard-blocks
    if problems:
        raise HTTPException(409, " | ".join(problems))
    active = (db.query(AgenticRun)
              .filter(AgenticRun.change_request_id == change_id,
                      AgenticRun.status == "active").first())
    if active is not None:
        raise HTTPException(409, f"another agentic run is active for this change "
                                 f"({active.kind} run {active.id[:8]}, phase {active.phase})")
    status_now = G.governance_status(db, change_id)
    if status_now["all_passed"] and status_now["started"]:
        return {"started": False, "already_passed": True, **status_now}
    # Resume from the failed/pending stage: EA already passed → start InfoSec.
    kind = "gov_is" if status_now["ea"]["passed"] else "gov_ea"
    run, created = G.create_stage_run(db, parent, kind, created_by=current_user.id)
    db.commit()
    if created:
        from app.services.celery_tasks import agentic_drive_task
        try:
            agentic_drive_task.delay(run.id)
        except Exception:  # noqa: BLE001 — row is committed; re-arm as a stale lease
            # so the recovery sweep re-dispatches (a lease-free active run is
            # invisible to recover_runs), instead of stranding an undispatched
            # run that 409s every later /start.
            logger.exception("governance start: dispatch failed for %s", run.id)
            from app.models.base import utcnow
            run.lease_owner = "governance.start:retry"
            run.lease_expires_at = utcnow()
            db.commit()
    return {"started": created, "run_id": run.id, "kind": kind,
            "smoke_warnings": warnings,
            **G.governance_status(db, change_id)}


@router.post("/changes/{change_id}/governance/reset")
def reset_governance_reviews(change_id: str, db: DbDep, current: AdminUser):
    """TESTING provision: supersede every governance stage run for the change's
    current approved parent so the next /start runs a fresh EA → InfoSec pass
    from scratch. Uncommitted fixer edits are reverted from the cited-file
    snapshots; superseded runs stay in the audit trail but stop counting.
    Admin-only — this withdraws recorded stage outcomes."""
    from app.agents import governance_orchestrator as G

    out = G.reset_governance(db, change_id, requested_by=current.id)
    if not out.get("reset"):
        raise HTTPException(409, out.get("reason", "governance reset refused"))
    return {**out, **G.governance_status(db, change_id)}


@router.get("/changes/{change_id}/governance/status")
def get_governance_status(change_id: str, db: DbDep, _: AgenticUser):
    """Derived stage status for the Phase-B page cards. Includes whether both
    skills are uploaded so the UI can explain a disabled start button."""
    from app.agents import governance_orchestrator as G
    out = G.governance_status(db, change_id)

    # Cheap existence check — NOT active_skills() (which loads + integrity-checks
    # every version row's content on the 5s poll path). "At least one enabled slot
    # per type" is all the UI's start-button gating needs; the real integrity gate
    # runs once at stage creation.
    enabled_types = {t for (t,) in
                     db.query(GovernanceSkill.skill_type)
                     .filter(GovernanceSkill.enabled.is_(True),
                             GovernanceSkill.skill_type.in_(SKILL_TYPES))
                     .distinct().all()}
    out["skills_ready"] = all(s in enabled_types for s in SKILL_TYPES)
    return out
