# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Capability-pack API (SIM-2/SIM-5) — the simulator's pack surface.

Store (draft) → publish (the capability gate) → resolve. `/capabilities`
advertises what the engine can honour; a publish refusal is a
machine-readable feature request (422 naming every shortfall). `/effective`
is the fully-merged 2am view. DELETE withdraws — rows are never deleted, and
a withdrawn ref on the wire is a 400, never a fallback.

**Every MUTATING route is admin-gated, reads are not** (plan S-5: "publishing
is authenticated and audited — a pack changes what 'certified' means"). The
admin dependency is also what `AdminActionAuditMiddleware` keys on — it
records a row only once `require_admin` has granted access — so gating these
on `AdminUser` is what makes them audited at all; `CurrentUser` would have
left publishing unaudited by construction. Reads stay open to any
authenticated operator: reviewing a pack before publish is the point.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.core.deps import AdminUser, CurrentUser, DbDep
from app.services.sim_packs.contract import PackValidationError, validate_pack
from app.services.simulator import engine, resolver, store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sim/packs", tags=["simulator"])


def _row_out(row) -> dict:
    return {
        "pack_ref": row.pack_ref, "pack_id": row.pack_id,
        "status": row.status, "base_pack_ref": row.base_pack_ref,
        "change_request_id": row.change_request_id,
        "engine_min": row.engine_min, "requires": row.requires or [],
        "coverage": row.coverage,
        "created_by": row.created_by,
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }


# Literal paths BEFORE /{pack_ref} — registration order is the router's match order.
@router.get("/capabilities")
def capabilities(_: CurrentUser) -> dict:
    return {"engine_version": engine.ENGINE_VERSION,
            "capabilities": sorted(engine.CAPABILITIES)}


@router.post("/build", status_code=201)
def build_from_change(body: dict, db: DbDep, user: AdminUser) -> dict:
    """The SIM-5 publish path, first half: project a change's registry delta
    (and, when given, its round's request variants) into a DRAFT layered on
    the active baseline. The operator reviews the coverage note — gaps
    included — before publishing; publish stays a separate, gated call.

    Refusals: no published baseline is a 409 (nothing to layer on — publish
    the root first); an EMPTY delta is a 422 `empty_delta`, because an empty
    pack over a baseline would be indistinguishable from "nothing changed"
    and hide a broken delta.
    """
    change_id = body.get("change_id") or ""
    revision = body.get("revision") or 1
    if not change_id:
        raise HTTPException(status_code=400, detail="change_id is required")

    base = store.active_baseline(db)
    if base is None:
        raise HTTPException(
            status_code=409,
            detail="no published baseline to layer on — build and publish "
                   "the root pack first")

    variants = []
    if body.get("cflow_id") and body.get("run_number"):
        from app.models.phase_c import CertRequestVariant

        variants = (db.query(CertRequestVariant)
                    .filter(CertRequestVariant.cflow_id == body["cflow_id"],
                            CertRequestVariant.run_number == int(body["run_number"]))
                    .order_by(CertRequestVariant.variant_id).all())

    from app.services.sim_packs import builder

    pack = builder.build_pack(
        db, change_id=change_id, pack_ref=f"{change_id}@{revision}",
        base_pack_ref=base.pack_ref, variants=variants,
        routes=body.get("routes") or None)
    if pack is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "empty_delta",
                    "detail": f"change {change_id} touched no registry rows — "
                              "no pack built; an empty pack would hide a "
                              "broken delta"})
    # NET-F24: the `pack is None` check above catches a delta that is empty BY
    # ROW COUNT. It does NOT catch one that is empty BY CONTENT — a change
    # touching rows that are unchanged from the base yields a pack whose
    # contract hashes identically to it. That is the state the docstring above
    # describes ("indistinguishable from nothing changed") and the state the
    # count check misses. pack_id is a content address, so the test is exact.
    if pack.pack_id == base.pack_id:
        raise HTTPException(
            status_code=422,
            detail={"error": "no_op_pack",
                    "detail": f"change {change_id} produces a contract identical "
                              f"to base {base.pack_ref} (both {pack.pack_id}) — "
                              "it certifies nothing. Either the delta is broken "
                              "or this change altered no contract; dispatch the "
                              "base directly for a baseline re-run.",
                    "pack_id": pack.pack_id,
                    "base_pack_ref": base.pack_ref})
    try:
        row = store.save_draft(db, pack,
                               created_by=getattr(user, "username", None))
    except store.StoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {**_row_out(row),
            "review": {"summary_gaps": (row.coverage or {}).get("gaps", []),
                       "scenarios": len(pack.scenarios)}}


