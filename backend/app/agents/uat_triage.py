# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""UAT triage agent — AI triage of the Phase B Build + UAT logs (Triage step).

Given the build+deploy log and the UAT test script's log for one change, one
structured LLM call classifies each distinct failure (code_bug /
test_case_issue / env_issue — the existing ``TriageVerdict`` vocabulary) with
quoted evidence and a remediation, plus an overall verdict and next action.
Family-A single-call agent (text-in → JSON-out via call_llm), fail-open like
cert_triage / build_triager: an LLM or parse failure still returns a
deterministic summary derived from the recorded counts, never an exception.

Logs are untrusted input (a build script echoes whatever the repo contains),
so they enter the prompt inside the untrusted-data envelope and the system
prompt carries the shared anti-injection clause.
"""
from __future__ import annotations

import logging

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.json_recovery import parse_llm_json
from app.core.llm import call_llm
from app.core.prompts import load_prompt

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = load_prompt("agents/uat_triage/system_prompt.md")

# A failing suite's decisive lines cluster at the END of a log; keep a small
# head for context (what ran, versions) and a large tail. Two logs at this cap
# stay well inside the context window.
_HEAD_CHARS = 2_000
_TAIL_CHARS = 18_000

MAX_OUTPUT_TOKENS = 4000   # matches is_review / cert_triage chat agents

_VALID_CLASS = {"code_bug", "test_case_issue", "env_issue"}
_VALID_SOURCE = {"build", "test", "environment"}
_VALID_NEXT = {"proceed", "fix_code", "fix_tests", "fix_env"}


def _slice_log(log: str | None) -> str:
    text = (log or "").strip()
    if not text:
        return "(no log recorded)"
    if len(text) <= _HEAD_CHARS + _TAIL_CHARS:
        return text
    return (text[:_HEAD_CHARS] + "\n…[middle of log omitted]…\n" + text[-_TAIL_CHARS:])


def _fallback(counts: dict, build_failed: bool, note: str) -> dict:
    """Deterministic result when the LLM layer is unavailable — honest, minimal."""
    failed = int(counts.get("failed") or 0)
    issues = build_failed or failed > 0
    bits = []
    if build_failed:
        bits.append("the build+deploy run is recorded as FAILED")
    if counts:
        bits.append(f"UAT counts: {counts.get('passed', 0)} passed, {failed} failed "
                    f"of {counts.get('total', 0)}")
    return {
        "overall": "issues_found" if issues else "pass",
        "summary": (f"AI triage unavailable ({note}) — deterministic summary only: "
                    + ("; ".join(bits) or "no build or test evidence recorded") + "."),
        "findings": [],
        "next_action": "fix_code" if issues else "proceed",
        "ai": False,
    }


def _normalise(data: dict, counts: dict, build_failed: bool) -> dict:
    """Clamp the model's answer to the schema so the API/UI never KeyErrors,
    and keep `overall` honest against the recorded outcome."""
    out = {
        "overall": data.get("overall"),
        "summary": str(data.get("summary") or "")[:2000],
        "findings": [],
        "next_action": data.get("next_action"),
        "ai": True,
    }
    for f in (data.get("findings") or []):
        if not isinstance(f, dict):
            continue
        cls = f.get("classification")
        out["findings"].append({
            "source": f.get("source") if f.get("source") in _VALID_SOURCE else "test",
            "test_id": str(f.get("test_id") or "")[:100],
            "classification": cls if cls in _VALID_CLASS else "env_issue",
            "evidence": str(f.get("evidence") or "")[:1500],
            "reasoning": str(f.get("reasoning") or "")[:1000],
            "remediation": str(f.get("remediation") or "")[:1000],
        })
    if out["overall"] not in ("pass", "issues_found"):
        out["overall"] = "issues_found" if (out["findings"] or build_failed
                                            or int(counts.get("failed") or 0) > 0) else "pass"
    # A recorded failure can never read as an all-clear, whatever the model said.
    if (build_failed or int(counts.get("failed") or 0) > 0) and out["overall"] == "pass":
        out["overall"] = "issues_found"
    if out["next_action"] not in _VALID_NEXT:
        out["next_action"] = "proceed" if out["overall"] == "pass" else "fix_code"
    return out


async def triage_from_logs(
    *,
    change_title: str,
    build_log: str | None,
    build_failed: bool,
    test_log: str | None,
    counts: dict | None = None,
) -> dict:
    """AI-triage the change's build + UAT logs. Never raises (fail-open)."""
    counts = counts or {}
    stats = (f"recorded outcome — build_failed={build_failed}, "
             f"tests: total={counts.get('total', '?')} passed={counts.get('passed', '?')} "
             f"failed={counts.get('failed', '?')} skipped={counts.get('skipped', '?')}")
    user = (
        f"Change: {change_title or '(untitled)'}\n{stats}\n\n"
        f"Build + Deploy log:\n{wrap_untrusted(_slice_log(build_log), 'BUILD_AND_DEPLOY_LOG')}\n\n"
        f"UAT test log:\n{wrap_untrusted(_slice_log(test_log), 'UAT_TEST_LOG')}"
    )
    logger.info("uat_triage — triaging logs (build=%d chars, test=%d chars, failed=%s)",
                len(build_log or ""), len(test_log or ""), counts.get("failed"))
    try:
        raw = await call_llm(
            system=SYSTEM_PROMPT + "\n\n" + ANTI_INJECTION_CLAUSE,
            messages=[{"role": "user", "content": user}],
            max_tokens=MAX_OUTPUT_TOKENS,
            agent_name="uat_triage",
        )
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("uat_triage LLM call failed: %s", e)
        return _fallback(counts, build_failed, "model call failed")

    data = await parse_llm_json(raw, fallback=None)
    if not isinstance(data, dict):
        logger.warning("uat_triage response did not parse as an object")
        return _fallback(counts, build_failed, "response parsing failed")
    out = _normalise(data, counts, build_failed)
    logger.info("uat_triage — %s, %d finding(s), next=%s",
                out["overall"], len(out["findings"]), out["next_action"])
    return out
