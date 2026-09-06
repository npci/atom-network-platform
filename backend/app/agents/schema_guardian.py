# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic XSD reuse-vs-create guardian (THE BOOK §7.4).

The XSD-Discovery subagent DECIDES reuse/extend/new agentically; this module is
the deterministic CHECK on that decision. It deliberately is NOT a full auto-gate:
schema equivalence is provably undecidable in general and design intent isn't in
the bytes, so it classifies only what IS checkable — does an existing schema in
this namespace already define this element, identically or in conflict? — and
ESCALATES the residual to a human instead of deciding (research: every
"fully-automatic XSD gate" formulation was refuted; analyzer + escalation is the
defensible shape).

Addresses the two observed failure modes head-on:
  - creating a NEW schema/element when an existing one already defines it
    identically  → flagged ``redundant`` → recommend reuse
  - reusing/forking an element that CONFLICTS with an existing definition
    → flagged ``conflict`` → escalate; never fork a shared type silently

Pure + deterministic (no DB, no I/O) so it unit-tests in isolation; the thin
working-tree/index wrapper is the ``schema_guardian`` tool in agentic_tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.xsd_graph_builder import element_index, parse_schema
from app.agents.xsd_namespace import namespace_variant_note, same_namespace


@dataclass
class ReuseFinding:
    element: str        # element-index key, e.g. "element:ReqTransfer" / "complexType:Money"
    status: str         # "redundant" | "conflict" | "novel"
    detail: str


@dataclass
class GuardianVerdict:
    decision: str                       # "reuse" | "extend" | "new" | "escalate"
    escalate: bool
    target_namespace: str | None
    findings: list[ReuseFinding] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"[schema_guardian — deterministic check; decision={self.decision.upper()}" \
               + (" — ESCALATE to human" if self.escalate else "") + "]"
        lines = [head, f"target_namespace = {self.target_namespace!r}"]
        for r in self.reasons:
            lines.append(f"• {r}")
        if self.findings:
            lines.append("elements:")
            for f in self.findings[:40]:
                lines.append(f"  [{f.status}] {f.element} — {f.detail}")
        return "\n".join(lines)


def analyze_reuse(proposed_content: str, siblings: list[tuple[str, str]]) -> GuardianVerdict:
    """Classify a proposed schema against existing sibling schemas in the same
    namespace family. ``siblings`` is ``[(path, content), ...]`` — the schemas the
    caller resolved as same-namespace neighbours. Pure: deterministic over the
    element indices, no decision about anything ambiguous (that escalates)."""
    proposed = element_index(proposed_content)
    tns = parse_schema(proposed_content).target_namespace
    verdict = GuardianVerdict(decision="new", escalate=False, target_namespace=tns)

    note = namespace_variant_note(tns)
    if note:
        verdict.reasons.append(note)

    # element-key -> [(sibling_path, signature, sibling_namespace)]
    sib_index: dict[str, list[tuple[str, str, str | None]]] = {}
    for path, content in siblings:
        sib_ns = parse_schema(content).target_namespace
        for key, sig in element_index(content).items():
            sib_index.setdefault(key, []).append((path, sig, sib_ns))

    redundant = conflict = novel = 0
    cross_ns = False
    for key, sig in proposed.items():
        matches = sib_index.get(key, [])
        if not matches:
            verdict.findings.append(ReuseFinding(key, "novel", "no existing definition in this namespace"))
            novel += 1
            continue
        identical = [p for p, s, _ns in matches if s == sig]
        differing = [(p, ns) for p, s, ns in matches if s != sig]
        # an element of the SAME name living under a DIFFERENT namespace is an
        # identity-boundary collision — escalate regardless of signature.
        if any(not same_namespace(tns, ns) for _p, _s, ns in matches):
            cross_ns = True
        if identical:
            verdict.findings.append(ReuseFinding(
                key, "redundant", f"already defined identically in {identical[0]} — reuse it, do not redefine"))
            redundant += 1
        else:
            verdict.findings.append(ReuseFinding(
                key, "conflict", f"defined DIFFERENTLY in {differing[0][0]} — reusing would fork a shared type"))
            conflict += 1

    # Decision — escalate the residual; never auto-approve a breaking/ambiguous case.
    if conflict or cross_ns:
        verdict.decision = "escalate"
        verdict.escalate = True
        if conflict:
            verdict.reasons.append(f"{conflict} element(s) conflict with an existing definition — do not "
                                   "fork a shared type; needs human/bank sign-off")
        if cross_ns:
            verdict.reasons.append("an element name collides across DIFFERENT namespaces — namespace "
                                   "identity boundary, escalate")
    elif redundant and not novel:
        verdict.decision = "reuse"
        verdict.reasons.append("every proposed element already exists identically — reuse the existing "
                               "schema instead of creating a new one")
    elif redundant and novel:
        verdict.decision = "extend"
        verdict.reasons.append(f"{redundant} element(s) already exist (reuse those) and {novel} are new — "
                               "add the new ones to the existing schema rather than a separate file")
    else:
        verdict.decision = "new"
        verdict.reasons.append("no overlap with existing schemas in this namespace — a new schema is justified")

    return verdict
