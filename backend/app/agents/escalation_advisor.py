# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Escalation advisor — drafts the *review team's* own assessment.

When a partner query is escalated to Risk / InfoSec / Tech, the reviewer needs
a starting point written from THEIR perspective — a draft of the team's
position the PM can act on — NOT the partner-facing holding reply the
feasibility resolver produces ("we've routed this to InfoSec, will revert in
10 days"). This agent produces that team-facing draft, which the reviewer
validates, edits, or replaces.

Pure function; the caller stores the result on the EscalationTicket.ai_suggestion.
"""
import logging
import re

from app.core.domain.registry import prompt_block
from app.core.llm import call_llm
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

# Domain nouns from the active pack, resolved at import (registry pattern).
# Under the default UPI pack these render byte-identically to the previous
# hardcoded prompt. The team TRIAD (risk/infosec/tech) is platform structure
# (EscalationTicket routing keys); only the naming is domain-flavoured.
_AUTHORITY = prompt_block("authority", "the ecosystem authority")
_DOMAIN = prompt_block("domain_name", "").strip()
_DOMAIN_ADJ = f"{_DOMAIN} " if _DOMAIN else ""

_TEAM_FRAMING = {
    "risk": (
        f"the {_AUTHORITY} Risk team (financial / fraud / settlement / regulatory-risk exposure)"
    ),
    "infosec": (
        f"the {_AUTHORITY} Information Security team (security, data protection, authentication, "
        "key handling, PII)"
    ),
    "tech": (
        f"the {_AUTHORITY} Technology team (architecture, API contract, performance, integration "
        "feasibility)"
    ),
}

_SYSTEM = f"""You are a senior reviewer on __TEAM_DESC__.

A {_DOMAIN_ADJ}ecosystem partner asked a question that {_AUTHORITY} escalated to your team for a
formal position. Draft YOUR TEAM'S assessment — the substantive expert opinion
the Product Manager will use to answer the partner.

Write it as the reviewer's own input, NOT as a reply to the partner. Do NOT
write "we will revert" or "this has been routed to..." — that is the PM's job.
Give the actual position.

Output EXACTLY this format — the two markers on their own lines, comment first:

REVIEW_COMMENT:
<the concise comment the reviewer would actually submit back to the PM — 2-4 sentences, plain prose, no markdown headers or bullets. Lead with the verdict, name the one key reason and the binding condition. This is the draft that goes into the reviewer's comment box and must stand on its own.>

ASSESSMENT:
<detailed markdown — the full reasoning the PM/reviewer can study. Cover: verdict (acceptable / not acceptable / acceptable-with-conditions); the specific rule/control/spec/risk driving it, citing policy/BRD/TSD sections shown in context; required conditions or mitigations; what the PM must NOT concede; any additional information your team needs.>

Rules:
  - Write as the reviewer's own input, NOT as a reply to the partner. No "we will
    revert" / "this has been routed to..." — give the actual position.
  - Emit the REVIEW_COMMENT section first, then ASSESSMENT. Use the markers
    verbatim. No preamble before REVIEW_COMMENT.
  - Ground your assessment STRICTLY in the COMMITTED PRODUCT KIT (the documents
    {_AUTHORITY} has already shipped to partners) plus {_AUTHORITY}_POLICY and the BRD/TSD shown
    below. Do NOT propose scope, limits, dates, fields, or features beyond what
    those committed documents state. If the partner is asking for something
    outside the committed scope, say so plainly — your position is that it is not
    committed (and cite what the kit DOES commit) — rather than inventing a new
    commitment on {_AUTHORITY}'s behalf.

""" + ANTI_INJECTION_CLAUSE

_MAX_DOC_CHARS = 8000


def _doc_block(change_docs: list[dict], budget: int = _MAX_DOC_CHARS, per_doc: int = 3000) -> str:
    parts: list[str] = []
    total = 0
    for d in change_docs or []:
        content = (d.get("content") or "").strip()
        if not content:
            continue
        piece = f"### {d.get('doc_type', 'document')}\n{content[:per_doc]}"
        if total + len(piece) > budget:
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n".join(parts)


def _parse(raw: str) -> dict:
    """Split the marker format into {review_comment, assessment}.

    Tolerant by design: the comment is emitted first, so even if the assessment
    is truncated mid-stream the comment still parses cleanly."""
    s = (raw or "").strip()
    if not s:
        return {"review_comment": "", "assessment": ""}

    # Locate the ASSESSMENT marker (case-insensitive, own line or inline).
    m = re.search(r"(?im)^\s*ASSESSMENT:\s*", s)
    if m:
        comment_part = s[:m.start()]
        assessment = s[m.end():].strip()
    else:
        comment_part = s
        assessment = ""

    comment = re.sub(r"(?im)^\s*REVIEW_COMMENT:\s*", "", comment_part).strip()
    return {"review_comment": comment, "assessment": assessment}


async def assess_escalation(
    *,
    team: str,
    question_text: str,
    change_docs: list[dict] | None = None,
    product_kit_docs: list[dict] | None = None,
    policy_content: str = "",
    partner_name: str = "",
) -> dict:
    """Return {"assessment": <full markdown>, "review_comment": <concise draft>}.

    `product_kit_docs` is the committed Product Kit (already shipped to partners)
    — surfaced as the authoritative scope so the assessment stays within what
    NPCI has committed and doesn't invent new commitments. `change_docs` (BRD/TSD)
    is supporting context. Both empty on failure — the ticket still opens."""
    team_desc = _TEAM_FRAMING.get(team, _TEAM_FRAMING["tech"])
    # NB: plain replace, not str.format — the prompt contains literal JSON
    # braces that str.format would try (and fail) to interpret as fields.
    system = _SYSTEM.replace("__TEAM_DESC__", team_desc)

    parts = []
    if policy_content:
        parts += [f"{_AUTHORITY}_POLICY.md (authoritative):", policy_content[:12000], "", "---"]
    # Committed Product Kit first, with its own generous budget so it is never
    # truncated away by the BRD/TSD below — it's the authoritative scope.
    kit = _doc_block(product_kit_docs or [], budget=14000, per_doc=3500)
    if kit:
        parts += [
            f"COMMITTED PRODUCT KIT — documents {_AUTHORITY} has already shipped to partners. "
            "This is the authoritative scope of what is committed. Do NOT suggest "
            "anything beyond what these documents state:",
            wrap_untrusted(kit, "PRODUCT_KIT"), "", "---",
        ]
    docs = _doc_block(change_docs or [])
    if docs:
        parts += ["Supporting change documents (BRD / TSD):", wrap_untrusted(docs, "CHANGE_DOCUMENTS"), "", "---"]
    parts += [
        f"Partner: {partner_name or 'Unknown'}",
        "",
        wrap_untrusted(question_text, "PARTNER_QUESTION"),
        "",
        f"Draft your ({team}) team's assessment per the instructions.",
    ]

    try:
        raw = await call_llm(
            system=system,
            messages=[{"role": "user", "content": "\n".join(parts)}],
            max_tokens=3000,
            agent_name="escalation_advisor",
        )
    except Exception as exc:
        logger.warning("escalation_advisor failed for team=%s: %s", team, exc)
        return {"assessment": "", "review_comment": ""}

    return _parse(raw)
