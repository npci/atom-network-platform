# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Feasibility resolver — authority-side recommendation engine.

Given an inbound partner message (query or counter-proposal), reads the
change's BRD/TSD/Kit + AUTHORITY_POLICY.md from the DB, calls an LLM, and
produces a structured recommendation the PM can act on.

The 6 recommended actions:
  refine_kit          — BRD/TSD gap; loop back to Phase A clarification.
  clarify_inline      — PM answers directly from existing knowledge.
  placeholder         — Holding response; the answer ships in the revised kit (next version, 24h round).
  wait_for_round_close— Similar asks from multiple partners; consolidate.
  revise_workflow     — Architectural flaw; take back to design review.
  escalate            — Needs compliance / architecture / legal / SVP sign-off.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings as _settings
from app.core.domain.registry import prompt_block
from app.core.llm import call_llm, get_model, get_provider
from app.models.authority_policy import AuthorityPolicy
from app.models.phase_c import A2AMessage
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

# Domain nouns from the active pack, resolved at import (the registry pattern —
# system prompts here are module-level constants). Under the default UPI pack
# these render byte-identically to the previous hardcoded prompt.
_AUTHORITY = prompt_block("authority", "the ecosystem authority")
_PARTNER_DESC = prompt_block("partner_descriptor", "An ecosystem partner")

MAX_POLICY_CHARS = 30000
MAX_DOC_CHARS = 12000
MAX_TOTAL_DOC_CHARS = 50000

VALID_ACTIONS = {
    "refine_kit", "clarify_inline", "placeholder",
    "wait_for_round_close", "revise_workflow", "escalate",
}

SYSTEM_PROMPT = f"""You are the {_AUTHORITY} Feasibility Resolver — an AI assistant for {_AUTHORITY} Product Managers.

{_PARTNER_DESC} has sent a message about a proposed change.
Your job: recommend what the PM should DO, and draft the response they should send.

You are given:
  1. {_AUTHORITY}_POLICY.md — {_AUTHORITY}'s authoritative policy for change-management decisions.
  2. The change's BRD / TSD / Product Kit documents.
  3. The partner's inbound message (a query asking for information, or a counter-proposal
     requesting a change to {_AUTHORITY}'s terms).

Produce a single JSON object with these fields:

  - recommended_action: one of:
      "refine_kit"           — Message reveals a gap in the Product Kit itself. PM should loop
                               back to Phase A clarification stage and update BRD / TSD before
                               responding to the partner.
      "clarify_inline"       — PM can answer directly with information {_AUTHORITY} already has but
                               didn't include in the docs. No structural change needed.
      "placeholder"          — Acknowledge with a holding response. Use when the ask is valid
                               but the substantive answer is a spec change that will be delivered
                               in the revised Product Kit (next version) within the 24-hour round
                               window — NOT a separate process or arbitrary date.
      "wait_for_round_close" — One of many similar asks from different partners in this round.
                               Don't reply individually; consolidate at round close and broadcast.
      "revise_workflow"      — The counter exposes an architectural flaw in the proposed feature.
                               Pause partner-facing response; take the workflow back to design review.
      "escalate"             — Needs sign-off from one of {_AUTHORITY}'s review teams before the PM can
                               answer. PM cannot decide alone; route to the team named in
                               escalation_target first. STRICTLY one of: risk / infosec / tech.

  - action_summary: 1-sentence human-readable reason for the chosen action (15–25 words).

  - draft_response: The EXACT text the PM can send verbatim to the partner. Professional,
    concise, addresses the partner's specific ask. 3–8 sentences. Reference BRD/TSD sections
    where relevant. If action is "wait_for_round_close" or "escalate", the draft is a
    holding-response, not the final answer.

  - reasoning: list of 2–4 bullets explaining why this action was chosen. Cite {_AUTHORITY}_POLICY
    sections and change-doc sections.

  - cited_policy_sections: list of {_AUTHORITY}_POLICY section labels referenced (e.g. ["§2", "§4"]).
    Empty list when policy wasn't cited.

  - cited_change_docs: list of change-doc types referenced (e.g. ["brd", "tsd"]). Empty when N/A.

  - confidence: "low" / "medium" / "high".

  - escalation_target: when action is "escalate", STRICTLY one of "risk" / "infosec" / "tech".
      "risk"    — financial / fraud / settlement / regulatory-risk exposure.
      "infosec" — security, data protection, authentication, key handling, PII.
      "tech"    — architecture, API contract, performance, integration feasibility.
    null otherwise.

CRITICAL rules:
  - Output STRICT JSON only. No prose, no markdown fences, no preamble.
  - {_AUTHORITY}_POLICY.md is authoritative. When it states a tolerance or a non-negotiable, respect it.
  - For "clarify_inline", the draft_response must actually ANSWER the partner's question using
    information from the BRD/TSD/Policy. If you can't answer, recommend "placeholder" instead.
  - For "refine_kit", identify which BRD/TSD section needs updating in the reasoning.
  - For "escalate", always specify escalation_target.
  - Keep draft_response professional, direct, and partner-friendly.
  - NEVER invent timelines, SLAs, deadlines, or processes {_AUTHORITY} does not have. The platform
    supports exactly ONE forward commitment: a revised Product Kit (next version) delivered
    within the 24-hour round window. The draft_response must NOT promise "a clarifying addendum",
    "X business days" (e.g. "7–14 business days"), "we will revert", a separate review/SLA cycle,
    or any artifact other than the next Product Kit version. Do NOT state a date or duration at
    all — when a spec change is needed, the system appends the canonical "revised Product Kit
    (vN+1) within 24 hours" line automatically, so your draft must not add its own timeline.
  - Any query not resolved within the 24-hour round is automatically accepted as-is at round
    close. Never imply an open-ended or post-round follow-up.

""" + ANTI_INJECTION_CLAUSE


