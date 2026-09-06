# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Delta grounding — code-back the plan amendments a reconciliation folds in.

Plan CREATION reads the code via a multi-turn, 14-tool agentic analysis. Plan
AMENDMENT — the brd-wins / custom deltas a reconciliation folds in — historically
went in as verbatim text with only a presence grep, so repeated reconciliation
cycles let the "code-grounded" plan drift from the code.

This closes that gap without a second full analysis: for each accepted delta it pulls
REAL code evidence from the SAME repo checkout the clarification-stage analysis used,
then one structured pass produces grounding — impact, schema / data-model additions,
reuse, impacted paths, a risk flag, and (only when the code genuinely leaves it open)
a question for the PM.

Scope note: this is the retrieval-grounded cut (same shape as ``doc_alignment`` /
``plan_coverage``), not the full multi-turn analysis loop — a deliberate lower-risk
first implementation that still reads the real code. Fail-open by contract: any
failure returns ``{"status": "failed"}`` and the caller folds with the prior
presence-check behaviour.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.llm import call_llm_structured
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE
from app.agents.upload_reconciler import _wire_names

logger = logging.getLogger(__name__)

_MAX_DELTAS = 12
_MAX_SNIPPETS = 10
_RISK = {"none", "low", "high"}

_SYSTEM = (
    "You ground proposed amendments to a ratified network solution plan in the ACTUAL code. "
    "You are given the ratified plan, the accepted DELTAS (each is a point where the uploaded "
    "BRD overrides the plan), and CODE EVIDENCE grepped from the repo the plan was built on. "
    "For each delta, decide how it lands in the code and return structured grounding:\n"
    "  impact: 1–2 sentences — what building this actually touches, grounded in the evidence.\n"
    "  schema_inventory_add: [{\"path\":\"..\",\"note\":\"..\"}] — .xsd/.xjb schema files this delta adds or "
    "changes. Only real ones; [] if none.\n"
    "  data_model_changes_add: [string] — data-model / persistence changes it implies.\n"
    "  reuse: [string] — existing messages / flows / symbols from the evidence it should reuse.\n"
    "  impacted_paths: [string] — repo file paths it touches, taken from the evidence.\n"
    "  risk: none|low|high — 'high' ONLY if it likely breaks or contradicts existing code.\n"
    "  risk_note: one sentence when risk != none, else \"\".\n"
    "  overturns_ratified: true ONLY if the delta CONTRADICTS a decision the plan already ratified "
    "(not merely extends it).\n"
    "  question: a single question for the PM ONLY when the code leaves the decision genuinely open "
    "(how to proceed); otherwise \"\". Never invent a question.\n"
    "Be concrete and evidence-bound — never name a file path or symbol that isn't in the evidence.\n"
    'Respond with ONLY JSON: {"deltas":[{"directive":"..","impact":"..","schema_inventory_add":[],'
    '"data_model_changes_add":[],"reuse":[],"impacted_paths":[],"risk":"none","risk_note":"",'
    '"overturns_ratified":false,"question":""}]}\n' + ANTI_INJECTION_CLAUSE
)


# Generic words that would only add grep noise (the plan already frames the domain).
_STOP = {"the", "this", "that", "with", "from", "into", "must", "should", "shall", "will",
         "when", "instead", "authoritative", "uploaded", "here", "reviewer", "ruling",
         "change", "feature", "each", "every", "user", "users", "based", "which", "their",
         "transaction", "transactions", "system", "request", "response", "customer", "using",
         "provide", "provided", "return", "returns", "value", "values", "details", "existing"}


def _salient_terms(deltas: list) -> list:
    """Grep terms for each delta — wire names (checkout-precise) PLUS CamelCase identifiers
    and significant domain words, so a BUSINESS delta with no wire name ('90-day inactivity',
    'consent propagation') still pulls real code evidence when the concept exists in the repo.
    Deduped, order-preserved, capped at 14."""
    import re
    raw: list = []
    for d in deltas:
        text = f"{d.get('conflict') or ''} {d.get('directive') or ''}"
        raw.extend(sorted(_wire_names({"text": text})))                 # wire schemas / messages
        raw.extend(re.findall(r"\b[A-Za-z]+[A-Z][A-Za-z0-9]+\b", text))  # CamelCase identifiers
        for w in re.findall(r"\b[a-zA-Z]{5,}\b", text):                 # significant domain words
            if w.lower() not in _STOP:
                raw.append(w)
    seen: set = set()
    out: list = []
    for t in raw:
        k = (t or "").lower()
        if k and k not in seen:
            seen.add(k)
            out.append(t)
        if len(out) >= 14:
            break
    return out


