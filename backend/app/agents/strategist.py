# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""One-shot structural strategist (P2, grok-build parity).

After repeated failed verify/review fix rounds, another "here are the errors,
fix them" round tends to whack-a-mole: each patch mints the next failure while
the underlying approach stays wrong. Grok Build's goal harness runs a separate
*strategist* prompt at that point whose only job is to recommend ONE structural
change of approach. This is that stage: a single bounded LLM call over the
failure history — no tools, no loop — whose output the orchestrator attaches to
the next round's feedback (rendered as a binding "change approach" directive by
`_feedback_block`).

Fail-open by design: any error returns "" and the fix round proceeds exactly as
before — the strategist can only ever ADD information.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import logging

logger = logging.getLogger("app.agentic")

_SYSTEM = load_prompt("agents/strategist/system.md")


async def structural_advice(*, plan_summary: str, attempts: int,
                            error_history: list, diff_stat: str = "") -> str:
    """One structural recommendation for a run stuck at ``attempts`` failed rounds.
    Returns "" on any failure (fail-open)."""
    try:
        from app.core.llm import call_llm
        hist_lines = []
        for h in (error_history or [])[-5:]:
            if isinstance(h, dict):
                hist_lines.append("- " + "; ".join(str(e) for e in (h.get("errors") or [])[:3]))
            else:
                hist_lines.append("- " + str(h)[:300])
        prompt = (
            f"A coding agent has failed {attempts} consecutive fix rounds on this change.\n\n"
            f"THE PLAN (what the change must deliver):\n{(plan_summary or '(none)')[:2000]}\n\n"
            f"FAILURE SIGNATURES, oldest→newest (each round's key errors):\n"
            + ("\n".join(hist_lines) or "(no parsed history)") + "\n\n"
            + (f"CURRENT DIFF SHAPE:\n{diff_stat[:800]}\n\n" if diff_stat else "")
            + "What ONE structural change of approach should the next round make?"
        )
        advice = (await call_llm(_SYSTEM, [{"role": "user", "content": prompt}],
                                 max_tokens=400, agent_name="strategist") or "").strip()
        if advice:
            logger.info("strategist: advice after %d failed attempts (%d chars)", attempts, len(advice))
        return advice
    except Exception as e:  # noqa: BLE001 — advisory only; never block the fix round
        logger.warning("strategist skipped (%s)", e)
        return ""
