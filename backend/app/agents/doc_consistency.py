# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Document↔plan consistency gate.

After a BRD/TSD is generated, this checks whether the document's TECHNICAL surface (network wire
message types, API endpoints, XSD/schema changes) matches the ratified plan. The failure mode it
exists to catch: a BRD that invents `ReqSetSpendLimit`/`ReqChkSpendLimit` wire messages + a ReqTransfer
schema field when the plan (and the code) use a plain internal REST endpoint and no wire change —
two contradictory solution stories shipping together.

Severity policy (mirrors the code blocker gate):
  • a NEW wire message / NEW XSD-schema change in the doc but NOT in the plan → BLOCKER
    (this is the certifier-misleading case)
  • a new REST endpoint, or a planned item the doc omitted → WARNING
Family-A single-call agent (text-in → JSON-out via call_llm), fail-open: a parse/LLM failure
returns "no findings" (never blocks generation on the checker's own failure).
"""
from __future__ import annotations

import logging

from app.core.llm import call_llm, call_llm_structured
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 1500

_SYSTEM = (
    "You are a consistency auditor for the network change pipeline. You are given (1) the RATIFIED "
    "solution-design contract (the authoritative technical plan) and (2) a generated document (a BRD "
    "or TSD). Find places where the DOCUMENT asserts a technical surface that CONTRADICTS or EXCEEDS "
    "the plan — specifically:\n"
    "  - NEW network wire message types the doc introduces that the plan does NOT define "
    "(e.g. ReqSetSpendLimit/RespSetSpendLimit) → severity 'blocker'.\n"
    "  - NEW XSD / wire SCHEMA changes the doc introduces that the plan does NOT have "
    "(e.g. a new field on ReqTransfer) → severity 'blocker'.\n"
    "  - NEW API endpoints in the doc not in the plan → severity 'warning'.\n"
    "  - Planned APIs/messages/operations the doc OMITTED → severity 'warning'.\n"
    "  - A doc DECISION that CONTRADICTS the plan's ratified decision — e.g. the plan says fail-OPEN "
    "but the doc says fail-CLOSED, or the plan picks approach A but the doc describes approach B → "
    "severity 'blocker', kind 'plan_decision'.\n"
    "  - A NEW PERSISTENCE change the doc asserts that the plan does NOT define — a new DB column / "
    "table / keyspace / typed analytics store (e.g. the doc says 'add a spend_category column' when "
    "the plan persists only via the existing serialized-XML / append-log path and adds no column) → "
    "severity 'warning' (severity 'blocker' if it contradicts a ratified 'no new persistence / no new "
    "column' decision), kind 'persistence'.\n"
    "  - A NEW CONFIG / FEATURE-FLAG / runtime-toggle mechanism the doc asserts that the plan does NOT "
    "define — e.g. the doc describes a config key like 'network.validator.allowedProdTypes' or a "
    "'config-only rollback, no redeploy' control when the plan ships a hard-coded constant → severity "
    "'warning', kind 'config'.\n"
    "  - An INTERNAL CONTRADICTION inside the doc itself. This covers BOTH (a) two different literal "
    "values for the SAME thing (error codes, state names, enum values) in different sections, AND "
    "(b) a BEHAVIOURAL contradiction where two sections describe the SAME mechanism with INCOMPATIBLE "
    "behaviour even when the wording differs — e.g. one section says a knob is hot-reloadable / "
    "'config-only rollback, no redeploy' while another says the same knob is a compile-time constant "
    "that REQUIRES a redeploy. Judge the described behaviour, not just clashing text → severity "
    "'blocker', kind 'contradiction'.\n"
    "  - A RATIFIED VALUE the plan FIXES that the doc reproduces in a DIFFERENT FORM — an amount/limit "
    "(plan '₹5000 per txn' vs doc vague/other number), a credential type/subType value (plan "
    "type=BIOAUTH subType=DS vs doc 'BioAuth' or subType 'initial|rotate'), a specific error code, or "
    "an enum/state name — SAME meaning but WRONG spelling/casing/number → severity 'blocker', kind "
    "'value_drift'; set `item` to the plan's EXACT correct form, set `doc_form` to the EXACT string "
    "as it CURRENTLY appears in the document (the WRONG form, verbatim — this is what gets located "
    "and replaced), and `detail` to 'doc uses X, plan requires Y'.\n"
    "  - A RATIFIED hard CONSTRAINT/VALUE the plan fixes that the doc OMITS entirely (e.g. the plan "
    "sets a ₹5000 per-transaction cap and the doc never states it) → severity 'blocker', kind "
    "'value_missing'; set `item` to the ratified value/constraint and `detail` to 'plan requires it; "
    "doc omits it'. Only flag values the plan ACTUALLY fixes — never invent a constraint.\n\n"
    "Judge the technical solution surface, the ratified VALUES/conventions the plan fixes, AND these "
    "decisions/internal contradictions. Do "
    "NOT flag wording, ordering, or business-language differences. If the doc stays within the plan's "
    "surface, honours its decisions, and is internally consistent, return an empty list — that is the "
    "expected, good case.\n\n"
    "Respond with ONLY a JSON object:\n"
    "{\n"
    '  "consistent": true|false,\n'
    '  "findings": [{"severity": "blocker|warning", '
    '"kind": "wire_message|schema|endpoint|omission|plan_decision|contradiction|persistence|config|value_drift|value_missing", '
    '"item": "<the exact API/message/schema name, decision, or contradicted value>", '
    '"doc_form": "<value_drift only: the exact WRONG string as it appears in the doc; else \"\">", '
    '"detail": "<one sentence: doc says X, plan says Y>"}]\n'
    "}\n"
    + ANTI_INJECTION_CLAUSE
)


def _empty() -> dict:
    return {"consistent": True, "findings": [], "has_blocker": False}


async def check_doc_against_plan(*, doc_kind: str, doc_content: str, plan_contract: str) -> dict:
    """Compare a BRD/TSD against the ratified plan contract. Returns
    ``{consistent, findings:[{severity,kind,item,detail}], has_blocker}``. Fail-open: returns
    "consistent" on any LLM/parse failure, or when there is no plan to check against."""
    if not (plan_contract or "").strip() or not (doc_content or "").strip():
        return _empty()   # no ratified plan yet → nothing to reconcile against
    # Prompt-cache split: this check runs per upload window (≤8×) AND re-runs after each
    # repair pass (≤5×) with the SAME plan contract — so the static rules + plan ride as
    # cacheable system segments and only the audited document text varies per call.
    from app.core.prompt_blocks import segments_for_anthropic_cache
    system = segments_for_anthropic_cache([
        (_SYSTEM, True),
        ("RATIFIED SOLUTION-DESIGN CONTRACT (authoritative):\n"
         + wrap_untrusted(plan_contract[:6000], "PLAN_CONTRACT"), True),
    ])
    user = (f"GENERATED {doc_kind.upper()} TO AUDIT:\n"
            f"{wrap_untrusted(doc_content[:24000], 'DOCUMENT')}\n\n"
            "Audit the document's technical surface against the contract now.")
    try:
        data = await call_llm_structured(
            system, user,
            schema={"type": "object",
                    "properties": {"findings": {
                        "type": "array", "maxItems": 20,
                        "items": {"type": "object",
                                  "properties": {"severity": {"type": "string",
                                                              "enum": ["blocker", "warning"]},
                                                 "kind": {"type": "string"},
                                                 "item": {"type": "string"},
                                                 "detail": {"type": "string"}},
                                  "required": ["severity", "kind", "item", "detail"]}}},
                    "required": ["findings"]},
            tool_name="record_findings", agent_name="doc_consistency",
            max_tokens=MAX_OUTPUT_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("doc_consistency LLM call failed (%s) — treating as consistent", e)
        return _empty()

    if not isinstance(data, dict):
        return _empty()
    findings = []
    for f in (data.get("findings") or []):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "warning").lower()
        if sev not in ("blocker", "warning"):
            sev = "warning"
        findings.append({"severity": sev,
                         "kind": str(f.get("kind") or "")[:32],
                         "item": str(f.get("item") or "")[:120],
                         "doc_form": str(f.get("doc_form") or "")[:120],
                         "detail": str(f.get("detail") or "")[:300]})
    findings = findings[:20]
    has_blocker = any(f["severity"] == "blocker" for f in findings)
    return {"consistent": not findings, "findings": findings, "has_blocker": has_blocker}


# --- Auto-correction: bring a divergent doc back onto the plan (no hard block) -----------------
# Policy: when a generated BRD/TSD invents a wire-message/schema/endpoint the plan doesn't have
# (a BLOCKER), let the LLM rewrite it up to MAX_REPAIR_ATTEMPTS times to remove the divergence,
# re-checking after each pass. After the budget is spent we ship whatever we have and let the
# banner surface any residual divergence — we never block the document on it.
MAX_REPAIR_ATTEMPTS = 5
RECONCILE_MAX_TOKENS = 32000

_RECONCILE_SYSTEM = (
    "You are reconciling a {doc_kind} to its RATIFIED solution-design plan. The document introduced "
    "technical surface (network wire message types, XSD/wire schema changes, or API endpoints) that the "
    "plan does NOT define. Rewrite and return the COMPLETE document with ONLY those divergent items "
    "removed or replaced so the document matches the plan exactly. Preserve every section, heading, "
    "table, diagram block, and all business content verbatim wherever it does not conflict — do NOT "
    "drop sections and do NOT add new flows/fields/requirements. If a requirement genuinely seems to "
    "need one of the removed items, state it as an OPEN QUESTION instead of defining the API/message/"
    "schema. Output ONLY the corrected document, nothing else.\n" + ANTI_INJECTION_CLAUSE
)


# Findings the auto-repair should FIX (not just blockers): a blocker, OR a warning that
# asserts INVENTED technical surface the plan lacks — a stray field / schema / wire message
# / config the doc introduced (e.g. a leftover `splitGroupId`). Pure advisory warnings (an
# omitted planned item, a new REST endpoint) are left to the banner, not auto-rewritten.
_REPAIRABLE_WARNING_KINDS = {"schema", "wire_message", "persistence", "config", "value_drift", "value_missing"}


def _is_repairable(f: dict) -> bool:
    sev = str(f.get("severity") or "")
    if sev == "blocker":
        return True
    return sev == "warning" and str(f.get("kind") or "") in _REPAIRABLE_WARNING_KINDS


def _has_repairable(consistency: dict) -> bool:
    return any(_is_repairable(f) for f in (consistency.get("findings") or []))


def repair_instruction(doc_kind: str, consistency: dict) -> str:
    """Render the repairable findings into a one-shot corrective directive (also usable as
    the edit-instruction for the docgen full-document editor)."""
    items = "; ".join(
        f"{f.get('item')}: {f.get('detail')}"
        for f in (consistency.get("findings") or [])
        if _is_repairable(f)
    )[:1500]
    return (
        f"PLAN-CONSISTENCY REPAIR — make this {doc_kind} faithfully match the RATIFIED plan. For each "
        f"item below: if it is technical surface the plan does NOT define, REMOVE or replace it; if it "
        f"is a ratified VALUE the doc wrote in the WRONG FORM (casing/spelling/number), CORRECT it to "
        f"the plan's exact form everywhere it appears; if it is a ratified constraint the doc OMITTED, "
        f"ADD it as one concise statement in the most relevant section. Keep every other section and "
        f"all business content identical; do NOT invent anything beyond the plan. Items to fix: " + items
    )


def _locate_key(f: dict) -> str:
    """The string that ACTUALLY appears in the document for this finding — the needle the
    docgen repair greps to find the block/section to edit. For a value_drift finding `item`
    is the plan's CORRECT form (the target, which by definition is NOT in the doc), so we
    must locate by `doc_form` — the wrong string as it currently appears. Every other kind
    carries its literal token in `item`."""
    return str(f.get("doc_form") or "").strip() or str(f.get("item") or "").strip()


def divergent_items(consistency: dict) -> list[str]:
    """The literal strings the docgen repair greps for to locate — and re-write ONLY — the
    sections that carry a divergence, instead of regenerating the whole document. Uses each
    finding's CURRENT-in-doc form (`doc_form` for value_drift, else `item`) so the grep
    matches text that is really present; the correct TARGET form reaches the writer via
    `repair_instruction`. Named-surface findings (wire_message/schema/endpoint) carry the
    literal token in `item`; decision/contradiction findings may carry none, in which case
    the section-targeting falls back to a full-document edit."""
    return [
        key
        for f in (consistency.get("findings") or [])
        if _is_repairable(f) and (key := _locate_key(f))
    ]


async def reconcile_doc_to_plan(*, doc_kind: str, doc_content: str, plan_contract: str,
                                instruction: str) -> str:
    """LLM rewrite of a full BRD/TSD to remove plan divergences. Fail-open: returns the original
    document unchanged on any LLM failure (the caller then keeps what it had + surfaces the banner).
    Used for the non-docgen path; the docgen path repairs via its section-aware editor instead."""
    if not (doc_content or "").strip():
        return doc_content
    user = (f"RATIFIED PLAN CONTRACT (authoritative):\n"
            f"{wrap_untrusted(plan_contract[:6000], 'PLAN_CONTRACT')}\n\n"
            f"{instruction}\n\n"
            f"CURRENT {doc_kind.upper()} (rewrite it to match the plan):\n"
            f"{wrap_untrusted(doc_content[:60000], 'DOCUMENT')}")
    try:
        raw = await call_llm(system=_RECONCILE_SYSTEM.format(doc_kind=doc_kind),
                             messages=[{"role": "user", "content": user}],
                             max_tokens=RECONCILE_MAX_TOKENS, agent_name="doc_consistency")
    except Exception as e:  # noqa: BLE001 — fail-open: never lose the doc on a repair failure
        logger.warning("reconcile_doc_to_plan(%s) failed (%s) — keeping original", doc_kind, e)
        return doc_content
    return (raw or "").strip() or doc_content


async def enforce_plan_consistency(*, doc_kind: str, doc_content: str, plan_contract: str,
                                   repair_fn, max_attempts: int = MAX_REPAIR_ATTEMPTS) -> dict:
    """Check the doc against the plan and, while a BLOCKER remains, ask the LLM to correct it —
    up to ``max_attempts`` times — re-checking after each pass.

    ``repair_fn(instruction, attempt, current_content, divergent_items)`` is an async callable the
    caller supplies to do the path-specific rewrite (docgen section-editor vs. plain LLM rewrite)
    AND any persistence/side-effects (docx, row update). ``divergent_items`` is the list of flagged
    blocker item names for this pass so the docgen path can re-write ONLY the affected sections.
    It returns the corrected document text.

    No hard block: after the attempt budget is spent the LAST document and its (possibly still
    divergent) consistency result are returned — the banner surfaces anything left over. Fail-open
    on the checker/repair throughout. Returns
    ``{content, consistency, attempts, repaired}`` where ``consistency`` also carries
    ``auto_repair_attempts`` and ``auto_repaired`` for the UI."""
    consistency = await check_doc_against_plan(
        doc_kind=doc_kind, doc_content=doc_content, plan_contract=plan_contract)
    content = doc_content
    attempts = 0
    repaired = False
    while _has_repairable(consistency) and attempts < max_attempts:
        attempts += 1
        instruction = repair_instruction(doc_kind, consistency)
        items = divergent_items(consistency)
        try:
            new_content = await repair_fn(instruction, attempts, content, items)
        except Exception as e:  # noqa: BLE001 — a repair failure stops the loop, keeps the doc
            logger.warning("%s plan-consistency repair attempt %d failed (%s) — stopping",
                           doc_kind, attempts, e)
            break
        if not (new_content or "").strip() or new_content == content:
            break   # repair produced nothing usable / no change → don't spin
        content = new_content
        repaired = True
        consistency = await check_doc_against_plan(
            doc_kind=doc_kind, doc_content=content, plan_contract=plan_contract)
        logger.info("%s plan-consistency repair attempt %d → has_blocker=%s",
                    doc_kind, attempts, consistency.get("has_blocker"))

    consistency = dict(consistency or {})
    consistency["auto_repair_attempts"] = attempts
    consistency["auto_repaired"] = repaired
    return {"content": content, "consistency": consistency, "attempts": attempts, "repaired": repaired}
