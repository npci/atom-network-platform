# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic XSD namespace canonicalization (THE BOOK §7.4 support).

XML namespaces are IDENTITY: to a parser and to every consumer on the wire, two
schemas with different ``targetNamespace`` strings are unrelated. Some
ecosystems carry MULTIPLE spellings of what is semantically the same authority
— UPI's schemas in the wild use both ``http://npci.org/upi/schema/`` (the
majority) and ``http://www.npci.org.in/upi/schema/`` (a handful, incl.
``ApiName.xsd`` and ``TransactionResult.xsd``). ``find_existing_xsd`` matches
namespaces by raw string, so the two spellings look like different namespaces
and schema reuse silently fails across the split.

The equivalence groups are PACK DATA (`schema_namespaces` — genericisation
sweep): which spellings denote the same authority is a fact about one
ecosystem's schema corpus, not about XML. Within each declared group, index 0
is the canonical (de-facto production) spelling; every member maps to it as
the grouping key. A pack that declares no groups gets raw-string matching,
which is correct for a corpus that never forked a spelling.

This module gives a deterministic CANONICAL KEY for grouping/matching only — it
is **not** a value to write back into a schema. Rewriting a live
``targetNamespace`` is a breaking change for wire consumers and must stay a
human decision; this module only makes the inconsistency VISIBLE and lets
reuse-matching see through it.
"""
from __future__ import annotations

from functools import lru_cache


def _normalize(ns: str | None) -> str:
    """Conservative, matching-safe normalization: trim whitespace + a single
    trailing slash. Deliberately does NOT touch scheme or host — host differences
    (``npci.org`` vs ``npci.org.in``) are genuine and handled by the explicit
    pack-declared group map, never by guessing."""
    return (ns or "").strip().rstrip("/")


@lru_cache(maxsize=4)
def _groups(pack_key: str) -> tuple[tuple[str, ...], ...]:
    _ = pack_key  # cache key only — invalidates when DOMAIN_PACK changes
    from app.core.domain.contract import schema_namespaces_of
    from app.core.domain.registry import get_active_pack

    return tuple(tuple(_normalize(m) for m in g)
                 for g in schema_namespaces_of(get_active_pack()))


def _canon_map() -> dict[str, str]:
    """member (normalized) -> canonical key (normalized index-0 of its group)."""
    from app.core.domain.registry import active_pack_key

    out: dict[str, str] = {}
    for group in _groups(active_pack_key()):
        key = group[0]
        for member in group:
            out[member] = key
    return out


def canonicalize_namespace(ns: str | None) -> str:
    """Return a stable grouping key for a ``targetNamespace``. Pack-declared
    spelling variants collapse to one key; everything else returns its
    normalized self. For MATCHING/grouping only — never write this value back
    into a schema."""
    return _canon_map().get(_normalize(ns), _normalize(ns))


def same_namespace(a: str | None, b: str | None) -> bool:
    """True when two ``targetNamespace`` strings denote the same authority,
    seeing through pack-declared spelling variants. Use for the guardian's
    same-namespace-ownership check so a variant spelling is not mistaken for a
    different (cross-namespace) owner."""
    return canonicalize_namespace(a) == canonicalize_namespace(b)


def sibling_namespace_spellings(ns: str | None) -> list[str]:
    """All known spelling variants of ``ns`` (including itself), normalized.
    For a namespace with no known variants, returns just the normalized input.
    Lets a namespace search match every spelling of the same authority."""
    from app.core.domain.registry import active_pack_key

    n = _normalize(ns)
    for group in _groups(active_pack_key()):
        members = set(group)
        if n in members:
            return sorted(members)
    return [n] if n else []


def namespace_variant_note(ns: str | None) -> str | None:
    """If ``ns`` is a known non-canonical spelling, return a short
    human-readable flag; else None. Surfaces the inconsistency in tool output
    and guardian findings rather than silently normalizing it away."""
    n = _normalize(ns)
    canon = _canon_map().get(n)
    if canon and canon != n:
        return (f"Known namespace spelling variant: {n!r} is treated as equivalent to "
                f"{canon!r} for reuse-matching, but both spellings coexist in the "
                f"schemas — flag for reconciliation, do NOT auto-rewrite (wire-breaking).")
    return None
