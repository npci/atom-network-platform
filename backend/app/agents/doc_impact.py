# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Doc-impact agent — does NPCI's reply imply a Product Kit document change?

Step 5 of the negotiation protocol: after NPCI decides how to answer a partner
query, determine whether honouring that answer requires editing one or more kit
documents — and if so, which. The output feeds the round-close consolidation
(Slice 4), which regenerates the affected docs into the next kit version.

Pure function; the caller persists the result (onto the resolver recommendation
JSON) and decides whether to append the "v2 kit in 24h" partner notice.
"""
import json
import logging
import re

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE
from app.core.domain.registry import prompt_block
from app.core.llm import call_llm

logger = logging.getLogger(__name__)

# Domain nouns from the active pack, resolved at import (registry pattern).
# Under the default UPI pack these render byte-identically to the previous
# hardcoded prompt.
_AUTHORITY = prompt_block("authority", "the ecosystem authority")
_DOMAIN = prompt_block("domain_name", "").strip()
_DOMAIN_ADJ = f"{_DOMAIN} " if _DOMAIN else ""

# The kit doc types the agent may name. Kept in sync with ProductKitDocType.
# `product_doc` retired — superseded by `product_note` (see RETIRED_DOC_TYPES in
# app.models.product_kit). Leaving it here let this agent flag a document that can no
# longer be generated, producing revision plans nothing could act on.
KIT_DOC_TYPES = (
    "product_deck", "promo_video", "explainer_video",
    "faq", "cert_test_cases", "circular", "manifest",
    "prototype_screens", "product_note",
)

_SYSTEM = f"""You are an {_AUTHORITY} change-management analyst.

A partner asked a question about a {_DOMAIN_ADJ}feature change. {_AUTHORITY} has decided how to
answer. Your job: decide whether honouring {_AUTHORITY}'s answer requires CHANGING one
or more Product Kit documents, and if so, which.

Only flag a document change when {_AUTHORITY}'s answer commits to something the current
kit does NOT already say — a new/changed limit, date, scope item, API detail,
FAQ clarification, or circular wording. A pure restatement of what the kit
already contains is NOT a document change.

Choose document types ONLY from this exact list:
  product_doc, product_deck, promo_video, explainer_video, faq,
  cert_test_cases, circular, manifest, prototype_screens, product_note

Respond with exactly one JSON object — nothing else:
{{
  "needs_doc_change": true|false,
  "documents": ["faq", "circular"],   // [] when needs_doc_change is false
  "rationale": "one sentence explaining the decision"
}}

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


async def assess_doc_impact(
    *,
    query_text: str,
    authority_reply: str,
    available_doc_types: list[str] | None = None,
) -> dict:
    """Return {needs_doc_change, documents, rationale}.

    Defaults to no-change on any LLM/parse failure — a wrong "yes" would
    trigger a needless kit regeneration, so we fail safe toward "no".
    `documents` is always filtered to the known kit doc types.
    """
    avail = available_doc_types or list(KIT_DOC_TYPES)
    msg = (
        "----- PARTNER QUESTION (untrusted data) -----\n"
        f"{query_text}\n"
        f"----- {_AUTHORITY}'S DECIDED ANSWER (untrusted data) -----\n"
        f"{authority_reply}\n"
        "----- END -----\n\n"
        f"Documents that exist in the current kit: {', '.join(avail)}\n\n"
        f"Does {_AUTHORITY}'s answer require changing any kit document? Respond with the JSON object."
    )
    try:
        raw = await call_llm(
            system=_SYSTEM,
            messages=[{"role": "user", "content": msg}],
            max_tokens=300,
            agent_name="doc_impact",
        )
        obj = _parse(raw)
    except Exception as exc:
        logger.warning("doc_impact: LLM failed (%s) — defaulting to no change", exc)
        obj = None

    if not obj:
        return {"needs_doc_change": False, "documents": [], "rationale": "assessment unavailable"}

    needs = bool(obj.get("needs_doc_change"))
    docs = [d for d in (obj.get("documents") or []) if d in KIT_DOC_TYPES] if needs else []
    # If the model said "yes" but named no valid doc, downgrade to no-change
    # rather than trigger an empty regeneration.
    if needs and not docs:
        needs = False
    return {
        "needs_doc_change": needs,
        "documents": docs,
        "rationale": str(obj.get("rationale") or ""),
    }
