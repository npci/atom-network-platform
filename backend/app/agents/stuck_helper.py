# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Stuck-run helper — when a run errors and retry/resume just re-hits the same wall, the
human can ask the LLM "what do I do?" and get 2-3 concrete recovery options (with one
recommended) drawn from a CLOSED catalog of safe actions. Includes a second LLM pass that
validates a free-text user direction before applying it — gibberish/unsafe input is bounced
back to the same options card with the textarea hidden, forcing a pick.

Family-A single-call agent (text-in → JSON-out via call_llm), fail-open: a missing/garbled
LLM response degrades to a single ``rerun_code_gen`` fallback option, never raises.
"""
from __future__ import annotations

import logging

from app.core.llm import call_llm
from app.core.json_recovery import parse_llm_json
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 1600

# Closed catalog: the ONLY action codes the helper can hand back. Each maps 1:1 to an existing
# recovery endpoint so the decide handler dispatches deterministically (the LLM never invents
# an action). "fits" guides the LLM toward the right option for a given error context.
ACTION_CATALOG: dict[str, dict] = {
    "rerun_code_gen": {
        "label": "Rerun code-gen on the current upstream",
        "fits": "stale workspace, base branch moved, want a clean retry from current main/head",
    },
    "reset_and_retry_push": {
        "label": "Reset workspace HEAD and retry push (fast path)",
        "fits": "BASE_DRIFT where ONLY the local workspace HEAD drifted (e.g. a leftover commit "
                "from a prior push) but upstream main is unchanged — clears the preflight "
                "without redoing code-gen. CAVEAT: pushes against the recorded base, so the MR "
                "may have conflicts at merge time if upstream actually moved. Prefer "
                "rerun_code_gen when in doubt.",
    },
    "retry_push": {
        "label": "Retry the push as-is",
        "fits": "a transient infra blip (rate limit, brief network failure) — NOT for base drift or auth errors",
    },
    "abandon": {
        "label": "Abandon this run",
        "fits": "the earlier push already covers the change, or you want to give up on this attempt and pick it up later",
    },
    "resume_once_more": {
        "label": "Resume from where it stopped",
        "fits": "ONLY when the failure was a one-off the system has since recovered from — not when retry would re-hit the same error",
    },
}


def _fallback(summary: str = "I couldn't analyse the failure — try rerunning code-gen on the current upstream.") -> dict:
    """Static fallback so the UI always has SOMETHING to show on an LLM/parse failure."""
    return {
        "summary": summary,
        "options": [{"id": "fallback-rerun", "action_code": "rerun_code_gen",
                     "title": "Rerun code-gen on the current upstream",
                     "why": "Generic safe recovery — a fresh workspace clears stale state from most failures.",
                     "tradeoffs": "Discards this run's work and starts Phase B again."}],
        "recommended": "fallback-rerun",
    }


def _catalog_block() -> str:
    return "\n".join(f"- {code}: {a['label']} — appropriate when: {a['fits']}"
                     for code, a in ACTION_CATALOG.items())


_OPTIONS_PROMPT = (
    "You help a human recover a stuck agentic code-gen run. Given the error context below, "
    "propose 2-3 recovery OPTIONS the human can pick — drawn ONLY from the closed action "
    "catalog. Mark exactly one option as recommended.\n\n"
    "STRICT RULES: (1) Every option's `action_code` MUST be one of the catalog codes verbatim "
    "— never invent. (2) Do not propose actions that would obviously re-hit the same error "
    "(e.g. retry_push on BASE_DRIFT will fail the same preflight again — exclude it). (3) Plain "
    "language a non-developer understands. (4) Keep `why` to one sentence pointing at THIS "
    "specific error; keep `tradeoffs` honest.\n\n"
    "Respond with ONLY a JSON object:\n"
    "{\n"
    '  "summary": "1-2 plain sentences: what went wrong and why retry/resume alone may not fix it",\n'
    '  "options": [{"id": "<short id>", "action_code": "<catalog code>", "title": "<plain title>", '
    '"why": "<1 sentence>", "tradeoffs": "<1 sentence>"}],\n'
    '  "recommended": "<id of the recommended option>"\n'
    "}\n"
    + ANTI_INJECTION_CLAUSE
)


_VALIDATOR_PROMPT = (
    "A human typed a free-text direction for recovering a stuck run. Classify it:\n"
    "- SAFE_AND_CLEAR: it cleanly maps to ONE of the available catalog actions and is safe.\n"
    "- UNCLEAR: gibberish, ambiguous, or doesn't map to any catalog action.\n"
    "- UNSAFE: would damage state or bypass safety (force-pushing main, destroying work, "
    "skipping review, etc.).\n\n"
    "Respond with ONLY a JSON object:\n"
    "{\n"
    '  "verdict": "SAFE_AND_CLEAR | UNCLEAR | UNSAFE",\n'
    '  "maps_to": "<catalog action code> | null",\n'
    '  "why": "<one short sentence>"\n'
    "}\n"
    + ANTI_INJECTION_CLAUSE
)


def _error_context(run, recent_events: list | None) -> str:
    parts = [f"phase: {getattr(run, 'phase', '?')}",
             f"status: {getattr(run, 'status', '?')}",
             f"error_code: {getattr(run, 'error_code', None) or '(none)'}",
             f"error: {(getattr(run, 'error', None) or '(none)')[:600]}"]
    if recent_events:
        evs = ["  " + (e.get("kind") or "?") + ": "
               + str((e.get("payload") or {}).get("action") or
                     (e.get("payload") or {}).get("reasons") or "")[:160]
               for e in recent_events[-6:]]
        parts.append("recent events (oldest→newest):\n" + "\n".join(evs))
    return "\n".join(parts)


async def propose_recovery(*, run, recent_events: list | None = None) -> dict:
    """Ask the LLM for 2-3 recovery options for this stuck run. Fail-open: a parse/LLM failure
    returns the static fallback (a single rerun_code_gen option), never raises."""
    ctx = _error_context(run, recent_events)
    user = (f"Available actions (catalog — choose from these ONLY):\n{_catalog_block()}\n\n"
            f"Error context for this stuck run:\n{wrap_untrusted(ctx, 'STUCK_RUN_CONTEXT')}\n\n"
            "Propose recovery options now.")
    try:
        raw = await call_llm(system=_OPTIONS_PROMPT,
                             messages=[{"role": "user", "content": user}],
                             max_tokens=MAX_OUTPUT_TOKENS, agent_name="stuck_helper")
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("stuck_helper LLM call failed: %s", e)
        return _fallback()

    data = await parse_llm_json(raw, fallback=None)
    if not isinstance(data, dict):
        return _fallback()
    # Normalise + DEFENSIVELY filter to the closed catalog so an off-script LLM response can't
    # smuggle an unknown action through to the dispatcher (the endpoint validates again, but
    # filtering here keeps the UI from showing options that would 400 on submit).
    opts = []
    for o in (data.get("options") or []):
        if not isinstance(o, dict):
            continue
        code = o.get("action_code")
        if code not in ACTION_CATALOG:
            continue
        opts.append({"id": str(o.get("id") or code)[:40],
                     "action_code": code,
                     "title": str(o.get("title") or ACTION_CATALOG[code]["label"])[:120],
                     "why": str(o.get("why") or "")[:300],
                     "tradeoffs": str(o.get("tradeoffs") or "")[:300]})
    if not opts:
        return _fallback()
    rec = data.get("recommended") if any(o["id"] == data.get("recommended") for o in opts) else opts[0]["id"]
    return {"summary": str(data.get("summary") or "")[:600], "options": opts[:3], "recommended": rec}


async def validate_custom_direction(*, run, recent_events: list | None, custom_direction: str) -> dict:
    """Classify a human's free-text recovery direction against the catalog. Returns
    ``{verdict, maps_to, why}`` — UNCLEAR on any LLM/parse failure (caller hides the textbox)."""
    ctx = _error_context(run, recent_events)
    user = (f"Available actions:\n{_catalog_block()}\n\n"
            f"Error context:\n{wrap_untrusted(ctx, 'STUCK_RUN_CONTEXT')}\n\n"
            f"User's free-text direction:\n{wrap_untrusted(custom_direction[:1000], 'USER_DIRECTION')}\n\n"
            "Classify it now.")
    try:
        raw = await call_llm(system=_VALIDATOR_PROMPT,
                             messages=[{"role": "user", "content": user}],
                             max_tokens=400, agent_name="stuck_helper_validator")
    except Exception as e:  # noqa: BLE001 — fail closed (UNCLEAR) so the human picks from options
        logger.warning("stuck_helper_validator LLM call failed: %s", e)
        return {"verdict": "UNCLEAR", "maps_to": None, "why": "Validator unavailable; please pick an option."}

    data = await parse_llm_json(raw, fallback=None)
    if not isinstance(data, dict):
        return {"verdict": "UNCLEAR", "maps_to": None, "why": "Couldn't parse validator response."}
    verdict = str(data.get("verdict") or "").upper()
    if verdict not in ("SAFE_AND_CLEAR", "UNCLEAR", "UNSAFE"):
        verdict = "UNCLEAR"
    maps_to = data.get("maps_to")
    if maps_to not in ACTION_CATALOG:
        maps_to = None
    # A SAFE verdict without a known mapping is incoherent — downgrade to UNCLEAR.
    if verdict == "SAFE_AND_CLEAR" and not maps_to:
        verdict = "UNCLEAR"
    return {"verdict": verdict, "maps_to": maps_to, "why": str(data.get("why") or "")[:300]}
