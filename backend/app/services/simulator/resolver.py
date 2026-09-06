# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Chain-merge pack resolution (`baseline ⊕ … ⊕ pack`), per request.

`resolve(db, ref)` walks the `base_pack_ref` chain leaf→root, then merges
root→leaf: a leaf's `apis[]` entry REPLACES the parent's for the same API
name (whole entry — partially-merged field tables would be a third contract
nobody wrote), and leaf scenarios take matching precedence over parent ones.
No global state — resolution affects this request only.

An unknown or WITHDRAWN ref raises `UnknownPackError` — the caller turns that
into HTTP 400 `unknown_pack`. Never soften this into a fallback: a silent
fallback certifies a bank against the old contract while the report says the
new one.

Merged results are cached by the chain's `pack_id` tuple — content addresses,
so entries are valid forever; the cache is only ever bounded, never
invalidated.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import TYPE_CHECKING, Any

from app.models.sim_pack import SimPackRecord
from app.services.simulator import store

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = ["UnknownPackError", "ResolvedPack", "resolve", "resolve_request"]

_CACHE: dict[tuple, "ResolvedPack"] = {}
_CACHE_MAX = 256


class UnknownPackError(LookupError):
    """Unknown or withdrawn pack ref — HTTP 400 `unknown_pack` at the edge."""


@dataclass
class ResolvedPack:
    pack_ref: str                       # the leaf — what the caller asked for
    pack_id: str
    chain: list[str]                    # refs, root first
    apis: dict[str, dict]               # lower(api name) -> merged entry
    scenarios: list[dict]               # leaf scenarios first (match order)
    content: dict = dc_field(default_factory=dict)   # merged view (the 2am endpoint)


def _load_chain(db: "Session", pack_ref: str, *,
                include_draft: bool) -> list[SimPackRecord]:
    """Leaf→root records for a ref; refuses the unknown, the withdrawn and
    the cyclic."""
    chain: list[SimPackRecord] = []
    seen: set[str] = set()
    ref: str | None = pack_ref
    while ref is not None:
        if ref in seen:
            raise UnknownPackError(
                f"pack chain for {pack_ref!r} is cyclic at {ref!r}")
        seen.add(ref)
        row = store.get(db, ref)
        if row is None:
            raise UnknownPackError(f"unknown pack {ref!r}")
        if row.status == "withdrawn":
            raise UnknownPackError(f"pack {ref!r} is withdrawn")
        if row.status != "published" and not include_draft:
            raise UnknownPackError(f"pack {ref!r} is not published")
        chain.append(row)
        ref = row.base_pack_ref
    return chain


def _merge(records: list[SimPackRecord]) -> ResolvedPack:
    """`records` leaf→root; merge root→leaf."""
    leaf = records[0]
    apis: dict[str, dict] = {}
    scenarios: list[dict] = []
    for row in reversed(records):                    # root first
        content = row.content or {}
        for api in content.get("apis", []):
            apis[str(api.get("api", "")).lower()] = api
        # Prepend later layers so the LEAF's scenarios match first.
        scenarios = list(content.get("scenarios", [])) + scenarios
    merged = dict(leaf.content or {})
    merged["apis"] = [apis[k] for k in sorted(apis)]
    merged["scenarios"] = scenarios
    return ResolvedPack(
        pack_ref=leaf.pack_ref,
        pack_id=leaf.pack_id,
        chain=[r.pack_ref for r in reversed(records)],
        apis=apis,
        scenarios=scenarios,
        content=merged,
    )


def resolve(db: "Session", pack_ref: str, *,
            include_draft: bool = False) -> ResolvedPack:
    records = _load_chain(db, pack_ref, include_draft=include_draft)
    # The REQUESTED ref is part of the key, not only the content hashes it
    # resolves to. Two refs can carry byte-identical content — a re-run round
    # published as `@r10` against the same delta as `@r5` — and a
    # content-only key hands the second caller the first one's ResolvedPack,
    # whose `pack_ref` then names a contract the caller never asked for.
    # Grading is unaffected (same bytes), but every downstream record of WHICH
    # contract certified the partner is wrong, and wrong nondeterministically:
    # it depends on which ref happened to warm the cache. `X-Sim-Pack` still
    # carries the pack_id, so the content identity is not lost by keying on
    # both.
    key = (pack_ref,) + tuple(r.pack_id for r in records) + (include_draft,)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    resolved = _merge(records)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()      # content-addressed: a cold cache is only slow
    _CACHE[key] = resolved
    return resolved


def resolve_request(db: "Session", pack_param: str | None) -> ResolvedPack | None:
    """The `?pack=` binding rule, §3.1: absent → active baseline (or None
    when no baseline is published — the pre-pack world, stated); present →
    resolve or raise. NEVER a fallback from a bad ref."""
    if pack_param:
        return resolve(db, pack_param)
    baseline = store.active_baseline(db)
    if baseline is None:
        return None
    return resolve(db, baseline.pack_ref)
