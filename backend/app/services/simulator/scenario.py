# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Scenario selection — `when → respond`, most specific identity first (SIM-4).

Identity tiers, not list order alone: a request that names a VARIANT must hit
that variant's declared scenario even when a broader field-predicate scenario
sits earlier in the merged list (Gate 2's variant-binding claim: "a pack with
multiple variants of one API resolves every variant to its declared
scenario"). Within a tier, merged list order decides — leaf pack first, so a
child pack overrides its base for the same identity.

No match → the caller uses the engine default. That default is `rc="00"`,
stated on the response (`X-Sim-Scenario: default`) — never silently
indistinguishable from a matched scenario.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["choose"]


def choose(scenarios: Sequence[Mapping[str, Any]], *,
           variant_id: str | None = None,
           tc_id: str | None = None,
           doc: Any = None,
           codec=None) -> Mapping[str, Any] | None:
    """The matching scenario, or None. Tiers: variant_id > tc_id > field
    predicate; first hit within a tier wins."""
    if variant_id:
        for s in scenarios:
            if s.get("when", {}).get("variant_id") == variant_id:
                return s
    if tc_id:
        for s in scenarios:
            if s.get("when", {}).get("tc_id") == tc_id:
                return s
    if doc is not None and codec is not None:
        for s in scenarios:
            when = s.get("when", {})
            field = when.get("field")
            if field and when.get("eq") in codec.values(doc, field):
                return s
    return None
