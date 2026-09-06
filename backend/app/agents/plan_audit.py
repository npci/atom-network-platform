# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Plan enforcement audit — challenge the PLAN itself, conservatively.

Every other gate checks an artifact AGAINST the plan. This is the one gate that audits the PLAN, at
proposal time, before the human ratifies it. It catches the class of defect where a requirement's
enforcement is ASSUMED at a layer that isn't actually wired — the fa4631e3 case: the plan asserted
"out-of-enum values are rejected at the schema/JAXB layer ... no extra Java-side enum check is added"
when no schema is wired into the unmarshaller, so the requirement's core "reject invalid values" is
not actually enforced anywhere the code can be shown to do it.

For each HARD enforcement claim (validate / reject / enforce / persist) the audit asks: is the named
enforcement mechanism backed by VERIFIED evidence, or merely ASSUMED? An enforcement resting on an
unverified assumption for a hard requirement is flagged — not as "the plan is wrong", but as "verify
this enforcement point, or add an explicit, tested check".

INTELLIGENT + CONSERVATIVE (per the product ask): a plan whose enforcement claims are evidence-backed
returns ``sound=True``, empty findings, and the plan is left BYTE-IDENTICAL — no busywork edits. The
audit NEVER rewrites the plan; on a real gap it only APPENDS an advisory note
(``technical_analysis["enforcement_audit"]`` + a risks line) for the human ratifier to act on.

