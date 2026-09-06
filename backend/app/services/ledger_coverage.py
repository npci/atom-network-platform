# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Ledger-coverage check (accuracy S8).

A deterministic backstop that flags binding Decision-Ledger decisions a generated
document IGNORED — i.e. a ratified decision whose terms appear nowhere in the doc.
The decisions are already injected into every generator, so this is a safety net
that turns a silently-dropped decision into a surfaced WARN finding.

Deterministic + fail-open: no LLM, never raises, returns finding strings ('' set
== fully covered). Heuristic (term presence), so it under-reports rather than
false-positives.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "onto", "existing",
    "should", "must", "will", "shall", "use", "used", "using", "via", "per", "not",
    "new", "add", "added", "field", "code", "value", "option", "instead", "rather",
}
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")


def ledger_coverage_findings(db: Session, change_request_id: str, artifact_text: str) -> list[str]:
    """Active ledger decisions whose distinctive terms appear NOWHERE in the artifact."""
    if not artifact_text:
        return []
    from app.services.decision_ledger import active_entries

    low = artifact_text.lower()
    findings: list[str] = []
    for e in active_entries(db, change_request_id):
        chosen = (e.chosen or "").strip()
        if not chosen:
            continue
        terms = [w for w in _WORD.findall(chosen) if w.lower() not in _STOP]
        if not terms:
            continue
        if not any(t.lower() in low for t in terms):
            label = (e.question or e.question_key or "decision").strip()
            findings.append(f"Ratified decision not reflected — '{label}': chose «{chosen[:80]}»")
    return findings
