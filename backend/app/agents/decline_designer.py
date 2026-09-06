# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Decline Designer agent — authors the per-feature Decline & Timeout design.

This is where declines are *designed*, not derived from a static catalog: the
LLM enumerates how each party on the feature's flow can refuse / time out / mis-
respond, a human approves the result, and the cert engine then tests exactly the
approved set (one test per row). Production safety comes from designing — and
therefore handling — these failures up front.

Three passes (the LLM proposes; a human approves before any of it is authoritative):
  * Pass 1 (BRD)  — business declines per entity, plain language, no codes yet.
  * Critic        — adversarial "what reachable failure did you miss?" pass.
  * Pass 2 (TSD)  — map each approved row to a condition + canonical/minted code,
                    reconciled against the feature's real XSD.

Mirrors the structured-agent pattern in cert_triage.py: inline SYSTEM_PROMPT,
`call_llm` + `parse_llm_json`, per-agent model routing via `pick_model_for_agent`.
"""
import json
from app.core.domain.registry import prompt_block
from app.core.prompts import load_prompt, render_prompt
import logging

from app.core.llm import call_llm
from app.core.json_recovery import parse_llm_json
from app.core.llm_router import pick_model_for_agent
from app.agents._prompt_safety import wrap_untrusted
from app.excel_testcase_engine.schemas.decline_spec import (
    DeclineRow,
    ExcludedCandidate,
    FeatureDeclineSpec,
    MintedCode,
)

logger = logging.getLogger(__name__)

AGENT = "decline_designer"


def _default_entities() -> list[str]:
    """The every-party lens: a complete pack must exercise failures ORIGINATING
    at each party, not just the initiator's happy path. The parties are the
    ACTIVE domain's (cert-vocabulary roles plus the authority), resolved per
    call so a long-lived worker follows DOMAIN_PACK rather than pinning the
    first domain it saw. For UPI this yields the same five entities the old
    hardcoded list carried."""
    from app.core.domain.contract import cert_vocabulary_of, participants_of
    from app.core.domain.registry import get_active_pack

    pack = get_active_pack()
    labels = [label for _, label in cert_vocabulary_of(pack).parties()]
    authority = next(
        (p.label for p in participants_of(pack) if p.is_authority), None)
    if authority:
        labels.insert(min(1, len(labels)), authority)
    return labels or ["Initiator", "Counterparty"]


# ── Prompts ──────────────────────────────────────────────────────────────────

# Domain nouns come from the active pack; under the default UPI pack the
# rendered prompt carries the same party enumeration and worked example the
# file used to hardcode.
_BRD_PROMPT = render_prompt(
    "agents/decline_designer/brd_prompt.md",
    DOMAIN_LABEL=prompt_block("domain_name", "certification-scope"),
    PARTY_ENUMERATION=prompt_block(
        "decline_party_enumeration", "each party this domain's flows involve"),
    PERSPECTIVE_EXAMPLE=prompt_block(
        "decline_perspective_example",
        "A counterparty that goes silent mid-flow leaves an uncertain outcome "
        "needing reconciliation — as essential as an outright decline."),
    EXAMPLE_ROW=prompt_block(
        "decline_example_row",
        '{"api":"<primary api>","owning_entity":"<who fails>",\n'
        '     "observing_entity":"<who must handle it>","stage":"<flow stage>",'
        '"failure_type":"decline",\n'
        '     "condition":"<a declared business rule is not met>",'
        '"required_behavior":"reject; notify initiator",\n'
        '     "reachable":true,"rationale":"every request can be refused by policy"}'),
)

_CRITIC_PROMPT = load_prompt("agents/decline_designer/critic_prompt.md")

_TSD_PROMPT = load_prompt("agents/decline_designer/tsd_prompt.md")


# ── Pure helpers (LLM-free — unit-testable in isolation) ─────────────────────

def _entity_prefix(entity: str) -> str:
    """Short stable prefix from an entity name, e.g. 'Remitter Bank' -> 'RMT'."""
    words = [w for w in entity.replace("/", " ").split() if w]
    if not words:
        return "GEN"
    if len(words) == 1:
        return words[0][:3].upper()
    return (words[0][:1] + words[1][:2]).upper()


def _coerce_rows(raw_rows, default_api: str, start: int = 1) -> list[DeclineRow]:
    """Validate raw LLM row dicts into DeclineRow, filling required id/api.

    Invalid rows (bad failure_type, missing condition) are skipped rather than
    failing the whole pass — the critic and human review backstop omissions.
    """
    rows: list[DeclineRow] = []
    n = start
    for raw in raw_rows or []:
        if not isinstance(raw, dict):
            continue
        data = dict(raw)
        data.setdefault("api", default_api)
        if not data.get("id"):
            prefix = _entity_prefix(str(data.get("owning_entity", "GEN")))
            data["id"] = f"DCL-{prefix}-{n:03d}"
        try:
            rows.append(DeclineRow.model_validate(data))
        except Exception as exc:  # noqa: BLE001 — skip malformed, keep the rest
            logger.warning("decline_designer: dropped malformed row: %s", exc)
            continue
        n += 1
    return rows


def _coerce_excluded(raw) -> list[ExcludedCandidate]:
    out: list[ExcludedCandidate] = []
    for r in raw or []:
        if isinstance(r, dict) and r.get("candidate"):
            out.append(ExcludedCandidate(candidate=str(r["candidate"]), reason=str(r.get("reason", ""))))
    return out


def _row_key(r: DeclineRow) -> tuple:
    """Identity for dedup — same party failing the same way at the same stage."""
    return (r.api, r.owning_entity.lower().strip(), r.stage.lower().strip(),
            r.failure_type, r.condition.lower().strip())


def _merge_rows(base: list[DeclineRow], extra: list[DeclineRow]) -> list[DeclineRow]:
    """Append `extra` rows that aren't already present in `base` (by _row_key)."""
    seen = {_row_key(r) for r in base}
    merged = list(base)
    for r in extra:
        k = _row_key(r)
        if k not in seen:
            seen.add(k)
            merged.append(r)
    return merged


