# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cross-partner cluster analyzer.

For a cluster of counter-proposals, generates an AI summary of all their
justifications, a recommendation for the PM (accept/modify/reject), and a
confidence score. Summaries describe what the partners actually asked for
(from their justification text) — not the section they filed under.
"""
import json
import logging
import re

from app.core.llm import call_llm
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)


def _loads_json_object(raw: str) -> dict:
    """Tolerant parse of a single JSON object (strips ``` fences).

    Mirrors the cluster_router / negotiation_classifier parsers. The bare
    json.loads used here previously threw whenever the model wrapped its
    object in ```json fences or added a stray line, which dropped the call
    into the fallback summary — the main reason clusters showed the generic
    'partners are requesting changes in the X area' heading.
    """
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    data = json.loads(s)
    return data if isinstance(data, dict) else {}

_CLUSTER_SYSTEM = """You are a product manager assistant at the Authority analysing a cluster of similar
negotiation requests from ecosystem partners (banks, PSPs, TPAPs).

Your job is to:
1. Summarise what all the partners are asking for (common theme + key reasons).
2. Identify any conflicting sub-positions within the cluster.
3. Recommend what the Authority should do: "accept", "modify", or "reject".
4. Provide a confidence score 0.0–1.0 for your recommendation.

Respond with exactly one JSON object — nothing else:
{
  "summary": "2-3 sentence summary of what the partners are requesting and why",
  "recommendation": "accept" | "modify" | "reject",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence explaining the recommendation"
}

""" + ANTI_INJECTION_CLAUSE


async def analyze_cluster(
    category: str,
    topic_summary: str,
    justifications: list[str],
    partner_names: list[str],
) -> dict:
    """Generate AI summary + recommendation for a cluster.

    Args:
        category:      The shared request_category.
        topic_summary: Short description of the cluster topic.
        justifications: List of partner justification strings.
        partner_names:  Names of the partners in the cluster.

    Returns dict with keys: summary, recommendation, confidence, reasoning.
    Falls back to a neutral default if the LLM call fails.
    """
    numbered = "\n".join(
        f"{i+1}. [{name}]: {j}"
        for i, (name, j) in enumerate(zip(partner_names, justifications))
    )

    msg = (
        f"Category: {category}\n"
        f"Topic: {topic_summary}\n"
        f"Number of requests: {len(justifications)}\n\n"
        f"Partner justifications:\n{wrap_untrusted(numbered, 'PARTNER_JUSTIFICATIONS')}\n\n"
        "Analyse this cluster and respond with the JSON object as instructed."
    )

    try:
        raw = await call_llm(
            system=_CLUSTER_SYSTEM,
            messages=[{"role": "user", "content": msg}],
            max_tokens=700,
            agent_name="cluster_analyzer",
        )
        data = _loads_json_object(raw)
        return {
            "summary": str(data.get("summary", "")) or _fallback_summary(len(justifications), topic_summary),
            "recommendation": str(data.get("recommendation", "modify")),
            "confidence": float(data.get("confidence", 0.5)),
            "reasoning": str(data.get("reasoning", "")),
        }
    except Exception as exc:
        logger.warning("Cluster analysis LLM call failed: %s — using text-based fallback", exc)
        return {
            # Fall back to the cluster's text-derived topic, NOT its category:
            # the category is frozen to the first member and can misrepresent a
            # text-clustered group (the bug behind the misleading heading).
            "summary": _fallback_summary(len(justifications), topic_summary),
            "recommendation": "modify",
            "confidence": 0.5,
            "reasoning": "AI analysis unavailable — PM review required.",
        }


def _fallback_summary(n: int, topic_summary: str) -> str:
    """Neutral, text-based summary used when the LLM call/parse fails."""
    topic = (topic_summary or "").strip()
    partners = f"{n} partner{'s' if n != 1 else ''}"
    if topic:
        return f"{partners} raising: {topic}."
    return f"{partners} with related counter-proposals awaiting PM review."
