# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Adversarial code reviewer (Slice 13).

Plan §7.5 — a second LLM pass with a deliberately strict, non-agreeable system
prompt. Runs AFTER the primary `code_review.py` agent (which checks
Sonar/PMD-style rules); this agent looks for what the first reviewer missed —
edge cases, race conditions, security gotchas, unstated assumptions.

Pure-ish:
  - One LLM call (`call_llm`) returning a JSON array of findings.
  - A genuinely-empty verdict is `[]`; a LOST verdict (LLM exception, non-JSON,
    non-list response) returns a SENTINEL finding — a missing review must never
    be indistinguishable from a clean one (mirrors agentic_review.parse_findings).

STATUS (2026-08-04): UNWIRED again at operator request (it doubled Phase-B review
LLM spend). ``review_adversarially`` has NO callers. If re-wired, the fail-open
path already returns a review-gap SENTINEL (a LOST review is never indistinguishable
from a CLEAN one — see below); wire it in ``trigger_code_review`` gated to a clean
primary review, and treat critical/high findings + the sentinel as CLEAN-blockers.
"""
from __future__ import annotations

import logging

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 2000
DEFAULT_MAX_FINDINGS = 10

_ALLOWED_SEVERITIES = frozenset({"critical", "high", "medium", "low"})


def _review_gap_finding(reason: str) -> list[dict]:
    """Sentinel for a LOST verdict — the review did not happen, which must never
    read as clean. `review_gap` lets the caller route it to the CLEAN gate
    without sending it to the code agent as if it were a code defect."""
    return [{
        "severity":   "high",
        "category":   "other",
        "issue":      f"Adversarial review produced no verdict ({reason}) — a MISSING review, not a clean one.",
        "suggestion": "Re-run the code review, or review the diff manually before approving.",
        "review_gap": True,
    }]
_ALLOWED_CATEGORIES = frozenset({
    "correctness", "security", "concurrency", "performance",
    "error_handling", "resource_leak", "edge_case", "missing_test",
    "convention", "other",
})


_SYSTEM_PROMPT = """You are an ADVERSARIAL code reviewer at a payments
infrastructure company. Your job is to find what the primary reviewer
missed. You are NOT agreeable; you are paid to be skeptical.

Focus ranges (in order):
1. Correctness bugs — off-by-ones, wrong operators, missed branches.
2. Security — injection, deserialization, auth bypass, PII leakage.
3. Concurrency — races, missing synchronisation, dirty reads.
4. Error handling — swallowed exceptions, generic catches, missing retries.
5. Resource leaks — unclosed connections, file handles, listeners.
6. Edge cases — null, empty, boundary values, unicode, timezones.
7. Missing tests — untested branches, missing negative cases.

Return ONLY a JSON array of findings. No prose, no markdown fences.
Each finding:

[
  {
    "severity":   "critical" | "high" | "medium" | "low",
    "category":   "correctness" | "security" | "concurrency" | "performance"
                  | "error_handling" | "resource_leak" | "edge_case"
                  | "missing_test" | "convention" | "other",
    "file":       "src/path/File.java",  // optional; omit if not file-specific
    "issue":      "One sentence description of the problem.",
    "suggestion": "One sentence recommendation."
  }
]

Rules:
- Return an empty array `[]` if genuinely nothing concerning.
- Never pad with filler like "code looks clean". Either a concrete issue or silence.
- Do NOT repeat issues the primary reviewer already caught (if primary findings
  are provided in the prompt).
- Keep `issue` and `suggestion` to one sentence each. No explanations of obvious
  things.
- Cap at """ + str(DEFAULT_MAX_FINDINGS) + """ findings — prioritise highest severity.

""" + ANTI_INJECTION_CLAUSE


async def review_adversarially(
    code: str,
    *,
    code_plan: dict | None = None,
    primary_findings: list[dict] | None = None,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> list[dict]:
    """Run the strict-reviewer pass. Returns findings list or [] on failure.

    Args:
        code: The full generated code or diff to review.
        code_plan: Optional CodePlan dict (Slice 12) — informs the reviewer
                   about intended scope so they can flag off-scope changes.
        primary_findings: Optional list of findings from the primary
                          `code_review.py` pass — adversarial reviewer is told
                          not to repeat them.
        max_findings: Soft cap passed to the LLM.

    Returns:
        List of finding dicts `{severity, category, file?, issue, suggestion}`.
        Malformed items are filtered out; the list is truncated to
        `max_findings`. Empty on any failure (fail-open).
    """
    if not code or not code.strip():
        return []

    def _mark(s: str, cap: int, what: str) -> str:
        # LOUD clip — the reviewer must know when later files are invisible to it, or it
        # verdicts "reviewed" on content it never saw.
        return s if len(s) <= cap else (
            s[:cap] + f"\n… [⚠ {what} CLIPPED — {len(s) - cap} of {len(s)} chars omitted; "
                      "content past this point was NOT reviewed]")

    context_bits: list[str] = []
    if code_plan and isinstance(code_plan, dict) and code_plan.get("files"):
        import json as _json
        context_bits.append(
            wrap_untrusted(_mark(_json.dumps(code_plan, indent=2), 3000, "CODE_PLAN"), "CODE_PLAN")
        )
    if primary_findings:
        import json as _json
        context_bits.append(
            wrap_untrusted(_mark(_json.dumps(primary_findings, indent=2), 2000, "PRIMARY_FINDINGS"),
                           "PRIMARY_FINDINGS")
        )

    context_block = "\n\n".join(context_bits)
    sep = "\n\n---\n\n" if context_block else ""
    user_payload = f"{context_block}{sep}{wrap_untrusted(_mark(code, 60000, 'CODE'), 'CODE_UNDER_REVIEW')}"

    try:
        from app.core.llm import call_llm
        from app.core.json_recovery import parse_llm_json
        from app.core.config import settings

        # Route the reviewer to a DIFFERENT model than the author when configured (the same
        # model self-reviewing has a documented blind spot). Empty override → None → normal
        # routing (unchanged from today).
        raw = await call_llm(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_payload}],
            max_tokens=MAX_OUTPUT_TOKENS,
            model=(settings.adversarial_reviewer_model or None),
            agent_name="adversarial",
        )
    except Exception as e:
        logger.warning("adversarial_reviewer: LLM call failed: %s", e)
        return _review_gap_finding("LLM call failed")

    parsed = await parse_llm_json(raw, expect_array=True, fallback=None, llm_self_correct=False)
    if not isinstance(parsed, list):
        logger.warning("adversarial_reviewer: LLM returned non-list; sentinel verdict")
        return _review_gap_finding("unparseable reviewer output")

    clean: list[dict] = []
    for item in parsed[:max_findings]:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "")).lower().strip()
        category = str(item.get("category", "")).lower().strip()
        issue = str(item.get("issue") or "").strip()
        suggestion = str(item.get("suggestion") or "").strip()
        if not issue or not suggestion:
            continue
        if severity not in _ALLOWED_SEVERITIES:
            severity = "medium"  # normalise unknown severities
        if category not in _ALLOWED_CATEGORIES:
            category = "other"
        entry = {
            "severity":   severity,
            "category":   category,
            "issue":      issue,
            "suggestion": suggestion,
        }
        if "file" in item and isinstance(item["file"], str) and item["file"].strip():
            entry["file"] = item["file"].strip()
        clean.append(entry)

    return clean