def _feature_block(feature_summary: str, apis, flow, entities) -> str:
    return (
        f"# Feature\n{wrap_untrusted(feature_summary, 'FEATURE')}\n\n"
        f"# APIs\n{json.dumps(apis, ensure_ascii=True)}\n\n"
        f"# Flow (ordered)\n{json.dumps(flow, ensure_ascii=True)}\n\n"
        f"# Entities on the wire\n{json.dumps(entities or _default_entities(), ensure_ascii=True)}\n"
    )


# ── LLM-backed passes ────────────────────────────────────────────────────────

async def run_completeness_critic(
    *, feature_summary: str, apis: list[str], flow: list,
    entities: list[str] | None, existing_rows: list[DeclineRow],
) -> list[DeclineRow]:
    """Adversarial 'what's missing' pass — returns candidate rows not yet present."""
    existing = [r.model_dump() for r in existing_rows]
    user = (
        _feature_block(feature_summary, apis, flow, entities)
        + f"\n# Declines designed so far\n{json.dumps(existing, ensure_ascii=True, default=str)}\n"
    )
    raw = await call_llm(
        system=_CRITIC_PROMPT,
        messages=[{"role": "user", "content": user}],
        max_tokens=4000,
        model=pick_model_for_agent(AGENT),
        agent_name=AGENT,
    )
    data = await parse_llm_json(raw, expect_array=False, fallback={"rows": []})
    default_api = apis[0] if apis else ""
    return _coerce_rows(data.get("rows", []), default_api, start=len(existing_rows) + 1)


async def design_declines_brd_pass(
    *, feature_id: str, feature_summary: str, apis: list[str], flow: list | None = None,
    entities: list[str] | None = None, run_critic: bool = True,
) -> FeatureDeclineSpec:
    """Pass 1 (+ optional critic) — the DRAFT business-decline design (no codes yet)."""
    flow = flow or []
    user = _feature_block(feature_summary, apis, flow, entities)
    raw = await call_llm(
        system=_BRD_PROMPT,
        messages=[{"role": "user", "content": user}],
        max_tokens=6000,
        model=pick_model_for_agent(AGENT),
        agent_name=AGENT,
    )
    data = await parse_llm_json(raw, expect_array=False, fallback={"rows": [], "excluded": []})
    default_api = apis[0] if apis else ""
    rows = _coerce_rows(data.get("rows", []), default_api)
    excluded = _coerce_excluded(data.get("excluded", []))

    if run_critic and rows:
        try:
            extra = await run_completeness_critic(
                feature_summary=feature_summary, apis=apis, flow=flow,
                entities=entities, existing_rows=rows,
            )
            rows = _merge_rows(rows, extra)
            logger.info("decline_designer: critic added %d row(s)", len(extra))
        except Exception as exc:  # noqa: BLE001 — critic is best-effort, never blocks
            logger.warning("decline_designer: critic pass failed: %s", exc)

    return FeatureDeclineSpec(feature_id=feature_id, apis=list(apis), rows=rows, excluded=excluded)


async def design_declines_tsd_pass(
    *, spec: FeatureDeclineSpec, error_catalog: dict, xsd_content: str = "",
) -> FeatureDeclineSpec:
    """Pass 2 — assign canonical/minted codes to the approved rows, XSD-reconciled."""
    rows_in = [r.model_dump() for r in spec.rows if r.reachable]
    user = (
        f"# Approved declines\n{json.dumps(rows_in, ensure_ascii=True, default=str)}\n\n"
        f"# Canonical error-code catalog\n{json.dumps(error_catalog, ensure_ascii=True)[:12000]}\n\n"
        f"# Feature XSD (real fields)\n{wrap_untrusted(xsd_content[:8000], 'XSD')}\n"
    )
    raw = await call_llm(
        system=_TSD_PROMPT,
        messages=[{"role": "user", "content": user}],
        max_tokens=8000,
        model=pick_model_for_agent(AGENT),
        agent_name=AGENT,
    )
    data = await parse_llm_json(raw, expect_array=False, fallback={"rows": rows_in, "new_codes": []})
    default_api = spec.apis[0] if spec.apis else ""
    coded = _coerce_rows(data.get("rows", []), default_api)
    minted: list[MintedCode] = []
    for c in data.get("new_codes", []) or []:
        if isinstance(c, dict) and c.get("code"):
            try:
                minted.append(MintedCode.model_validate(c))
            except Exception as exc:  # noqa: BLE001
                logger.warning("decline_designer: dropped malformed minted code: %s", exc)
    # Keep the original rows as a floor if the LLM dropped any (preserve coverage).
    # Compare against the reachable rows we actually fed in (`rows_in`): the input
    # spec is the BRD-pass output whose rows have no error_code yet, so
    # spec.reachable_rows() (which requires a code) would always be empty here.
    if len(coded) < len(rows_in):
        logger.warning("decline_designer: Pass 2 returned fewer rows than input; keeping originals for the gap")
        coded = _merge_rows(coded, [r for r in spec.rows if r.reachable])
    return FeatureDeclineSpec(
        feature_id=spec.feature_id, apis=spec.apis, rows=coded,
        excluded=spec.excluded, new_codes=minted,
    )
