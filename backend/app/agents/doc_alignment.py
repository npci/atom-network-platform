# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""BRD→plan alignment (extension + divergence) detection for uploaded-doc reconciliation.

The other two reconciliation axes are one-directional: `doc_consistency` catches what a
doc INVENTS on the technical wire surface, `plan_coverage` catches which plan requirements
the doc DROPS. Neither sees the business/flow level of what the doc itself asserts — so an
uploaded human BRD that adds requirements the plan never discussed, or describes the SAME
feature with a DIFFERENT implementation story (mechanism, step order, responsible actor,
scope, value), reconciled as nothing but omissions.

This module is the missing direction. Extract → align, not "list differences":

1. ``extract_doc_commitments`` — pull the doc's implementable COMMITMENTS (must/shall-grade
   rules, flow steps with actors, limits, scope statements). Background prose, benefits and
   "no change" statements never enter, which is what keeps a rich human BRD from drowning
   the user in noise.
2. ``align_commitments`` — one structured pass judging each commitment against the ratified
   plan contract: consistent (dropped), CONFLICT (with the relation: mechanism / sequence /
   actor / scope / value / dependency / terminology), or BRD-ONLY (material extension).

Fail-open like the sibling axes: any LLM/parse failure returns [] — detection must never
block an upload. The caller (``upload_reconciler``) owns the conflict shape and dedups
against the technical axis.
"""
from __future__ import annotations

import logging

from app.core.llm import call_llm_structured
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE
from app.agents.plan_coverage import windows

logger = logging.getLogger(__name__)

_MAX_COMMITMENTS = 40          # bounds the alignment prompt on very rich docs
_MAX_FINDINGS = 20             # bounds the conflict list the user must resolve

_EXTRACT_SYSTEM = (
    "You are given a section of an network {doc_label}. Extract the IMPLEMENTABLE COMMITMENTS "
    "it asserts — statements that imply build work or constrain the implementation:\n"
    "  - rules/constraints (limits, expiries, caps, retry/attempt counts, eligibility conditions)\n"
    "  - flow steps WITH the responsible actor and their order (who does what, in what sequence)\n"
    "  - actor duties (what the PSP / the network app / issuer bank / the Authority must each do)\n"
    "  - scope statements (which transaction types / parties / phases are in or out)\n"
    "  - specific values (amounts, durations, counts, API/message names, error codes)\n"
    "Do NOT extract background, motivations, benefits, 'no change' statements, or generic "
    "restatements of how the network already works. One commitment per item, concise (<=25 words), "
    "keep exact values and actor names. "
    'Respond with ONLY JSON: {{"commitments":[{{"id":"c1","text":"...","category":'
    '"rule|flow_step|actor_duty|scope|value|wire"}}]}} — at most 20 items, the most material first.\n'
    + ANTI_INJECTION_CLAUSE
)

_ALIGN_SYSTEM = (
    "You are reconciling an uploaded network {doc_label} against the RATIFIED solution-design "
    "plan (the authoritative implementation contract). You get the PLAN CONTRACT and the list of "
    "COMMITMENTS extracted from the uploaded document. Judge each commitment against the plan:\n"
    "  - CONSISTENT — the plan covers it, or it is a business-level restatement compatible with "
    "the plan's implementation → OMIT it from the output entirely.\n"
    "  - CONFLICT — the plan handles the SAME concern a DIFFERENT way. Classify the relation:\n"
    "      value_conflict: same parameter, different number/name/code\n"
    "      mechanism_conflict: same feature, different technical approach (e.g. doc replaces PIN "
    "validation, plan keeps PIN and adds a second factor)\n"
    "      sequence_conflict: same steps, different order or trigger\n"
    "      actor_conflict: same step, different responsible party (PSP vs issuer vs the Authority vs app)\n"
    "      scope_conflict: doc includes transaction types/parties/phases the plan excludes, or vice versa\n"
    "      dependency_conflict: doc assumes/requires a capability the plan says does not exist (or "
    "treats as existing something the plan builds)\n"
    "      terminology_conflict: same name used for different things, or two names for the same thing, "
    "in a way that would mislead implementation\n"
    "  - BRD_ONLY — the plan is SILENT on it AND it is material (implies real build work or a real "
    "constraint). Purely narrative or trivially-implied items are NOT material → omit.\n"
    "Be THOROUGH — a missed divergence is far more costly than an extra question. Report every material "
    "divergence; when you are UNSURE whether the two sides are compatible, REPORT it (severity 'warning') "
    "rather than staying silent — the human resolves it in one click. Only genuinely narrative or "
    "detail-level-of-the-same-story differences are omitted. For every finding, 'detail' must state BOTH "
    "sides: 'doc says X; plan says/does Y' (for BRD_ONLY: 'plan does not cover this').\n"
    'Respond with ONLY JSON: {{"findings":[{{"commitment_id":"c1","relation":'
    '"value_conflict|mechanism_conflict|sequence_conflict|actor_conflict|scope_conflict|'
    'dependency_conflict|terminology_conflict|brd_only","item":"<short label>",'
    '"detail":"doc says X; plan says Y","severity":"blocker|warning"}}]}} '
    "— at most {max_findings}, conflicts before extensions, the most material first. "
    "An empty list is the expected result for a faithful document.\n"
    + ANTI_INJECTION_CLAUSE
)


def _doc_label(doc_kind: str) -> str:
    return "Tech Spec (TSD)" if doc_kind == "tech_spec" else "BRD"


async def extract_doc_commitments(doc_content: str, doc_kind: str = "brd") -> list[dict]:
    """Extract the doc's implementable commitments, scanning every window.
    Returns [{id, text, category}] (ids renumbered globally); [] on failure."""
    out: list[dict] = []
    seen: set[str] = set()
    # Cacheable system: identical bytes across all ≤8 window calls of one extraction run.
    from app.core.prompt_blocks import segments_for_anthropic_cache
    system = segments_for_anthropic_cache(
        [(_EXTRACT_SYSTEM.format(doc_label=_doc_label(doc_kind)), True)])
    for w in windows(doc_content):
        if len(out) >= _MAX_COMMITMENTS:
            break
        try:
            data = await call_llm_structured(
                system, wrap_untrusted(w, "DOCUMENT_SECTION"),
                schema={"type": "object",
                        "properties": {"commitments": {
                            "type": "array",
                            "items": {"type": "object",
                                      "properties": {"text": {"type": "string"},
                                                     "category": {"type": "string"}},
                                      "required": ["text"]}}},
                        "required": ["commitments"]},
                tool_name="record_commitments", agent_name="doc_alignment_extract", max_tokens=1500,
            )
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("doc_alignment: commitment extraction failed (%s)", e)
            continue
        items = data.get("commitments") if isinstance(data, dict) else None
        for c in (items or []):
            if not isinstance(c, dict):
                continue
            text = str(c.get("text") or "").strip()
            key = text.lower()[:80]
            if not text or key in seen:
                continue
            seen.add(key)
            out.append({"id": f"c{len(out) + 1}", "text": text[:220],
                        "category": str(c.get("category") or "rule")})
            if len(out) >= _MAX_COMMITMENTS:
                break
    return out


_RELATIONS = {"value_conflict", "mechanism_conflict", "sequence_conflict", "actor_conflict",
              "scope_conflict", "dependency_conflict", "terminology_conflict", "brd_only"}


async def align_commitments(plan_contract: str, commitments: list[dict],
                            doc_kind: str = "brd") -> list[dict]:
    """Judge each doc commitment against the plan contract. Returns raw findings
    ``[{commitment_id, relation, item, detail, severity, commitment_text}]`` — only
    conflicts and material BRD-only extensions (consistent items are omitted by the
    model). [] on empty inputs or any failure (fail-open)."""
    if not (plan_contract or "").strip() or not commitments:
        return []
    listing = "\n".join(f"- {c['id']} [{c.get('category', 'rule')}]: {c['text']}" for c in commitments)
    try:
        data = await call_llm_structured(
            _ALIGN_SYSTEM.format(doc_label=_doc_label(doc_kind), max_findings=_MAX_FINDINGS),
            f"PLAN CONTRACT (authoritative):\n{wrap_untrusted(plan_contract[:8000], 'PLAN_CONTRACT')}\n\n"
            f"DOCUMENT COMMITMENTS:\n{listing}\n\n"
            "Return the findings now.",
            schema={"type": "object",
                    "properties": {"findings": {
                        "type": "array", "maxItems": _MAX_FINDINGS,
                        "items": {"type": "object",
                                  "properties": {"commitment_id": {"type": "string"},
                                                 "relation": {"type": "string",
                                                              "enum": sorted(_RELATIONS)},
                                                 "item": {"type": "string"},
                                                 "detail": {"type": "string"},
                                                 "severity": {"type": "string",
                                                              "enum": ["blocker", "warning"]}},
                                  "required": ["commitment_id", "relation", "detail"]}}},
                    "required": ["findings"]},
            tool_name="record_findings", agent_name="doc_alignment_align", max_tokens=2000,
        )
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("doc_alignment: alignment failed (%s)", e)
        return []
    items = data.get("findings") if isinstance(data, dict) else None
    by_id = {c["id"]: c for c in commitments}
    out: list[dict] = []
    for f in (items or []):
        if not isinstance(f, dict):
            continue
        relation = str(f.get("relation") or "").strip().lower()
        detail = str(f.get("detail") or "").strip()
        if relation not in _RELATIONS or not detail:
            continue
        cid = str(f.get("commitment_id") or "")
        out.append({
            "commitment_id": cid,
            "commitment_text": (by_id.get(cid) or {}).get("text", ""),
            "relation": relation,
            "item": str(f.get("item") or "").strip()[:120],
            "detail": detail[:400],
            "severity": "blocker" if str(f.get("severity") or "").lower() == "blocker" else "warning",
        })
        if len(out) >= _MAX_FINDINGS:
            break
    # conflicts before extensions — the resolve list leads with the dangerous ones
    out.sort(key=lambda f: (f["relation"] == "brd_only", f["severity"] != "blocker"))
    return out
