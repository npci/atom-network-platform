# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pack store over `sim_packs` — immutability enforced at the edge.

A stored pack's `content` is the stamped canonical data and is never edited:
`save_draft` refuses a `pack_ref` that already exists (a new revision is a
new ref), and refuses content whose `pack_id` does not verify. Publishing
flips status after the capability gate; withdrawing flips it back out of
resolution — rows are never deleted, because a certification report must stay
readable against the contract as it stood.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.models.sim_pack import SimPackPublication, SimPackRecord
from app.services.sim_packs.contract import (
    PackValidationError,
    SimPack,
    validate_pack,
)
from app.services.simulator.engine import engine_shortfalls

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["StoreError", "save_draft", "publish", "withdraw", "get",
           "active_baseline"]


class StoreError(ValueError):
    """A store operation refused, with the reason in the message."""


def get(db: "Session", pack_ref: str) -> SimPackRecord | None:
    return db.query(SimPackRecord).filter(
        SimPackRecord.pack_ref == pack_ref).first()


def active_baseline(db: "Session") -> SimPackRecord | None:
    """The most recently published ROOT pack (no base_pack_ref) — what an
    absent `?pack=` resolves against. None = the pre-pack world."""
    return (db.query(SimPackRecord)
            .filter(SimPackRecord.base_pack_ref.is_(None),
                    SimPackRecord.status == "published")
            .order_by(SimPackRecord.published_at.desc())
            .first())


def save_draft(db: "Session", pack: SimPack, *,
               created_by: str | None = None) -> SimPackRecord:
    """Store a stamped pack as a draft. Refuses an existing ref (immutability:
    editing means a new revision, so a new ref) and unverifiable content."""
    if pack.pack_id is None:
        raise StoreError("pack is not stamped — stamp() before storing")
    # Re-validate including the pack_id integrity check.
    try:
        validate_pack(pack.canonical_dict())
    except PackValidationError as exc:
        raise StoreError(str(exc)) from exc
    if get(db, pack.pack_ref) is not None:
        raise StoreError(
            f"pack_ref {pack.pack_ref!r} already exists — packs are immutable; "
            "a changed pack is a NEW revision under a new ref")
    if pack.base_pack is not None and get(db, pack.base_pack) is None \
            and not _is_root_marker(pack):
        raise StoreError(
            f"base pack {pack.base_pack!r} is not in the store — publish the "
            "chain root first")

    coverage = None
    if pack.provenance is not None and pack.provenance.coverage is not None:
        coverage = pack.provenance.coverage.model_dump()
    row = SimPackRecord(
        pack_ref=pack.pack_ref,
        pack_id=pack.pack_id,
        change_request_id=pack.change_id,
        base_pack_ref=None if _is_root_marker(pack) else pack.base_pack,
        engine_min=pack.engine_min,
        requires=list(pack.requires) or None,
        content=pack.canonical_dict(),
        coverage=coverage,
        status="draft",
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    return row


def _is_root_marker(pack: SimPack) -> bool:
    """A root (baseline) pack declares itself its own base — the contract has
    no nullable base_pack, and self-reference is unambiguous."""
    return pack.base_pack == pack.pack_ref


def publish(db: "Session", pack_ref: str, *,
            actor: str | None = None) -> SimPackRecord:
    """The capability gate. Refusal names every shortfall — it is the scope
    of the next engine release, not a mystery.

    `actor` is recorded on the publication row: publishing changes what
    "certified" means, so WHO did it is part of the evidence (plan S-5). The
    API layer additionally rides the admin-action audit middleware.
    """
    row = get(db, pack_ref)
    if row is None:
        raise StoreError(f"unknown pack {pack_ref!r}")
    if row.status == "published":
        return row     # idempotent

    # NET-F24: refuse a pack whose CONTRACT is identical to the base it layers
    # on. `pack_id` is a content address over the canonical contract, so the
    # comparison is exact and computes nothing new — pack_ref, base_pack and
    # generated_at are excluded from the hash by design, which is exactly why a
    # metadata-only revision lands here instead of slipping through.
    #
    # `build` carries the same guard, but THIS is the boundary that matters: a
    # no-op draft is a curiosity, a published one is the defect, because publish
    # is the last point before a pack enters the store we certify against. A
    # round dispatched on such a pack grades the partner against BASELINE
    # content while every label says it is testing a change — the "certified
    # against baseline" failure both plans name, reached through the builder
    # rather than through query normalisation.
    #
    # A caller who genuinely wants to re-run the base should dispatch the base,
    # honestly labelled, rather than have a no-op inferred from silence.
    if row.base_pack_ref:
        base = get(db, row.base_pack_ref)
        if base is not None and base.pack_id == row.pack_id:
            raise StoreError(
                f"{pack_ref!r} is identical to its base {row.base_pack_ref!r} "
                f"(both {row.pack_id}) — it certifies nothing. Publishing it "
                f"would grade a partner against baseline content under a "
                f"change's label. Dispatch {row.base_pack_ref!r} directly if a "
                f"baseline re-run is what you want.")

    shortfalls = engine_shortfalls(engine_min=row.engine_min,
                                   requires=list(row.requires or []))
    if shortfalls:
        raise StoreError(
            f"engine cannot honour {pack_ref!r}: " + "; ".join(shortfalls))
    row.status = "published"
    row.published_at = datetime.now(timezone.utc)
    db.add(SimPackPublication(pack_ref=row.pack_ref, pack_id=row.pack_id,
                              target="local", response_status=200,
                              echoed_pack_id=row.pack_id,
                              published_by=actor))
    db.commit()
    logger.info("sim_packs: published %s (%s)", row.pack_ref, row.pack_id)
    return row


def withdraw(db: "Session", pack_ref: str) -> SimPackRecord:
    """Take a pack out of resolution WITHOUT deleting it. A withdrawn ref on
    the wire is a 400, never a fallback."""
    row = get(db, pack_ref)
    if row is None:
        raise StoreError(f"unknown pack {pack_ref!r}")
    row.status = "withdrawn"
    db.commit()
    logger.info("sim_packs: withdrew %s", row.pack_ref)
    return row