def _load_policy(db: Session) -> str:
    """Read the AUTHORITY_POLICY content from the singleton DB row."""
    row = db.get(AuthorityPolicy, 1)
    if not row or not row.content:
        return ""
    content = row.content
    if len(content) > MAX_POLICY_CHARS:
        content = content[:MAX_POLICY_CHARS] + "\n\n... (truncated)"
    return content


def _truncate_change_docs(documents: list[dict]) -> str:
    parts: list[str] = []
    total = 0
    for d in documents:
        content = (d.get("content") or "").strip()
        if not content:
            continue
        chunk = content[:MAX_DOC_CHARS]
        header = f"### {d.get('doc_type', 'document')}"
        piece = f"{header}\n{chunk}"
        if total + len(piece) > MAX_TOTAL_DOC_CHARS:
            remaining = MAX_TOTAL_DOC_CHARS - total
            if remaining > 500:
                parts.append(piece[:remaining])
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n".join(parts)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl > 0:
            s = s[nl + 1:]
    if s.rstrip().endswith("```"):
        s = s.rstrip()[:-3].rstrip()
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def _validate(obj: dict) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return False, "not an object"
    for k in ("recommended_action", "action_summary", "draft_response", "reasoning", "confidence"):
        if k not in obj:
            return False, f"missing key: {k}"
    if obj["recommended_action"] not in VALID_ACTIONS:
        return False, f"invalid action: {obj['recommended_action']}"
    if obj["recommended_action"] == "escalate" and not obj.get("escalation_target"):
        return False, "escalate without escalation_target"
    return True, ""