Family-A single-call agent (text-in → JSON-out via call_llm), fail-open: any LLM/parse failure →
``sound=True``, no findings (an audit fault never blocks or mutates a plan).
"""
from __future__ import annotations

import logging

from app.core.llm import call_llm
from app.core.json_recovery import parse_llm_json
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 1500

_SYSTEM = (
    "You audit a ratified-candidate change PLAN for the network switch for ENFORCEMENT SOUNDNESS — and "
    "nothing else. For every HARD requirement in the plan that says the system will VALIDATE, REJECT, "
    "ENFORCE, or PERSIST something, identify WHERE the plan says that is enforced and judge whether the "
    "enforcement is BACKED BY VERIFIED EVIDENCE or merely ASSUMED.\n"
    "Flag a requirement ONLY when its enforcement rests on an UNVERIFIED ASSUMPTION about a mechanism "
    "that may not actually be wired — e.g. 'invalid values are rejected at the JAXB/schema layer' when "
    "nothing shows a schema is validated on unmarshal and the plan explicitly adds 'no Java-side check', "
    "or 'X is persisted' with no datastore / column / log named. The fa4631e3 archetype: a 'reject "
    "invalid enum' requirement delegated ENTIRELY to JAXB with 'no explicit Java check', where schema "
    "validation is assumed, not shown to be wired.\n"
    "Be CONSERVATIVE. If a requirement's enforcement is concrete and evidence-backed (a named validator "
    "check, a cited file, an explicit rule the plan will add), DO NOT flag it. Do NOT flag style, "
    "wording, completeness, design taste, or anything that is not an enforcement-soundness risk. A sound "
    "plan returns an EMPTY list — that is the expected, good case. Never propose rewriting the plan; you "
    "only surface enforcement risks for a human to confirm.\n\n"
    "Respond with ONLY a JSON object:\n"
    "{\n"
    '  "sound": true|false,\n'
    '  "findings": [{"requirement": "<the hard validate/reject/enforce/persist requirement>", '
    '"enforcement_point": "<where the plan says it is enforced>", "verified": false, '
    '"severity": "warning|blocker", '
    '"detail": "<one sentence: the enforcement is assumed not shown; what to verify or add>"}]\n'
    "}\n"
    + ANTI_INJECTION_CLAUSE
)


def _render_plan_for_audit(fp: dict, ta: dict) -> str:
    """The enforcement-relevant slice of the plan — especially ASSUMPTIONS, where unverified
    enforcement claims hide (the JAXB-enum assumption lived in functional_plan['assumptions'])."""
    parts: list[str] = []
    for key, label in (("overview", "OVERVIEW"), ("steps", "STEPS"),
                       ("assumptions", "ASSUMPTIONS"), ("compatibility", "COMPATIBILITY")):
        if fp.get(key):
            parts.append(f"{label}: {fp[key]}")
    # The file list moves between five key names; reading only `files_to_change` dropped it
    # from the audit prompt for four of them, and a plan whose functional half is also thin
    # then audits as sound with zero findings.
    from app.agents.plan_files import plan_file_entries
    _files = plan_file_entries(ta)
    if _files:
        parts.append("FILES_TO_CHANGE: " + str([{"path": p, "intent": e.get("intent")} for p, e in _files]))
    for key, label in (("data_model_changes", "DATA_MODEL_CHANGES"),
                       ("reuse_findings", "REUSE_FINDINGS"), ("constraints", "CONSTRAINTS"),
                       ("risks", "RISKS")):
        if ta.get(key):
            parts.append(f"{label}: {ta[key]}")
    return "\n".join(str(p) for p in parts)[:12000]


def _empty() -> dict:
    return {"sound": True, "findings": []}


async def audit_plan_enforcement(*, functional_plan: dict | None, technical_analysis: dict | None) -> dict:
    """Audit the plan's hard enforcement claims. Returns ``{sound, findings:[{requirement,
    enforcement_point, verified, severity, detail}]}``. Conservative + fail-open: an evidence-backed
    plan (or any LLM/parse failure) → ``sound=True``, empty findings."""
    plan_text = _render_plan_for_audit(functional_plan or {}, technical_analysis or {})
    if not plan_text.strip():
        return _empty()
    user = (f"PLAN TO AUDIT:\n{wrap_untrusted(plan_text, 'PLAN')}\n\n"
            "List only enforcement claims that rest on an unverified assumption. Empty list if every "
            "validate/reject/enforce/persist claim is evidence-backed.")
    try:
        raw = await call_llm(system=_SYSTEM, messages=[{"role": "user", "content": user}],
                             max_tokens=MAX_OUTPUT_TOKENS, agent_name="plan_audit")
    except Exception as e:  # noqa: BLE001 — fail-open: never block/mutate a plan on the auditor
        logger.warning("plan_audit LLM call failed (%s) — treating plan as sound", e)
        return _empty()

    data = await parse_llm_json(raw, fallback=None)
    if not isinstance(data, dict):
        return _empty()
    findings = []
    for f in (data.get("findings") or []):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "warning").lower()
        if sev not in ("blocker", "warning"):
            sev = "warning"
        findings.append({"requirement": str(f.get("requirement") or "")[:200],
                         "enforcement_point": str(f.get("enforcement_point") or "")[:200],
                         "verified": bool(f.get("verified", False)),
                         "severity": sev,
                         "detail": str(f.get("detail") or "")[:300]})
    findings = findings[:15]
    return {"sound": not findings, "findings": findings}


def annotate_plan(ca, findings: list[dict]) -> bool:
    """ADDITIVE, non-destructive: record the audit on ``technical_analysis['enforcement_audit']`` and
    append a risks line per finding so the human ratifier sees it. Never touches ``functional_plan`` /
    ``flow_spec``. Reassigns ``technical_analysis`` so the ORM JSON column detects the change. Returns
    True when it annotated. Caller is responsible for ``db.add(ca)`` + commit."""
    if not findings:
        return False
    ta = dict(getattr(ca, "technical_analysis", None) or {})
    ta["enforcement_audit"] = findings
    risks = list(ta.get("risks") or [])
    for f in findings:
        risks.append(f"ENFORCEMENT AUDIT ({f.get('severity')}): {f.get('requirement')} — {f.get('detail')}")
    ta["risks"] = risks
    ca.technical_analysis = ta
    return True
