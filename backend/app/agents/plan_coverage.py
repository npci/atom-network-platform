# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Plan-coverage (omission) detection for uploaded-doc reconciliation.

The shared plan auditor (`doc_consistency.check_doc_against_plan`) is biased
toward what a doc INVENTS — Phase-0 testing confirmed it misses what a doc
DROPS. Reconciliation needs the opposite axis: which of the plan's OWN
requirements are not covered by the uploaded doc.

We extract the plan's atomic requirements once (from the small ~7k plan_contract),
then scan the doc in overlapping windows and union the covered set — so a
requirement counts as omitted only when NO window covers it. Keying the check on
the (small) plan and windowing the (possibly huge) doc also sidesteps the shared
auditor's 24k truncation blind spot.

Fail-open: any LLM/parse failure yields "nothing omitted" — reconciliation must
never fabricate an omission from a detector glitch. In particular, if not a
single window could be checked, we return [] rather than flagging everything.
"""
from __future__ import annotations

import logging

from app.core.llm import call_llm_structured
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

# Doc windowing. Window < the shared auditor's 24k cap; small overlap so a
# requirement's evidence isn't split across a boundary. _MAX_WINDOWS bounds cost
# on very large uploads (we log when a doc exceeds it — no silent truncation).
_WINDOW = 20000
_OVERLAP = 1000
_MAX_WINDOWS = 8
_STEP = _WINDOW - _OVERLAP

_EXTRACT_SYSTEM = (
    "You are given a RATIFIED solution-design plan for an network change. Extract the atomic, "
    "checkable REQUIREMENTS that a downstream BRD MUST cover — the specific rules, values, "
    "messages, schema changes, endpoints, data-model changes and behaviours the plan mandates. "
    "One concise requirement per item. Only POSITIVE requirements the document must contain — "
    "NEVER the prohibitions (do not emit 'do not invent X'). Merge trivially-related points. "
    'Respond with ONLY JSON: {"requirements":[{"id":"r1","text":"..."}]} — at most 25 items.\n'
    + ANTI_INJECTION_CLAUSE
)

_COVERAGE_SYSTEM = (
    "You are checking whether a SECTION of a BRD addresses a list of plan requirements. For each "
    "requirement, decide if this section mentions or satisfies it — even loosely or partially. "
    "Be generous: partial coverage counts as covered. Return ONLY the ids that ARE addressed in "
    'this section.\nRespond with ONLY JSON: {"covered":["r1","r3"]} — ids not addressed are simply '
    "omitted from the list. Never invent an id that was not in the requirements.\n"
    + ANTI_INJECTION_CLAUSE
)


def windows(text: str) -> list[str]:
    """Split a document into overlapping windows for whole-doc scanning. Returns
    [] for empty text and a single window for text under the window size."""
    text = text or ""
    if not text.strip():
        return []
    if len(text) <= _WINDOW:
        return [text]
    out: list[str] = []
    i = 0
    while i < len(text) and len(out) < _MAX_WINDOWS:
        out.append(text[i:i + _WINDOW])
        i += _STEP
    if i < len(text):
        logger.warning(
            "plan_coverage: doc %d chars exceeds %d windows — coverage limited to first ~%d chars",
            len(text), _MAX_WINDOWS, _MAX_WINDOWS * _STEP,
        )
    return out


async def extract_plan_requirements(plan_contract: str) -> list[dict]:
    """Extract the plan's atomic requirements from the (small) plan contract.
    Returns [{id, text}]; [] on empty plan or any failure (fail-open)."""
    if not (plan_contract or "").strip():
        return []
    try:
        data = await call_llm_structured(
            _EXTRACT_SYSTEM, wrap_untrusted(plan_contract[:8000], "PLAN_CONTRACT"),
            schema={"type": "object",
                    "properties": {"requirements": {
                        "type": "array", "maxItems": 25,
                        "items": {"type": "object",
                                  "properties": {"id": {"type": "string"},
                                                 "text": {"type": "string"}},
                                  "required": ["id", "text"]}}},
                    "required": ["requirements"]},
            tool_name="record_requirements", agent_name="plan_coverage_extract", max_tokens=1500,
        )
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("plan_coverage: requirement extraction failed (%s)", e)
        return []
    reqs = data.get("requirements") if isinstance(data, dict) else None
    out: list[dict] = []
    for i, r in enumerate(reqs or []):
        if not isinstance(r, dict):
            continue
        text = str(r.get("text") or "").strip()
        if text:
            out.append({"id": str(r.get("id") or f"r{i + 1}"), "text": text[:200]})
        if len(out) >= 25:
            break
    return out


async def find_uncovered_requirements(requirements: list[dict], doc_content: str) -> list[dict]:
    """Return the requirements NOT covered anywhere in the doc. Scans every window
    and unions the covered ids. Fail-open: if not one window could be checked,
    returns [] (never flags everything as omitted on a detector outage)."""
    if not requirements:
        return []
    wins = windows(doc_content)
    if not wins:
        return []  # empty doc — caller already guards content; never fabricate omissions
    req_list = "\n".join(f"- {r['id']}: {r['text']}" for r in requirements)
    # Prompt-cache split: the rules + requirement list are byte-identical across all ≤8
    # window calls — cacheable system segments; only the doc window varies per call.
    from app.core.prompt_blocks import segments_for_anthropic_cache
    system = segments_for_anthropic_cache([
        (_COVERAGE_SYSTEM, True),
        (f"PLAN REQUIREMENTS:\n{req_list}", True),
    ])
    covered: set[str] = set()
    checked = 0
    valid_ids = [str(r["id"]) for r in requirements]
    for w in wins:
        try:
            data = await call_llm_structured(
                system,
                f"BRD SECTION:\n{wrap_untrusted(w, 'BRD_SECTION')}\n\n"
                "Which requirement ids are addressed in this section?",
                schema={"type": "object",
                        "properties": {"covered": {"type": "array",
                                                   "items": {"type": "string", "enum": valid_ids}}},
                        "required": ["covered"]},
                tool_name="record_covered", agent_name="plan_coverage_check", max_tokens=800,
            )
        except Exception as e:  # noqa: BLE001 — one window failing must not fabricate omissions
            logger.warning("plan_coverage: coverage window failed (%s)", e)
            continue
        checked += 1
        ids = data.get("covered") if isinstance(data, dict) else None
        for cid in (ids or []):
            covered.add(str(cid).strip().casefold())
    if checked == 0:
        logger.warning("plan_coverage: no window could be checked — skipping omission detection")
        return []
    # casefold+strip both sides — an LLM id-echo like "R1" vs "r1" / " r1" is drift, not
    # an omission, and must not be reported as a false dropped-requirement conflict.
    return [r for r in requirements if str(r["id"]).strip().casefold() not in covered]
