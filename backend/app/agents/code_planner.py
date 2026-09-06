# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code planner — structured-output generator (Slice 12).

Given a Tech Spec + BRD, makes a single LLM call and returns a structured
CodePlan dict validated against `code_plan_schema`. Parallel path to the
existing `code_change.py` streaming agent (which still produces full-file
rewrites) — wiring the planner as a pre-step in the editor flow is a
follow-up slice. For now the planner runs on-demand and its output
persists to the new `code_plans` table via a future service helper.

Fail-open: any LLM error or unparseable response returns `{}`. Callers
check `code_plan_schema.validate(result)["schema_valid"]` before acting.
"""
from __future__ import annotations

import logging

from app.agents.code_plan_schema import ALL_PLAN_KEYS
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 3000


_SYSTEM_PROMPT = """You are the Code Planner for the Network Change
Management Platform. Given a Technical Specification and the prior BRD,
produce a STRUCTURED code-change plan that a downstream editor agent can
execute mechanically.

Return ONLY a JSON object with EXACTLY this shape:

{
  "files": [
    {
      "path":               "src/ratelimit/TieredRateLimiter.java",
      "action":             "create",          // or "modify"
      "intent":             "Brief sentence (≥12 chars) explaining WHY this change",
      "repo":               "common-infra",    // optional
      "signatures_to_add":  ["public class TieredRateLimiter extends RateLimiter",
                             "public boolean acquire(TenantContext ctx, int permits)"],
      "callers_impacted":   ["PaymentRetryController.retry"]   // optional
    }
  ],
  "tests": [
    {
      "path":   "test/ratelimit/TieredRateLimiterTest.java",
      "action": "create",
      "cases":  ["enterprise_under_limit_allowed",
                 "enterprise_at_limit_returns_429",
                 "non_enterprise_unlimited"]
    }
  ],
  "notes": "Any cross-cutting concerns (rollout, feature flags, etc.)"
}

Rules:
- Return ONLY the JSON. No markdown fences, no prose, no preamble.
- `files` MUST have ≥1 entry; use clear `action` (create or modify) per file.
- `intent` should be one sentence — WHY the change, not HOW.
- Prefer specific signatures in `signatures_to_add` — method/class signatures
  as they would appear in the final code. Omit if uncertain rather than guess.
- `tests` is optional but strongly recommended — list case IDs (snake_case);
  downstream test_gen agent elaborates them into actual test bodies.
- Never invent files outside the stated repo structure. When uncertain,
  omit `repo`.
- Be language-agnostic where possible (Java is our primary target today).

""" + ANTI_INJECTION_CLAUSE


async def generate_plan(tech_spec: str, brd: str = "") -> dict:
    """Produce a CodePlan dict from a Tech Spec (+ optional BRD).

    Args:
        tech_spec: Full text of the approved Technical Specification.
        brd:       Optional BRD text (truncated to keep tokens bounded).

    Returns:
        Dict matching the CodePlan schema. Empty dict on failure.
    """
    if not tech_spec or not tech_spec.strip():
        return {}

    brd_block = ""
    if brd and brd.strip():
        brd_snippet = brd[:4000] + ("..." if len(brd) > 4000 else "")
        brd_block = f"\n\nAPPROVED BRD (for context):\n{wrap_untrusted(brd_snippet, 'BRD')}\n"

    user_payload = f"TECH SPEC:\n{wrap_untrusted(tech_spec.strip(), 'TECH_SPEC')}{brd_block}"

    try:
        from app.core.llm import call_llm
        from app.core.json_recovery import parse_llm_json

        raw = await call_llm(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_payload}],
            max_tokens=MAX_OUTPUT_TOKENS,
            agent_name="code_planner",
        )
    except Exception as e:
        logger.warning("code_planner.generate_plan: LLM call failed: %s", e)
        return {}

    parsed = await parse_llm_json(raw, fallback=None, llm_self_correct=False)
    if not isinstance(parsed, dict):
        logger.warning("code_planner.generate_plan: LLM returned non-dict; discarding")
        return {}

    # Keep only expected top-level keys (drop hallucinated extras).
    return {k: v for k, v in parsed.items() if k in ALL_PLAN_KEYS}
