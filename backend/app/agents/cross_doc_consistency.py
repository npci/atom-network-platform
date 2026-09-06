# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cross-document BRD↔TSD consistency (pending #3).

The doc↔plan gate (`doc_consistency`) checks each document against the ratified
plan, but nothing checks the TSD against the BRD it derives from. This module
extracts shared ANCHORS from both documents and reports where they diverge.

It is deterministic (regex, no LLM), so it is cheap and cannot hallucinate. The
first, cleanest anchor is the functional-requirement id (`FR-<n>`): the BRD
DEFINES them and the TSD should IMPLEMENT them, so a directional mismatch is a
real signal, not noise:

  • an FR the TSD references but the BRD never defines  → the TSD invented a
    requirement (kind 'fr_undefined');
  • an FR the BRD defines but the TSD never references  → the TSD dropped a
    requirement (kind 'fr_uncovered').

Findings use the same {severity, kind, item, detail} shape as `doc_consistency`,
so the surgical editor can repair them by focus item. Advisory by default
(warnings, never a hard block) — deeper wire-message / decision contradictions
still need the LLM doc↔plan pass; this is the cheap deterministic first line.
"""
from __future__ import annotations

import re

_FR_RE = re.compile(r"\bFR-(\d+)\b", re.IGNORECASE)


def _frs(text: str) -> set[str]:
    # Normalise the numeric part so zero-padding differences don't read as a
    # mismatch: the BRD blueprint emits "FR-01" while the TSD may emit "FR-1".
    # Compare on the integer value (canonicalised to "FR-<n>", no leading zeros).
    return {f"FR-{int(n)}" for n in _FR_RE.findall(text or "")}


def check_cross_doc(brd_content: str, tsd_content: str) -> dict:
    """Compare a TSD against its BRD by functional-requirement id. Returns
    ``{consistent, findings:[{severity,kind,item,detail}], has_blocker}``.
    Fail-open: empty input → consistent (nothing to reconcile)."""
    if not (brd_content or "").strip() or not (tsd_content or "").strip():
        return {"consistent": True, "findings": [], "has_blocker": False}

    brd_fr, tsd_fr = _frs(brd_content), _frs(tsd_content)
    findings: list[dict] = []
    for fr in sorted(tsd_fr - brd_fr):
        findings.append({"severity": "warning", "kind": "fr_undefined", "item": fr,
                         "detail": f"TSD references {fr} which the BRD does not define."})
    for fr in sorted(brd_fr - tsd_fr):
        findings.append({"severity": "warning", "kind": "fr_uncovered", "item": fr,
                         "detail": f"BRD defines {fr} but the TSD does not reference it."})
    findings = findings[:30]
    return {"consistent": not findings, "findings": findings,
            "has_blocker": any(f["severity"] == "blocker" for f in findings)}


def cross_doc_items(result: dict) -> list[str]:
    """Item names of the cross-doc findings — feed to the surgical editor's
    focus_items so a repair targets exactly the divergent FR references."""
    return [f.get("item") for f in (result.get("findings") or []) if f.get("item")]