@router.get("")
def list_packs(db: DbDep, _: CurrentUser) -> dict:
    from app.models.sim_pack import SimPackRecord

    rows = db.query(SimPackRecord).order_by(SimPackRecord.created_at).all()
    return {"packs": [_row_out(r) for r in rows]}


@router.post("", status_code=201)
def store_pack(body: dict, db: DbDep, user: AdminUser) -> dict:
    """Store a stamped pack as a DRAFT. Structural + integrity validation
    here; the capability gate runs at publish."""
    try:
        pack = validate_pack(body)
        row = store.save_draft(db, pack,
                               created_by=getattr(user, "username", None))
    except (PackValidationError, store.StoreError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _row_out(row)


@router.post("/{pack_ref}/publish")
def publish_pack(pack_ref: str, db: DbDep, user: AdminUser) -> dict:
    """The §4.3 gate: a refusal names exactly what the engine lacks — that
    list is the scope of the next engine release."""
    try:
        return _row_out(store.publish(
            db, pack_ref,
            actor=getattr(user, "username", None) or getattr(user, "id", None)))
    except store.StoreError as exc:
        status = 404 if "unknown pack" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc))


@router.delete("/{pack_ref}")
def withdraw_pack(pack_ref: str, db: DbDep, user: AdminUser) -> dict:
    try:
        return _row_out(store.withdraw(db, pack_ref))
    except store.StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{pack_ref}")
def get_pack(pack_ref: str, db: DbDep, _: CurrentUser) -> dict:
    row = store.get(db, pack_ref)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown pack {pack_ref!r}")
    return {**_row_out(row), "content": row.content}


@router.get("/{pack_ref}/diff")
def diff_pack(pack_ref: str, db: DbDep, _: CurrentUser,
              against: str | None = None) -> dict:
    """What publishing this pack would CHANGE (S-5's review surface).

    Defaults to diffing against the pack's own chain parent — the common
    question is "what does this add to what is already published". `against`
    names any other ref explicitly (e.g. the previous revision of the same
    change). Both sides resolve their full chains first, so the comparison is
    between EFFECTIVE contracts, not between layer fragments — a field the
    baseline supplies and this pack does not override must not read as
    missing.
    """
    from app.services.sim_packs.diff import diff_packs

    try:
        after = resolver.resolve(db, pack_ref, include_draft=True)
    except resolver.UnknownPackError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    base_ref = against or store.get(db, pack_ref).base_pack_ref
    before = None
    if base_ref and base_ref != pack_ref:
        try:
            before = resolver.resolve(db, base_ref, include_draft=True).content
            before = {**before, "pack_ref": base_ref}
        except resolver.UnknownPackError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
    return {"pack_ref": pack_ref, "pack_id": after.pack_id,
            "diff": diff_packs(before, after.content)}


@router.get("/{pack_ref}/effective")
def effective_pack(pack_ref: str, db: DbDep, _: CurrentUser) -> dict:
    """The fully-merged view (`baseline ⊕ … ⊕ pack`) — what a request
    selecting this ref actually runs against. Works for drafts too: review
    happens before publish."""
    try:
        resolved = resolver.resolve(db, pack_ref, include_draft=True)
    except resolver.UnknownPackError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"pack_ref": resolved.pack_ref, "pack_id": resolved.pack_id,
            "chain": resolved.chain, "content": resolved.content}
