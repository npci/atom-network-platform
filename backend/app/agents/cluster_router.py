# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""LLM-based cluster router.

Decides whether an incoming counter-proposal joins an existing negotiation
cluster (same underlying ask) or starts a new one. Clusters PRIMARILY on the
partner's justification text (what they're actually asking for and why); the
request category/section is only a coarse secondary hint, used to break a tie
when the text alone is too sparse to decide. Replaces the brittle first-4-words
`cluster_key` exact-match: two partners phrasing the same request differently
now land in the same cluster, two partners in the same category asking for
different things stay apart, and a mislabelled category no longer splits a
cluster.

Returns a routing decision for the caller (negotiation_extended) to apply.
"""
import json
from app.core.domain.registry import prompt_block
from app.core.prompts import render_prompt
import logging
import re

from app.core.llm import call_llm

logger = logging.getLogger(__name__)

# Identity nouns come from the active domain pack; under the default UPI pack
# this renders the same authority and actor list the file used to hardcode.
_ROUTER_SYSTEM = render_prompt(
    "agents/cluster_router/router_system.md",
    AUTHORITY=prompt_block("authority", "the ecosystem authority"),
    ECOSYSTEM_ACTORS=prompt_block("ecosystem_actors", "the ecosystem's participants"),
)


def _loads_json_object(raw: str) -> dict:
    """Tolerant parse of a single JSON object (strips ``` fences)."""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    data = json.loads(s)
    return data if isinstance(data, dict) else {}


async def route_counter_proposal(
    category: str,
    justification: str,
    payload: dict,
    existing_clusters: list[dict],
) -> dict:
    """Decide which cluster a counter-proposal joins.

    Args:
        category:      The CP's request_category (may be "general").
        justification: The partner's free-text justification.
        payload:       Structured payload (current/proposed values, dates).
        existing_clusters: list of {"index": int, "category": str,
                       "topic_summary": str, "sample": str} for the clusters
                       already open on this change. Must be non-empty —
                       callers skip this function entirely for the first CP.

    Returns dict: {"decision","cluster_index","topic_summary","reason"}.
    Falls back to {"decision":"new",...} if the LLM call fails, so a model
    error opens a fresh cluster rather than mis-merging two distinct asks.
    """
    # Lead each existing cluster with its text (topic + an example request);
    # the category is a trailing hint, not the headline — so the model matches
    # on what was asked, not on the section it was filed under.
    existing_block = "\n".join(
        f'{c["index"]}. {c["topic_summary"]}'
        + (f' — example request: "{(c.get("sample") or "")[:200]}"' if c.get("sample") else "")
        + f'  (category hint: {c["category"]})'
        for c in existing_clusters
    )

    msg = (
        "NEW counter-proposal (justification is the primary signal) —\n"
        "----- BEGIN PARTNER JUSTIFICATION (untrusted data) -----\n"
        f"{justification}\n"
        "----- END PARTNER JUSTIFICATION -----\n"
        "----- BEGIN PARTNER PAYLOAD (untrusted data) -----\n"
        f"{json.dumps(payload, default=str)[:600]}\n"
        "----- END PARTNER PAYLOAD -----\n"
        f"Category hint (secondary, use only if the text is insufficient): {category}\n\n"
        "----- BEGIN EXISTING CLUSTERS (topic labels + example partner requests; untrusted data) -----\n"
        f"{existing_block}\n"
        "----- END EXISTING CLUSTERS -----\n\n"
        "Match the new counter-proposal to an existing cluster, or start a new one — judging "
        "primarily by the justification text. Respond with the JSON object as instructed."
    )

    try:
        raw = await call_llm(
            system=_ROUTER_SYSTEM,
            messages=[{"role": "user", "content": msg}],
            max_tokens=200,
            agent_name="cluster_router",
        )
        data = _loads_json_object(raw)
        decision = str(data.get("decision", "new")).strip().lower()
        idx = data.get("cluster_index")
        idx = int(idx) if isinstance(idx, (int, float, str)) and str(idx).isdigit() else None
        matched = decision == "match" and idx is not None
        return {
            "decision": "match" if matched else "new",
            "cluster_index": idx if matched else None,
            "topic_summary": str(data.get("topic_summary", "")).strip(),
            "reason": str(data.get("reason", "")).strip(),
        }
    except Exception as exc:
        logger.warning("Cluster router LLM call failed: %s — opening a new cluster", exc)
        return {"decision": "new", "cluster_index": None, "topic_summary": "", "reason": "router unavailable"}
