# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""API Registry — admin surface for the canonical network wire-API spec store.

  GET    /api-registry/messages              — list messages (+field counts)
  GET    /api-registry/messages/{id}         — one message + ordered field rows
  PATCH  /api-registry/messages/{id}         — edit description
  PATCH  /api-registry/fields/{field_id}     — edit a field's documented constraints
  POST   /api-registry/ingest                — populate/refresh from XSDs (deterministic)
  POST   /api-registry/harvest-code          — attach tier-1 code-constraint evidence

Reads are open to any logged-in user; mutations require admin. Human edits
set ``updated_by``, which locks the row's canonical values against future
ingest overwrites (the fresh XSD facts still land in constraint_sources so
drift stays visible).
"""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, text

from app.core.deps import AdminUser, CurrentUser, DbDep
from app.models.api_registry import ApiField, ApiMessage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api-registry", tags=["api-registry"])


class _NulSafe(BaseModel):
    """Reject NUL bytes at the boundary. Postgres text can't store 0x00 — psycopg
    raises at flush, which would surface as a 500 on client-controllable input.
    Turn it into a clean 422 for every string/list-of-string field."""
    @field_validator("*")
    @classmethod
    def _reject_nul(cls, v):
        vals = v if isinstance(v, list) else [v]
        if any(isinstance(x, str) and "\x00" in x for x in vals):
            raise ValueError("NUL (0x00) characters are not allowed")
        return v


class IngestRequest(_NulSafe):
    xsd_dir: str | None = None


class HarvestRequest(_NulSafe):
    java_dir: str | None = None


class MessagePatch(_NulSafe):
    description: str | None = None


class FieldPatch(_NulSafe):
    # max_lengths mirror the column widths — over-width input must 422, not 500.
    message_item: str | None = None
    occurrence: str | None = Field(None, max_length=20)
    datatype: str | None = Field(None, max_length=60)
    length_rule: str | None = Field(None, max_length=200)
    mandatory: str | None = Field(None, max_length=5)
    condition_text: str | None = None
    rules_ref: str | None = Field(None, max_length=500)
    pattern_rule: str | None = Field(None, max_length=500)
    enum_values: list[str] | None = None


_AUDIT_VALUE_CAP = 500  # per from/to value; mirrors the code-harvest snippet cap


def _clip_audit(v):
    """Bound a single audit value so a huge edit doesn't bloat constraint_sources
    (shipped in full on every GET). Non-strings pass through untouched."""
    if isinstance(v, str) and len(v) > _AUDIT_VALUE_CAP:
        return v[:_AUDIT_VALUE_CAP] + f"…(+{len(v) - _AUDIT_VALUE_CAP} chars)"
    return v


def _field_out(f: ApiField) -> dict:
    cs = f.constraint_sources or {}
    code_ev = (cs.get("code") or {}).get("evidence") or []
    return {
        "id": f.id,
        "parent_field_id": f.parent_field_id,
        "position": f.position,
        "depth": f.depth,
        "tag_num": f.tag_num,
        "xml_tag": f.xml_tag,
        "is_attribute": f.is_attribute,
        "xpath": f.xpath,
        "message_item": f.message_item,
        "occurrence": f.occurrence,
        "datatype": f.datatype,
        "length_rule": f.length_rule,
        "mandatory": f.mandatory,
        "condition_text": f.condition_text,
        "rules_ref": f.rules_ref,
        "pattern_rule": f.pattern_rule,
        "xsd_pattern": (cs.get("xsd") or {}).get("pattern"),
        "enum_values": f.enum_values,
        "source": f.source,
        "status": f.status,
        "edited": f.updated_by is not None,
        "updated_by": f.updated_by,
        "has_code_evidence": bool(code_ev),
        "has_conflict": any(e.get("conflict_with_xsd") for e in code_ev),
        "constraint_sources": cs,
    }


@router.get("/messages")
def list_messages(db: DbDep, _: CurrentUser):
    counts = dict(
        db.query(ApiField.message_id, func.count(ApiField.id))
        .filter(ApiField.status == "active")
        .group_by(ApiField.message_id).all()
    )
    out = []
    for m in db.query(ApiMessage).order_by(ApiMessage.api_name).all():
        out.append({
            "id": m.id, "api_name": m.api_name, "direction": m.direction,
            "namespace": m.namespace, "description": m.description,
            "source": m.source, "status": m.status,
            "field_count": counts.get(m.id, 0),
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        })
    return {"messages": out}


@router.get("/messages/{message_id}")
def get_message(message_id: str, db: DbDep, _: CurrentUser):
    m = db.get(ApiMessage, message_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Unknown message")
    fields = (db.query(ApiField)
              .filter(ApiField.message_id == m.id, ApiField.status.in_(["active", "stale"]))
              .order_by(ApiField.position).all())
    return {
        "id": m.id, "api_name": m.api_name, "direction": m.direction,
        "namespace": m.namespace, "description": m.description,
        "sample_xml": m.sample_xml, "source": m.source,
        "source_schema_path": m.source_schema_path,
        "fields": [_field_out(f) for f in fields],
    }


@router.patch("/messages/{message_id}")
def patch_message(message_id: str, payload: MessagePatch, db: DbDep, current: AdminUser):
    m = db.get(ApiMessage, message_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Unknown message")
    changed = False
    if payload.description is not None:
        # Mirror patch_field: "" is the explicit clear signal, and a no-change
        # PATCH must not stamp updated_by.
        desc = payload.description or None
        if desc != m.description:
            m.description = desc
            m.updated_by = current.email or current.id
            changed = True
    db.commit()
    # `changed` lets the UI say "no changes to save" instead of "saved" on a no-op.
    return {"ok": True, "changed": changed}


@router.patch("/fields/{field_id}")
def patch_field(field_id: str, payload: FieldPatch, db: DbDep, current: AdminUser):
    f = db.get(ApiField, field_id)
    if f is None:
        raise HTTPException(status_code=404, detail="Unknown field")
    changed = {}
    for col in ("message_item", "occurrence", "datatype", "length_rule",
                "mandatory", "condition_text", "rules_ref", "pattern_rule", "enum_values"):
        val = getattr(payload, col)
        if val is None:
            continue  # absent → no change
        # Explicit clear: empty string / empty list means "unset this cell".
        if val == "" or val == []:
            val = None
        if val != getattr(f, col):
            changed[col] = {"from": _clip_audit(getattr(f, col)), "to": _clip_audit(val)}
            setattr(f, col, val)
    if changed:
        f.updated_by = current.email or current.id
        cs = dict(f.constraint_sources or {})
        manual = list(cs.get("manual") or [])
        manual.append({"by": f.updated_by, "changes": changed})
        cs["manual"] = manual[-20:]
        f.constraint_sources = cs
    db.commit()
    return _field_out(f)


class ProductionSourcePut(_NulSafe):
    repo_id: str | None = None      # row to select; None clears (see gitlab_repo)
    gitlab_repo: str | None = None  # with repo_id None: clear only this repo's baseline


def _baseline_rows(db) -> list[dict]:
    """(repo_id, branch) of every row marked as a production baseline — the shape
    the workspace-clone discovery in the registry services takes."""
    from app.models.code_repo import CodeRepo

    rows = db.query(CodeRepo).filter(CodeRepo.is_registry_baseline.is_(True)).all()
    return [{"repo_id": r.id, "branch": r.gitlab_branch} for r in rows]


@router.get("/production-source")
def get_production_source(db: DbDep, _: CurrentUser):
    """Indexed (repo, branch) rows the admin can pick as the registry's production
    baseline sources — one selectable per repo (core AND app)."""
    from app.models.code_repo import CodeRepo

    repos = db.query(CodeRepo).order_by(CodeRepo.gitlab_repo, CodeRepo.gitlab_branch).all()
    return {
        "selected_ids": [r.id for r in repos if r.is_registry_baseline],
        "repos": [{
            "id": r.id, "label": r.label, "role": r.role,
            "gitlab_repo": r.gitlab_repo, "gitlab_branch": r.gitlab_branch,
            "indexed": r.last_indexed_at is not None,
            "selected": r.is_registry_baseline,
        } for r in repos],
    }


@router.put("/production-source")
def set_production_source(payload: ProductionSourcePut, db: DbDep, current: AdminUser):
    """Mark one indexed branch as its repo's production baseline (clears other rows
    of the SAME repo — one baseline per repo, so core and app can both have one).
    ``repo_id: null`` + ``gitlab_repo`` clears that repo's baseline; ``repo_id:
    null`` alone clears every selection."""
    from app.models.code_repo import CodeRepo

    # Serialize concurrent selects so clear → set-one is atomic across requests —
    # otherwise two overlapping PUTs each miss the other's pending row and both survive
    # (or both clobber, leaving zero). Postgres only; the partial unique index (0120)
    # is the hard guarantee that also covers SQLite / direct writes.
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('api_registry_baseline'))"))

    target = None
    if payload.repo_id is not None:
        target = db.get(CodeRepo, payload.repo_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Unknown repo")
    clear_q = db.query(CodeRepo).filter(CodeRepo.is_registry_baseline.is_(True))
    scope_repo = target.gitlab_repo if target is not None else payload.gitlab_repo
    if scope_repo is not None:
        clear_q = clear_q.filter(CodeRepo.gitlab_repo == scope_repo)
    for r in clear_q.all():
        r.is_registry_baseline = False
    # Flush the clears to the DB BEFORE setting the new target, else SQLAlchemy may
    # emit set-true before set-false and transiently have two baselines — which the
    # (immediate, non-deferrable) partial unique index rejects mid-transaction.
    db.flush()
    if target is not None:
        target.is_registry_baseline = True
    db.commit()
    logger.info("api-registry production source set to %s by %s",
                (target.gitlab_repo + "@" + target.gitlab_branch) if target else None, current.email)
    return {"ok": True, "selected_id": target.id if target else None}


@router.post("/ingest")
def ingest(payload: IngestRequest, db: DbDep, current: AdminUser):
    from app.core.config import settings
    from app.services.api_registry_ingest import discover_default_xsd_dirs, ingest_from_dir

    dirs = [Path(payload.xsd_dir)] if payload.xsd_dir else discover_default_xsd_dirs(_baseline_rows(db))
    dirs = [d for d in dirs if d.is_dir()]
    if not dirs:
        raise HTTPException(
            status_code=400,
            detail="No XSD directory found. Provide xsd_dir explicitly — none discovered "
                   "under knowledge_base/existing_xsds or workspace clones at "
                   f"{settings.agentic_workspace_root}.",
        )
    counts: dict = {}
    for d in dirs:
        for k, v in ingest_from_dir(db, d).items():
            counts[k] = counts.get(k, 0) + v
    logger.info("api-registry ingest by %s from %s: %s", current.email, dirs, counts)
    return {"xsd_dir": "; ".join(str(d) for d in dirs), **counts}


@router.post("/harvest-code")
def harvest_code(payload: HarvestRequest, db: DbDep, current: AdminUser):
    from app.core.config import settings
    from app.services.api_registry_code_harvest import discover_default_java_dirs, harvest_into_registry

    dirs = [Path(payload.java_dir)] if payload.java_dir else discover_default_java_dirs(_baseline_rows(db))
    dirs = [d for d in dirs if d.is_dir()]
    if not dirs:
        raise HTTPException(
            status_code=400,
            detail="No Java sources found in workspace clones under "
                   f"{settings.agentic_workspace_root} — clones appear there after an "
                   "agentic codegen run. Provide java_dir explicitly to scan another tree.",
        )
    counts = harvest_into_registry(db, dirs)
    logger.info("api-registry code harvest by %s from %s: %s", current.email, dirs, counts)
    return {"java_dir": "; ".join(str(d) for d in dirs), **counts}