async def resolve_message(
    *,
    partner_name: str,
    message_text: str,
    message_type: str,
    change_title: str,
    change_docs: list[dict],
    policy_content: str,
    rag_context: str = "",
    team_input: str = "",
    frozen: bool = False,
) -> dict | None:
    """Run the resolver. Returns the recommendation dict or None on failure.

    Async because the authority-side `call_llm` is async (unlike the partner
    side). Pure function — no DB writes. Caller persists.
    """
    if not policy_content:
        logger.warning("resolve_message: NPCI policy content is empty")

    doc_block = _truncate_change_docs(change_docs)

    user_parts = [
        f"{_AUTHORITY}_POLICY.md",
        "",
        policy_content or "(no policy loaded)",
        "",
        "---",
        "",
        f"Change: {wrap_untrusted(change_title, 'CHANGE_TITLE')}",
        "",
        "---",
        "",
        "Change documents:",
        "",
        wrap_untrusted(doc_block or "(no documents)", "CHANGE_DOCUMENTS"),
        "",
        "---",
    ]
    if rag_context:
        user_parts.extend([
            "",
            "Relevant knowledge base excerpts:",
            "",
            wrap_untrusted(rag_context, "RAG_CONTEXT"),
            "",
            "---",
        ])
    if team_input:
        # A review team (Risk/InfoSec/Tech) already weighed in on a prior
        # escalation of this same message. Fold their input in so the draft
        # the PM reviews reflects the team's position — and so this re-run
        # can move off "escalate" now that the cross-team sign-off exists.
        user_parts.extend([
            "",
            "Internal review-team input on this message (authoritative — already obtained):",
            "",
            wrap_untrusted(team_input, "TEAM_INPUT"),
            "",
            "Given this team input, you no longer need to escalate unless a DIFFERENT team is required.",
            "",
            "---",
        ])
    if frozen:
        # The kit is FROZEN — the final Product Kit version has shipped and no
        # further versions will be issued. The "next Product Kit version" forward
        # commitment the system prompt assumes does NOT exist here, so the only
        # valid responses are an inline answer or an escalation. This overrides
        # the default action menu for this message.
        user_parts.extend([
            "",
            "IMPORTANT — THIS CHANGE IS FROZEN: the final Product Kit has shipped and NO "
            "further versions will be issued. There is no 'next Product Kit version'.",
            "Therefore you MUST treat this purely as a clarification:",
            "  - recommended_action MUST be \"clarify_inline\" (answer the partner's question "
            "directly from the existing BRD / TSD / Product Kit / Policy), or \"escalate\" if a "
            "review team must sign off first.",
            "  - You MUST NOT use \"refine_kit\", \"placeholder\", \"revise_workflow\", or "
            "\"wait_for_round_close\", and the draft_response MUST NOT promise, imply, or "
            "reference any document change, revised/updated Product Kit, new version, addendum, "
            "or future delivery. Only answer what is already specified.",
            "  - If the answer genuinely is not present in the documents, say so plainly and note "
            "the partner may raise an Emergency Issue if it blocks implementation — do NOT promise "
            "a revision.",
            "",
            "---",
        ])
    user_parts.extend([
        "",
        wrap_untrusted(partner_name, "PARTNER_NAME"),
        f"Message type: {message_type}",
        "",
        "Partner's message:",
        "",
        wrap_untrusted(message_text, "PARTNER_MESSAGE"),
        "",
        "Produce the JSON recommendation per the schema in the system prompt.",
    ])

    # Retry transient LLM failures (connection blips to the provider) with a
    # short backoff. The provider SDK retries 429s internally; this loop adds
    # resilience to connection-level errors, which otherwise strand the query
    # with no recommendation (the PM UI shows the "analysing…" spinner until
    # this returns — a hard failure here leaves it spinning).
    text = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            text = await call_llm(
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": "\n".join(user_parts)}],
                max_tokens=4000,
            )
            break
        except Exception as e:
            last_err = e
            logger.warning("resolve_message: LLM attempt %d/3 failed: %s", attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))  # 2s, then 4s
    if text is None:
        logger.error("resolve_message: LLM call failed after retries: %s", last_err)
        return None

    # call_llm may return a coroutine on some providers — belt-and-suspenders
    import inspect
    if inspect.isawaitable(text):
        text = await text

    obj = _extract_json(text)
    if obj is None:
        logger.error(
            "resolve_message: LLM output not valid JSON; len=%d head=%r tail=%r",
            len(text), text[:600], text[-400:],
        )
        return None

    ok, err = _validate(obj)
    if not ok:
        logger.error("resolve_message: invalid shape: %s", err)
        return None

    obj["_meta"] = {
        "model_used": f"{get_provider()}:{get_model()}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "resolve_message: action=%s confidence=%s partner=%s",
        obj["recommended_action"], obj.get("confidence"), partner_name,
    )
    return obj


# Escalation-target aliases. The three canonical teams (risk / infosec / tech)
# are PLATFORM structure (they key EscalationTicket routing), not domain
# vocabulary. A few aliases ("settlement" -> risk) are domain-flavoured but the
# mapping is a defensive normaliser for stray model output, not operator-visible
# text — deliberately left inline rather than moved to pack data; a domain
# wanting different aliases should extend this seam then.
_TEAM_SYNONYMS = {
    "risk": "risk", "fraud": "risk", "regulatory": "risk", "compliance": "risk",
    "legal": "risk", "settlement": "risk",
    "infosec": "infosec", "security": "infosec", "infosecurity": "infosec",
    "privacy": "infosec", "data_protection": "infosec",
    "tech": "tech", "technology": "tech", "architecture": "tech",
    "engineering": "tech", "svp": "tech",
}


