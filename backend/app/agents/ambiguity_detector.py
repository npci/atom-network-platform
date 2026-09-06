# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Gap / ambiguity detection for feature specs.

Given the extracted technical proposals and the taxonomy's required_fields,
find what's missing or under-specified. Returns a list of gap keys with a
flag for whether each gap is *critical* (impacts API/flow design) or *safe
to default* (PM unlikely to change the answer).

Pattern adapted from `Downloads/RAG_SYSTEM-main/reasoning/ambiguity_detector.py`;
domain wording is sourced from the active domain pack (see docs/genericization).
"""
import logging

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.domain.registry import prompt_block
from app.core.llm import call_llm
from app.core.llm_router import pick_model_for_agent
from app.core.json_recovery import parse_llm_json

logger = logging.getLogger(__name__)


_SYSTEM = f"""You are a specification-gap detector for {prompt_block("platform_name", "this change-management platform")}.

You will be given:
- A feature description
- The taxonomy bucket's REQUIRED_FIELDS (fields any spec for this bucket must cover)
- A structured PROPOSALS JSON extracted from past {prompt_block("authority", "platform")} documents

Your job: identify SPECIFICATION GAPS the Product Manager must resolve.
A "gap" is a field or decision that is either:
  (a) missing from proposals entirely, or
  (b) present but ambiguous / contradictory / TBD-ish.

For each gap, classify it:
  critical=true   if the answer changes API names, field definitions, error codes,
                  flow steps, or state-transition mechanics
  critical=false  if the answer is cosmetic, stylistic, or has a very safe default

Do NOT fabricate gaps for fields that are already specified clearly in the proposals.
Do NOT flag naming/wording quibbles as gaps.

Respond with ONLY this JSON (no markdown fences, no commentary):
{{
  "gaps": [
    {{
      "key": "snake_case_identifier",
      "description": "one short sentence on what is unknown / underspecified",
      "critical": true
    }}
  ]
}}

Aim for 3-10 gaps. If the spec is fully specified, return {{"gaps": []}}.

""" + ANTI_INJECTION_CLAUSE


async def detect(
    feature_description: str,
    proposals: dict | None,
    required_fields: list[str],
    taxonomy_primary: str | None = None,
) -> list[dict]:
    """Detect specification gaps. Returns list of {key, description, critical}."""
    if not feature_description:
        return []

    # Build a compact proposals summary for the prompt
    proposals = proposals or {}
    summary_lines: list[str] = []
    if proposals.get("apis"):
        names = [a.get("name") for a in proposals["apis"] if a.get("name")]
        summary_lines.append(f"APIs: {', '.join(names)}")
    if proposals.get("error_codes"):
        codes = [e.get("code") for e in proposals["error_codes"] if e.get("code")]
        summary_lines.append(f"Error codes: {', '.join(codes)}")
    if proposals.get("auth_method"):
        summary_lines.append(f"Auth method: {proposals['auth_method']}")
    if proposals.get("transaction_limit"):
        summary_lines.append(f"Transaction limit: {proposals['transaction_limit']}")
    if proposals.get("flow_sequence"):
        summary_lines.append(f"Flow steps: {len(proposals['flow_sequence'])}")
    if proposals.get("functional_requirements"):
        summary_lines.append(f"FRs: {len(proposals['functional_requirements'])}")
    proposals_summary = "\n".join(summary_lines) if summary_lines else "(no proposals available)"

    user_content = (
        f"# Feature description\n{wrap_untrusted(feature_description[:3000], 'FEATURE_DESCRIPTION')}\n\n"
        f"# Taxonomy primary\n{taxonomy_primary or 'unknown'}\n\n"
        f"# REQUIRED_FIELDS for this bucket (must be resolved)\n"
        f"{', '.join(required_fields) if required_fields else '(none)'}\n\n"
        f"# PROPOSALS (summary)\n{wrap_untrusted(proposals_summary, 'PROPOSALS_SUMMARY')}\n\n"
        f"# Full proposals JSON (for reference)\n{_short_json(proposals)}"
    )

    # Slice 27a — ambiguity_detector is Purpose.UTILITY (gap detection over
    # already-extracted proposals; no novel reasoning required).
    raw = await call_llm(
        system=_SYSTEM, messages=[{"role": "user", "content": user_content}],
        max_tokens=1500, model=pick_model_for_agent("ambiguity_detector"),
        agent_name="ambiguity_detector",
    )
    parsed = await parse_llm_json(raw, fallback={"gaps": []})

    gaps = parsed.get("gaps") if isinstance(parsed, dict) else None
    if not isinstance(gaps, list):
        gaps = []

    # Normalise entries
    cleaned: list[dict] = []
    seen_keys: set[str] = set()
    for g in gaps:
        if not isinstance(g, dict):
            continue
        key = str(g.get("key", "")).strip().lower().replace(" ", "_")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        cleaned.append({
            "key":         key,
            "description": str(g.get("description", "")).strip(),
            "critical":    bool(g.get("critical", False)),
        })

    # Always include any REQUIRED_FIELDS that the proposals are missing entirely —
    # this gives the detector a deterministic floor even if the LLM missed them.
    already_keys = seen_keys
    for rf in required_fields or []:
        rfk = rf.strip().lower().replace(" ", "_")
        if rfk and rfk not in already_keys and not _field_present_in_proposals(rf, proposals):
            cleaned.append({
                "key":         rfk,
                "description": f"Required field '{rf}' for this taxonomy bucket is not specified.",
                "critical":    True,
            })
            already_keys.add(rfk)

    logger.info("ambiguity_detector: %d gap(s) detected (%d critical)",
                len(cleaned), sum(1 for g in cleaned if g["critical"]))
    return cleaned


def _short_json(obj: dict | list | None) -> str:
    """Truncate a JSON blob for inclusion in prompts."""
    import json
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        return "{}"
    return s[:3000] + ("... [truncated]" if len(s) > 3000 else "")


def _field_present_in_proposals(field_name: str, proposals: dict | None) -> bool:
    """Heuristic: does `field_name` appear in any of the proposal strings/keys?"""
    if not proposals:
        return False
    import json
    blob = json.dumps(proposals, ensure_ascii=False).lower()
    return field_name.lower().replace(" ", "_") in blob or field_name.lower() in blob
