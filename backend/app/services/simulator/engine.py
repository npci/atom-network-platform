# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""What THIS simulator engine can honour — version and capability set.

Advertised at `GET /sim/packs/capabilities` and enforced at publish time:
`POST …/publish` rejects a pack whose `engine_min` or `requires` this engine
cannot honour, with the missing capability NAMED — a machine-readable feature
request, raised while a human is watching rather than mid-certification.

The set lists only what is IMPLEMENTED. Adding a string here without the
mechanism behind it converts the publish-time gate into a lie.
"""
from __future__ import annotations

ENGINE_VERSION = "1.0"

CAPABILITIES = frozenset({
    # cert_assertions' six rule kinds, applied on ingress (SIM-3).
    "validation.occurrence",
    "validation.datatype",
    "validation.length",
    "validation.mandatory",
    "validation.enum",
    "validation.pattern",
    # Scenario engine (SIM-4).
    "scenario.delay",
    "scenario.no_response",
    # Wire flavours with a registered codec.
    "wire.xml",
})


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except ValueError:
        return (0,)


def engine_shortfalls(*, engine_min: str, requires: list[str]) -> list[str]:
    """Everything this engine cannot honour about a pack, each entry a
    human-readable refusal. Empty list = publishable."""
    out: list[str] = []
    if _version_tuple(engine_min) > _version_tuple(ENGINE_VERSION):
        out.append(f"pack needs engine >= {engine_min}, this engine is "
                   f"{ENGINE_VERSION}")
    missing = sorted(set(requires or []) - CAPABILITIES)
    if missing:
        out.append("missing capability: " + ", ".join(missing))
    return out