def _normalize_team(target: str | None) -> str:
    """Map a resolver escalation_target onto one of risk / infosec / tech.

    The prompt constrains the model to the three teams, but a stray synonym
    (or a legacy target like 'compliance') shouldn't strand the escalation —
    map it, defaulting to 'tech' as the catch-all engineering reviewer."""
    key = (target or "").strip().lower().replace(" ", "_")
    return _TEAM_SYNONYMS.get(key, "tech")


async def auto_resolve_background(
    a2a_message_id: str,
    change_id: str,
    team_input: str = "",
) -> None:
    """Background task fired by the executor on inbound QUERY / COUNTER_PROPOSAL.

    Async because resolve_message awaits the authority-side call_llm (which is
    async). FastAPI's BackgroundTasks supports async callables natively.

    Opens a fresh DB session, assembles context, calls the resolver, and
    persists the recommendation. Failure is logged and swallowed — the PM
    can re-run manually from the UI.

    `team_input` is set on the loop-back re-run (after a review team responds
    to an escalation): it's folded into the resolver prompt so the new draft
    reflects the team's position.
    """
    from sqlalchemy import select
    from app.core.database import SessionLocal
    from app.models.change_request import ChangeRequest
    from app.models.escalation_ticket import EscalationTicket
    from app.models.phase_c import A2AMessage, PartnerAgent
    from app.models.resolver_recommendation import ResolverRecommendation

    db = SessionLocal()
    try:
        msg = db.get(A2AMessage, a2a_message_id)
        if msg is None:
            logger.warning("auto_resolve: a2a_message %s not found", a2a_message_id)
            return

        partner = db.get(PartnerAgent, msg.partner_id)
        partner_name = partner.name if partner else "Unknown"

        message_text = ""
        message_type = "query"
        payload = msg.payload or {}
        inner = payload.get("payload", payload)
        message_text = inner.get("message") or inner.get("justification") or json.dumps(inner)
        # A2AMessage.task_type is a plain String (protocol-v1 audit column);
        # guard for the legacy enum shape too.
        _tt = msg.task_type.value if hasattr(msg.task_type, "value") else (msg.task_type or "")
        if "counter" in _tt.lower():
            message_type = "counter"
        elif "blocker" in _tt.lower():
            # A blocker carries STRUCTURED fields, not `message`/`justification` — without
            # this it fell through to json.dumps(), so the resolver reasoned over raw JSON.
            # Flatten what a triager actually needs. `message_type='blocker'` also keeps it
            # out of the BRD mandatory pre-check below (queries only): a blocker is an
            # implementation obstacle, not a spec negotiation, and must never be
            # auto-rejected as a BRD violation.
            message_type = "blocker"
            _parts = [f"BLOCKER ({inner.get('severity') or 'high'}): "
                      f"{inner.get('description') or '(no description)'}"]
            if inner.get("impact"):
                _parts.append(f"Impact: {inner['impact']}")
            if inner.get("investigation_done"):
                _parts.append("Investigation already done: "
                              + "; ".join(str(x) for x in inner["investigation_done"]))
            if inner.get("options_considered"):
                _parts.append("Options the partner considered: "
                              + "; ".join(str(x) for x in inner["options_considered"]))
            if inner.get("requested_action_from_npci"):
                _parts.append(f"Requested from {_AUTHORITY}: {inner['requested_action_from_npci']}")
            message_text = "\n".join(_parts)

        # ── BRD mandatory-violation pre-check (queries only) ──────────────
        # A small focused LLM call runs BEFORE the full resolver. If the
        # query targets a non-negotiable BRD requirement we auto-reject here
        # and return immediately — no resolver LLM, no escalation advisor.
        if message_type == "query" and not team_input:
            from app.models.phase_c import BRDRequirement
            mandatory_reqs = (
                db.query(BRDRequirement)
                .filter(
                    BRDRequirement.change_request_id == change_id,
                    BRDRequirement.is_mandatory.is_(True),
                )
                .all()
            )
            if mandatory_reqs:
                from app.models.brd import BRD as _BRD_pre
                brd_text_pre = ("\n\n").join(
                    (b.content or "")
                    for b in db.execute(select(_BRD_pre).where(_BRD_pre.change_request_id == change_id)).scalars()
                )[:6000]
                from app.agents.negotiation_classifier import _ai_mandatory_violation_check
                violates, req_label, reason = await _ai_mandatory_violation_check(
                    mandatory_reqs, message_text, {}, brd_text_pre
                )
                if violates:
                    logger.info(
                        "auto_resolve: BRD mandatory violation query=%s req=%s — skipping resolver",
                        a2a_message_id, req_label,
                    )
                    violation_summary = (
                        f'Auto-rejected: this query contradicts the mandatory BRD requirement'
                        f' "{req_label}". No further negotiation on this point is accepted.'
                    )
                    rec_content = {
                        "recommended_action": "auto_rejected_brd",
                        "action_summary": violation_summary,
                        "draft_response": "",
                        "reasoning": [reason] if reason else [],
                        "confidence": "high",
                        "cited_policy_sections": [],
                        "cited_change_docs": ["brd"],
                        "violated_requirement": req_label,
                        "violated_reason": reason,
                        "_meta": {
                            "model_used": "rule:brd_mandatory_check",
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                    db.add(ResolverRecommendation(
                        change_request_id=change_id,
                        partner_id=msg.partner_id,
                        a2a_message_id=a2a_message_id,
                        message_type="query",
                        version=1,
                        content=json.dumps(rec_content),
                        model_used="rule:brd_mandatory_check",
                    ))
                    db.commit()
                    if partner:
                        from app.models.phase_c import A2ATaskType
                        from app.services.partner_dispatch import notify_partner
                        corr_id = (msg.payload or {}).get("correlation_id") or inner.get("correlation_id")
                        try:
                            await notify_partner(
                                partner.id,
                                A2ATaskType.CLARIFICATION_RESPONSE.value,
                                {
                                    "change_id": change_id,
                                    "message_kind": "BRD_VIOLATION",
                                    "requirement": req_label,
                                    "reason": reason,
                                    "response": violation_summary,
                                    "channel": "general",
                                },
                                change_id=change_id,
                                label=partner.name,
                                correlation_id=corr_id,
                                context="BRD violation notify",
                            )
                        except Exception:
                            logger.exception(
                                "auto_resolve: BRD violation A2A notify failed change=%s msg=%s",
                                change_id, a2a_message_id,
                            )
                    return

        # Load change docs
        from app.models.brd import BRD
        from app.models.tech_spec import TechSpec
        from app.services.product_kit_query import latest_kit_docs
        docs: list[dict] = []
        for brd in db.execute(select(BRD).where(BRD.change_request_id == change_id)).scalars():
            docs.append({"doc_type": "brd", "content": brd.content or ""})
        for ts in db.execute(select(TechSpec).where(TechSpec.change_request_id == change_id)).scalars():
            docs.append({"doc_type": "tsd", "content": ts.content or ""})
        # Latest version per kit doc_type only — version history must not feed
        # duplicate copies of the same logical doc into the resolver prompt.
        # Also kept separately as `kit_docs` so the escalation advisor can treat
        # the committed Product Kit as the authoritative scope.
        kit_docs: list[dict] = []
        for pk in latest_kit_docs(db, change_id):
            d = {"doc_type": pk.doc_type or "kit", "content": pk.content or ""}
            docs.append(d)
            kit_docs.append(d)

        policy_content = _load_policy(db)

        # Optional RAG retrieval — best-effort, degrade gracefully.
        rag_context = ""
        try:
            from app.rag.retrieval import retrieve
            results = retrieve(message_text, top_k=5)
            if results:
                rag_context = "\n\n".join(
                    f"[{r.get('source_file', 'unknown')}] {r.get('content', '')[:500]}"
                    for r in results if r.get("content")
                )
        except Exception as rag_err:
            logger.debug("auto_resolve: RAG retrieval failed (degrading): %s", rag_err)

        # Freeze gate: once the final kit ships there is no "next version", so the
        # resolver must answer post-freeze queries as pure clarifications — never
        # promise a document/kit revision.
        _cr_frozen = db.get(ChangeRequest, change_id)
        frozen = bool(_cr_frozen and getattr(_cr_frozen, "negotiation_frozen_at", None) is not None)

        result = await resolve_message(
            partner_name=partner_name,
            message_text=message_text,
            message_type=message_type,
            change_title="",
            change_docs=docs,
            policy_content=policy_content,
            rag_context=rag_context,
            team_input=team_input,
            frozen=frozen,
        )
        if result is None:
            logger.warning("auto_resolve: resolver returned None for %s", a2a_message_id)
            return

        # ── Doc-impact (Step 5) ───────────────────────────────────────────
        # When NPCI's answer is a real reply (not a holding response while we
        # escalate or wait for round close), assess whether honouring it needs
        # a kit document change. The result is stored on the recommendation and
        # consumed by the round-close consolidation (Slice 4). When a change is
        # needed we append the static "next version in 24h" notice to the draft
        # the PM will send.
        action = result.get("recommended_action")
        # Skip doc-impact + the "next version in 24h" promise entirely when the
        # change is frozen: there is no next version to ship, and a frozen-change
        # query is a clarification, not a trigger for a document revision.
        if not frozen and action not in ("escalate", "wait_for_round_close"):
            from app.agents.doc_impact import KIT_DOC_TYPES, assess_doc_impact
            avail = [d["doc_type"] for d in docs if d.get("doc_type") in KIT_DOC_TYPES]
            impact = await assess_doc_impact(
                query_text=message_text,
                authority_reply=result.get("draft_response") or "",
                available_doc_types=avail or None,
            )
            result["doc_impact"] = impact
            if impact.get("needs_doc_change"):
                cr = db.get(ChangeRequest, change_id)
                next_version = (getattr(cr, "negotiation_version", 1) or 1) + 1
                result["draft_response"] = (result.get("draft_response") or "").rstrip() + (
                    f"\n\nAn updated Product Kit (v{next_version}) reflecting this will be "
                    "delivered within 24 hours."
                )

        meta = result.get("_meta", {})
        row = ResolverRecommendation(
            change_request_id=change_id,
            partner_id=msg.partner_id,
            a2a_message_id=a2a_message_id,
            message_type=message_type,
            version=1,
            content=json.dumps(result),
            model_used=meta.get("model_used"),
        )
        db.add(row)
        db.commit()
        logger.info(
            "auto_resolve: recommendation stored for msg=%s action=%s",
            a2a_message_id, result.get("recommended_action"),
        )

        # ── Escalation routing ────────────────────────────────────────────
        # When the resolver wants a review team to sign off, open a ticket so
        # the team sees it in their inbox. Skip on the loop-back re-run
        # (team_input set) — that re-run is the *result* of an escalation, not
        # a new one. Idempotent: one OPEN ticket per (message, team).
        if not team_input and result.get("recommended_action") == "escalate":
            team = _normalize_team(result.get("escalation_target"))
            existing = (
                db.query(EscalationTicket)
                .filter(
                    EscalationTicket.a2a_message_id == a2a_message_id,
                    EscalationTicket.team == team,
                    EscalationTicket.status != "closed",
                )
                .first()
            )
            if existing is None:
                # Create the ticket FIRST and commit so it surfaces in the team
                # inbox immediately. The team-facing AI draft (a ~20s LLM call)
                # is generated after and patched onto the row — the inbox shows a
                # "drafting…" state meanwhile rather than the escalation being
                # invisible until the draft lands.
                ticket = EscalationTicket(
                    change_request_id=change_id,
                    partner_id=msg.partner_id,
                    a2a_message_id=a2a_message_id,
                    team=team,
                    status="open",
                    question_text=message_text,
                    escalation_reason=result.get("action_summary"),
                )
                db.add(ticket)
                db.commit()
                logger.info(
                    "auto_resolve: opened escalation ticket msg=%s team=%s",
                    a2a_message_id, team,
                )
                # Best-effort team-facing draft, patched onto the (already
                # visible) ticket. A failure leaves it draftless — the reviewer
                # can still respond.
                try:
                    from app.agents.escalation_advisor import assess_escalation
                    advice = await assess_escalation(
                        team=team,
                        question_text=message_text,
                        # BRD/TSD only — the kit goes in product_kit_docs so it's
                        # not duplicated across both context sections.
                        change_docs=[d for d in docs if d.get("doc_type") in ("brd", "tsd")],
                        product_kit_docs=kit_docs,
                        policy_content=policy_content,
                        partner_name=partner_name,
                    )
                    ticket.ai_suggestion = advice.get("assessment") or None
                    ticket.ai_comment_draft = advice.get("review_comment") or None
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("escalation_advisor failed for msg=%s — ticket left without draft", a2a_message_id)
    except Exception as exc:
        # Rollback to return a clean connection to the pool — without this,
        # a failed transaction can poison the next session that grabs the
        # same pooled connection (shows up as InFailedSqlTransaction).
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("auto_resolve failed for %s: %s", a2a_message_id, exc)
    finally:
        db.close()