def _grep_evidence(checkouts: list, terms: list) -> list:
    """Grep the analysis checkout(s) for each term → up to ``_MAX_SNIPPETS`` ``file:line``
    hits, so the grounding call reasons over REAL code, not just names. Best-effort.

    Routed through core.process_executor.ProcessExecutor (S5 call-site
    migration, closing ARCHITECTURE_REVIEW_ACTIONS.md S5 for this file) —
    `git` is already in the allowlist (it's a Maven-build-adjacent tool
    ADR-0003 named explicitly), so this migration needed no allowlist
    change, only the invocation mechanism."""
    from app.core.process_executor import (
        ProcessExecutionRequest, ProcessNotAllowedError, ProcessTimeoutError,
        run_sync,
    )

    seen: set = set()
    ev: list = []
    for t in terms:
        t = (t or "").strip()
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        for d in checkouts:
            try:
                result = run_sync(ProcessExecutionRequest(
                    command="git", args=["grep", "-n", "-i", "--max-count", "3", t],
                    cwd=d, timeout_s=30, actor="delta_grounding",
                ))
                if result.exit_code == 0 and (result.stdout or "").strip():
                    for ln in result.stdout.splitlines()[:3]:
                        ev.append(f"[{d.name}] {ln.strip()[:200]}")
                        if len(ev) >= _MAX_SNIPPETS:
                            return ev
            except (ProcessNotAllowedError, ProcessTimeoutError):
                continue
            except Exception:  # noqa: BLE001 — one grep failing just yields less evidence
                continue
    return ev


async def ground_deltas(db, *, change_id: str, deltas: list, doc_kind: str,
                        plan_contract: str, checkouts: list) -> dict:
    """Ground the accepted reconciliation deltas in the real checkout. Returns
    ``{status, grounded_at, deltas:[{directive, impact, schema_inventory_add,
    data_model_changes_add, reuse, impacted_paths, risk, risk_note,
    overturns_ratified, question}]}``. ``status`` = ok | failed | skipped.
    Fail-open: never raises."""
    deltas = [d for d in (deltas or [])
              if isinstance(d, dict) and (d.get("directive") or d.get("conflict"))][:_MAX_DELTAS]
    if not deltas or not (plan_contract or "").strip():
        return {"status": "skipped"}

    evidence = _grep_evidence(checkouts, _salient_terms(deltas)) if checkouts else []

    listing = "\n".join(f"- delta {i + 1}: {d.get('directive') or d.get('conflict')}"
                        for i, d in enumerate(deltas))
    ev_block = "\n".join(evidence) if evidence else "(no wire entities matched — evidence is the plan alone)"
    user = (f"RATIFIED PLAN:\n{wrap_untrusted(plan_contract[:7000], 'PLAN')}\n\n"
            f"ACCEPTED DELTAS (the plan is being amended to each of these):\n{listing}\n\n"
            f"CODE EVIDENCE (grep of the analysis checkout):\n{wrap_untrusted(ev_block[:6000], 'CODE')}\n\n"
            "Return the grounding JSON now.")
    try:
        # Forced tool use (was prose JSON): ~12 deltas run ~3k output tokens, and the AiNxt
        # gateway strips stop_reason, so a truncated prose JSON used to fail the parse
        # silently → 'failed' . Tool-validated arguments remove the parse
        # step entirely; 8k output keeps >2x headroom for the payload itself.
        data = await call_llm_structured(
            _SYSTEM, user,
            schema={"type": "object",
                    "properties": {"deltas": {
                        "type": "array", "maxItems": _MAX_DELTAS,
                        "items": {"type": "object",
                                  "properties": {
                                      "directive": {"type": "string"},
                                      "impact": {"type": "string"},
                                      "schema_inventory_add": {
                                          "type": "array",
                                          "items": {"type": "object",
                                                    "properties": {"path": {"type": "string"},
                                                                   "note": {"type": "string"}},
                                                    "required": ["path"]}},
                                      "data_model_changes_add": {"type": "array",
                                                                 "items": {"type": "string"}},
                                      "reuse": {"type": "array", "items": {"type": "string"}},
                                      "impacted_paths": {"type": "array",
                                                         "items": {"type": "string"}},
                                      "risk": {"type": "string", "enum": sorted(_RISK)},
                                      "risk_note": {"type": "string"},
                                      "overturns_ratified": {"type": "boolean"},
                                      "question": {"type": "string"}},
                                  "required": ["directive", "impact", "risk"]}}},
                    "required": ["deltas"]},
            tool_name="record_grounding", agent_name="delta_grounding", max_tokens=8000,
        )
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("ground_deltas failed for %s: %s", change_id, e)
        return {"status": "failed"}
    items = data.get("deltas") if isinstance(data, dict) else None
    if not items:
        return {"status": "failed"}

    out: list = []
    for it in items:
        if not isinstance(it, dict):
            continue
        risk = it.get("risk") if it.get("risk") in _RISK else "none"
        out.append({
            "directive": str(it.get("directive") or "")[:400],
            "impact": str(it.get("impact") or "")[:400],
            "schema_inventory_add": [{"path": str(x.get("path") or "")[:300],
                                      "note": str(x.get("note") or "")[:200]}
                                     for x in (it.get("schema_inventory_add") or [])
                                     if isinstance(x, dict) and x.get("path")][:6],
            "data_model_changes_add": [str(x)[:200] for x in (it.get("data_model_changes_add") or [])][:6],
            "reuse": [str(x)[:160] for x in (it.get("reuse") or [])][:6],
            "impacted_paths": [str(x)[:300] for x in (it.get("impacted_paths") or [])][:8],
            "risk": risk,
            "risk_note": str(it.get("risk_note") or "")[:300] if risk != "none" else "",
            "overturns_ratified": bool(it.get("overturns_ratified")),
            "question": str(it.get("question") or "")[:400],
        })
    return {"status": "ok", "grounded_at": datetime.now(timezone.utc).isoformat(), "deltas": out}
