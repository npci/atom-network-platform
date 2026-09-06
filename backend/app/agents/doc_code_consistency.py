# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Document↔code consistency gate (post-codegen).

Where `doc_consistency` checks a BRD/TSD against the ratified PLAN *before* code exists, this checks
the TSD against the actual CODE — the unified diff produced by the agentic run — *after* the change
set is frozen. The CODE is the source of truth at this point.

The failure mode it exists to catch (observed on change fa4631e3 "spend-category on Lite"): a TSD
that asserts a `spend_category` DB column, a `network.validator.allowedProdTypes` config key with
"config-only rollback, no redeploy", and clean `U16/U09` Acks — none of which the code builds. The
existing plan/fidelity/predicate gates never catch this because they all compare to the plan, never
to the code.

Two finding classes, two actions:
  • DOC OVER-CLAIM (persistence / config / error_code / method / contradiction the code doesn't
    support) → the DOC is wrong → reconcile the TSD to the code (caller re-persists). severity 'warning'.
  • CODE MISSING (a behaviour the TSD AND plan require that the diff does NOT implement) → a real
    code gap → severity 'blocker'; the caller routes it into the existing review/blocking path.

Family-A single-call agent (text-in → JSON-out via call_llm), fail-open: any LLM/parse failure
returns "consistent" so a checker fault never blocks or mutates a code run.
"""
from __future__ import annotations

import logging

from app.core.llm import call_llm
from app.core.json_recovery import parse_llm_json
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 1500
RECONCILE_MAX_TOKENS = 32000

_SYSTEM = (
    "You are a document↔code consistency auditor for the network change pipeline. You are given (1) a "
    "generated TECHNICAL SPEC (TSD) and (2) the ACTUAL CODE CHANGE that implements it (a unified diff), "
    "and optionally (3) the ratified plan for context. The CODE is the source of truth. Find every "
    "place where the TSD asserts a CONCRETE, CHECKABLE IMPLEMENTATION FACT that the diff does NOT "
    "support:\n"
    "  - PERSISTENCE the TSD claims but the code does not add — a new DB column / table / migration / "
    "entity field (e.g. TSD says 'add a spend_category column' but there is no DDL / migration / field "
    "in the diff) → kind 'persistence', severity 'warning'.\n"
    "  - CONFIG / FEATURE-FLAG the TSD claims but the code does not have — a named config key or a "
    "'config-only, no redeploy' control when the diff ships a hard-coded constant (e.g. TSD cites "
    "'network.validator.allowedProdTypes' / config-reload rollback but the code is a literal "
    "`Set.of(\"the network\",\"UPILITE\")`) → kind 'config', severity 'warning'.\n"
    "  - ERROR CODES the TSD names that the code never emits (e.g. TSD tables show 'U16'/'U09' but the "
    "diff derives the code differently or not at all) → kind 'error_code', severity 'warning'.\n"
    "  - A NAMED METHOD / CLASS / ENDPOINT the TSD says was added or changed that is ABSENT from the "
    "diff → kind 'method', severity 'warning'.\n"
    "  - An INTERNAL CONTRADICTION in the TSD that the code resolves one way (the TSD describes two "
    "incompatible behaviours for the same mechanism; the diff does exactly one) → kind 'contradiction', "
    "severity 'warning'.\n"
    "  - CODE MISSING: a behaviour the TSD AND the ratified plan BOTH require that the diff does NOT "
    "implement — a real CODE gap, not a doc over-claim → kind 'code_missing', severity 'blocker'.\n\n"
    "Only flag CONCRETE, checkable claims tied to a named column/key/code/symbol. Do NOT flag prose, "
    "naming, ordering, or business language. If the TSD only describes what the diff actually does, "
    "return an empty list — that is the expected, good case.\n\n"
    "Respond with ONLY a JSON object:\n"
    "{\n"
    '  "consistent": true|false,\n'
    '  "findings": [{"severity": "warning|blocker", '
    '"kind": "persistence|config|error_code|method|contradiction|code_missing", '
    '"item": "<the exact column / key / code / symbol>", '
    '"detail": "<one sentence: TSD says X, the diff shows Y>"}]\n'
    "}\n"
    + ANTI_INJECTION_CLAUSE
)

_DOC_FABRICATION_KINDS = frozenset({"persistence", "config", "error_code", "method", "contradiction"})


def _empty() -> dict:
    return {"consistent": True, "findings": [], "has_blocker": False}


async def check_doc_against_code(*, tsd_content: str, diff_text: str, plan_contract: str = "") -> dict:
    """Compare a TSD against the actual code diff. Returns
    ``{consistent, findings:[{severity,kind,item,detail}], has_blocker}``. ``has_blocker`` is True only
    for a ``code_missing`` finding (a real code gap). Fail-open: returns "consistent" on any LLM/parse
    failure, or when there is no TSD or no diff to reconcile."""
    if not (tsd_content or "").strip() or not (diff_text or "").strip():
        return _empty()
    ctx = (f"RATIFIED PLAN (context only):\n{wrap_untrusted(plan_contract[:4000], 'PLAN')}\n\n"
           if (plan_contract or "").strip() else "")
    user = (f"{ctx}ACTUAL CODE CHANGE — unified diff (the source of truth):\n"
            f"{wrap_untrusted(diff_text[:40000], 'DIFF')}\n\n"
            f"GENERATED TSD TO AUDIT against the code:\n"
            f"{wrap_untrusted(tsd_content[:24000], 'TSD')}\n\n"
            "List only TSD claims the diff does not support. Empty list if the TSD matches the code.")
    try:
        raw = await call_llm(system=_SYSTEM, messages=[{"role": "user", "content": user}],
                             max_tokens=MAX_OUTPUT_TOKENS, agent_name="doc_code_consistency")
    except Exception as e:  # noqa: BLE001 — fail-open: never block/mutate a code run on the checker
        logger.warning("doc_code_consistency LLM call failed (%s) — treating as consistent", e)
        return _empty()

    data = await parse_llm_json(raw, fallback=None)
    if not isinstance(data, dict):
        return _empty()
    findings = []
    for f in (data.get("findings") or []):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "warning").lower()
        if sev not in ("blocker", "warning"):
            sev = "warning"
        kind = str(f.get("kind") or "")[:32]
        # Only a real CODE gap may block; everything else is a doc over-claim (warning), never a blocker.
        if kind != "code_missing":
            sev = "warning"
        findings.append({"severity": sev, "kind": kind,
                         "item": str(f.get("item") or "")[:120],
                         "detail": str(f.get("detail") or "")[:300]})
    findings = findings[:20]
    has_blocker = any(f["kind"] == "code_missing" and f["severity"] == "blocker" for f in findings)
    return {"consistent": not findings, "findings": findings, "has_blocker": has_blocker}


def doc_fabrication_findings(consistency: dict) -> list[dict]:
    """The findings where the DOC over-claims (code is right) — these drive the TSD reconcile."""
    return [f for f in (consistency.get("findings") or []) if f.get("kind") in _DOC_FABRICATION_KINDS]


def code_gap_findings(consistency: dict) -> list[dict]:
    """The ``code_missing`` findings — real code gaps the caller routes into the review/blocking path."""
    return [f for f in (consistency.get("findings") or []) if f.get("kind") == "code_missing"]


def repair_instruction(findings: list[dict]) -> str:
    """Render the doc-over-claim findings into a corrective directive for the TSD rewrite."""
    items = "; ".join(f"{f.get('item')}: {f.get('detail')}" for f in findings)[:1500]
    return (
        "DOC↔CODE RECONCILE — this TSD asserts implementation facts the ACTUAL code does NOT contain. "
        "Correct ONLY these claims so the TSD describes what the code actually does (e.g. remove an "
        "invented DB column, replace a fictional config key with the real hard-coded constant, fix "
        "error codes the code never emits). Keep every other section and all business content "
        "identical; do NOT add new flows/fields/requirements. Claims to fix: " + items)


async def reconcile_doc_to_code(*, tsd_content: str, diff_text: str, instruction: str) -> str:
    """LLM rewrite of the TSD to remove claims the code does not support. Fail-open: returns the
    original TSD unchanged on any LLM failure (caller then keeps what it had + surfaces the banner)."""
    if not (tsd_content or "").strip():
        return tsd_content
    user = (f"ACTUAL CODE CHANGE — unified diff (the source of truth):\n"
            f"{wrap_untrusted(diff_text[:40000], 'DIFF')}\n\n"
            f"{instruction}\n\n"
            f"CURRENT TSD (rewrite it to match the code):\n"
            f"{wrap_untrusted(tsd_content[:60000], 'TSD')}")
    sys = (
        "You are reconciling a TSD to the ACTUAL code that implements it. The TSD asserted "
        "implementation facts the code does not contain. Rewrite and return the COMPLETE TSD with ONLY "
        "those divergent claims corrected to match the code. Preserve every section, heading, table, "
        "and all business content verbatim wherever it does not conflict — do NOT drop sections and do "
        "NOT add new flows/fields/requirements. Output ONLY the corrected document.\n" + ANTI_INJECTION_CLAUSE
    )
    try:
        raw = await call_llm(system=sys, messages=[{"role": "user", "content": user}],
                             max_tokens=RECONCILE_MAX_TOKENS, agent_name="doc_code_consistency")
    except Exception as e:  # noqa: BLE001 — fail-open: never lose the doc on a reconcile failure
        logger.warning("reconcile_doc_to_code failed (%s) — keeping original", e)
        return tsd_content
    return (raw or "").strip() or tsd_content
