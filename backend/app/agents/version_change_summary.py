# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Version change-summary agent — partner-facing "what changed" note.

When the Authority ships a revised Product Kit (v2, v3), partners need to know what
changed from the previous version and why. This agent turns the consolidated
round outcomes (resolved counter clusters + doc-impact decisions) into a short
markdown summary that rides along in the dispatch envelope.

Pure function; the caller (new_version_and_ship) gathers the outcomes and
persists/ships the result.
"""
import logging

from app.core.llm import call_llm
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

_SYSTEM = """You are an authority product communications writer.

The Authority is shipping a revised network feature Product Kit to ecosystem partners. Write
a concise, partner-facing summary of what changed from the previous version and
why, based on the resolved negotiation outcomes provided.

Rules:
- Markdown. Start with a one-line headline, then 3-8 bullet points.
- Each bullet: what changed + the affected document(s) when known. Keep it
  factual and neutral; this goes to banks/PSPs/TPAPs.
- Do NOT invent changes not present in the outcomes. If outcomes are sparse,
  write a short honest summary rather than padding.
- No preamble, no sign-off, no "Dear partner".

""" + ANTI_INJECTION_CLAUSE


async def summarize_version_changes(
    *,
    previous_version: int,
    new_version: int,
    outcomes: list[dict],
    change_title: str = "",
) -> str:
    """Return a markdown change summary. Empty string on failure (the ship
    still proceeds — the summary is informative, not load-bearing).

    `outcomes` items look like:
      {"topic": str, "decision": str, "rationale": str, "documents": [str]}
    """
    if not outcomes:
        return ""

    outcome_lines = []
    for o in outcomes:
        docs = ", ".join(o.get("documents") or []) or "—"
        outcome_lines.append(
            f"- topic: {o.get('topic', '')!r}; decision: {o.get('decision', '')}; "
            f"docs: {docs}; rationale: {o.get('rationale', '')}"
        )
    lines = [
        f"Change: {wrap_untrusted(change_title or '(untitled)', 'CHANGE_TITLE')}",
        f"Previous version: v{previous_version} → New version: v{new_version}",
        "",
        "Resolved outcomes feeding this revision:",
        wrap_untrusted("\n".join(outcome_lines), "ROUND_OUTCOMES"),
    ]
    user = "\n".join(lines) + "\n\nWrite the partner-facing change summary."

    try:
        raw = await call_llm(
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            max_tokens=1200,
            agent_name="version_change_summary",
        )
        return (raw or "").strip()
    except Exception as exc:
        logger.warning("version_change_summary failed (%s) — shipping without summary", exc)
        return ""
