# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Revision planner — turn a round's resolved outcomes into a per-doc kit plan.

After a negotiation round closes, the consolidated outcomes (decided clusters +
doc-impact decisions) are messy. This agent produces a clean, editable plan: for
each Product Kit document that genuinely needs updating, a concrete change
instruction the regeneration step can act on, plus a one-paragraph overview.

Pure function; the caller persists the result on a KitRevisionPlan row.
"""
import json
import logging
import re

from app.core.llm import call_llm
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

# Kit doc types the planner may target (kept in sync with ProductKitDocType,
# minus change_summary which is generated separately).
# `product_doc` retired — superseded by `product_note` (see RETIRED_DOC_TYPES in
# app.models.product_kit). Keep in sync with the identical tuple in doc_impact.py.
KIT_DOC_TYPES = (
    "product_deck", "promo_video", "explainer_video",
    "faq", "cert_test_cases", "circular", "manifest",
    "prototype_screens", "product_note",
)

_SYSTEM = """You are an authority Product Manager's assistant preparing the next version of a network feature Product Kit.

A negotiation round just closed. You are given the resolved outcomes for that
round — decisions on partner counter-proposals and which documents partner
queries implied a change to. Turn them into a concrete, editable PLAN for the
next kit version.

For EACH kit document that genuinely needs updating, write one plan item with a
specific change instruction (what to change and why) that a generator can act
on. Do NOT include documents that don't need changes. Be concrete and grounded
in the outcomes — do not invent changes the outcomes don't support.

Choose doc_type ONLY from this exact list:
  product_doc, product_deck, promo_video, explainer_video, faq,
  cert_test_cases, circular, manifest, prototype_screens, product_note

Respond with exactly one JSON object — nothing else:
{
  "summary": "<one-paragraph plain-prose overview of what changes from the current version to the next, for the PM and for partners>",
  "items": [
    {
      "doc_type": "faq",
      "change_instruction": "<concrete instruction: what to add/change in this doc and why>",
      "rationale": "<which outcome(s) drive this>"
    }
  ]
}

If no document needs changing, return an empty items list with a summary saying so.
STRICT JSON. No markdown fences, no preamble.

""" + ANTI_INJECTION_CLAUSE


def _parse(raw: str) -> dict | None:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _fallback_plan(outcomes: list[dict]) -> dict:
    """Build a plan directly from the round outcomes, no LLM.

    Used when the planner LLM is unavailable/unparseable: rather than masquerade
    as "no changes needed" (and silently drop doc-impact findings), we surface
    every document the round's doc-impact already flagged. Only `doc_update`
    outcomes carry explicit `documents`; cluster outcomes don't map to docs
    without the LLM, so they can't seed fallback items. Items are merged per
    doc_type so a doc flagged by two queries appears once.
    """
    by_doc: dict[str, list[str]] = {}
    for o in outcomes or []:
        if o.get("decision") != "doc_update":
            continue
        reason = (o.get("rationale") or o.get("topic") or "").strip()
        for dt in (o.get("documents") or []):
            if dt not in KIT_DOC_TYPES:
                continue
            if reason:
                by_doc.setdefault(dt, []).append(reason)
            else:
                by_doc.setdefault(dt, [])
    items = []
    for dt, reasons in by_doc.items():
        merged = "; ".join(dict.fromkeys(reasons))  # dedupe, preserve order
        items.append({
            "doc_type": dt,
            "change_instruction": (
                f"Update the {dt} to reflect this round's resolved outcomes"
                + (f": {merged}" if merged else ".")
            ),
            "rationale": merged,
            "include": True,
        })
    summary = (
        "Auto-generated from the round outcomes because the revision-planner was "
        "unavailable. The flagged documents are correct; review and refine the "
        "change instructions (or re-draft) before generating."
    ) if items else ""
    return {"summary": summary, "items": items}


def _outcomes_block(outcomes: list[dict]) -> str:
    lines = []
    for o in outcomes or []:
        docs = ", ".join(o.get("documents") or []) or "—"
        lines.append(
            f"- topic: {o.get('topic', '')}; decision: {o.get('decision', '')}; "
            f"docs flagged: {docs}; rationale: {o.get('rationale', '')}"
        )
    return "\n".join(lines) if lines else "(no resolved outcomes this round)"


async def plan_revision(
    *,
    outcomes: list[dict],
    change_title: str = "",
    current_version: int = 1,
) -> dict:
    """Return {"summary": str, "items": [...], "ok": bool}.

    `ok` is False when the planner LLM failed or returned unparseable output —
    the caller should mark the plan needs_retry rather than treat it as a clean
    result. On that path we still return a deterministic fallback plan built from
    the outcomes (so doc-impact findings aren't silently dropped); on a genuine
    empty result from a healthy LLM, `ok` is True with an empty items list.
    Items are filtered to known kit doc types and tagged include=True."""
    user = "\n".join([
        f"Change: {wrap_untrusted(change_title or '(untitled)', 'CHANGE_TITLE')}",
        f"Current published version: v{current_version}  →  preparing: v{current_version + 1}",
        "",
        "Resolved outcomes from the round just closed:",
        wrap_untrusted(_outcomes_block(outcomes), "ROUND_OUTCOMES"),
        "",
        "Produce the JSON plan per the instructions.",
    ])
    try:
        raw = await call_llm(
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            max_tokens=2000,
            agent_name="revision_planner",
        )
        obj = _parse(raw)
    except Exception as exc:
        logger.warning("revision_planner failed: %s", exc)
        obj = None

    if not obj:
        # LLM unavailable / unparseable — fall back to the outcomes themselves so
        # the flagged docs still surface, and signal not-ok so the caller marks
        # the plan needs_retry instead of "no changes needed".
        return {**_fallback_plan(outcomes), "ok": False}

    items = []
    for it in (obj.get("items") or []):
        dt = it.get("doc_type")
        if dt not in KIT_DOC_TYPES:
            continue
        items.append({
            "doc_type": dt,
            "change_instruction": str(it.get("change_instruction") or "").strip(),
            "rationale": str(it.get("rationale") or "").strip(),
            "include": True,
        })
    return {"summary": str(obj.get("summary") or "").strip(), "items": items, "ok": True}
