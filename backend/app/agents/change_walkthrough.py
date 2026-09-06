# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Change Walkthrough agent — turns an implemented change into a plain-language flow
for DEVELOPERS and TESTERS (THE BOOK §17 support).

Given the change intent + the ratified plan + the actual diff, it produces a structured,
simple-language walkthrough: what the API does now, the step-by-step flow, the decision/
decline logic, and concrete tester scenarios (which double as a downloadable QA sheet).

Strictly grounded in the DIFF — it describes only what the code actually does; it must not
invent endpoints, fields, or behaviour that aren't in the change. Family-A single-call agent
(text-in → JSON-out via call_llm), fail-open like cert_triage / build_triager.
"""
import json
import logging

from app.core.llm import call_llm
from app.core.json_recovery import parse_llm_json
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

# Generous: a full walkthrough (flow + scenarios + caveats) for a multi-file change is large,
# and AiNxt strips finish_reason so truncation isn't detectable — size to the real output (§3.6).
MAX_OUTPUT_TOKENS = 6000

SYSTEM_PROMPT = (
    "You are a senior technical writer and QA lead for the network platform. You are given an "
    "implemented code change (its intent, the approved plan, and the actual diff). Produce a "
    "PLAIN-LANGUAGE walkthrough that a developer AND a tester can both follow — simple, concrete, "
    "no jargon dumps.\n\n"
    "GROUND EVERYTHING IN THE DIFF. Describe only what the code actually does. Do NOT invent "
    "endpoints, fields, error codes, or behaviour that are not in the change. If something is "
    "unclear from the diff, say so rather than guessing. Use the real names from the code.\n\n"
    "Respond with ONLY a JSON object with these keys:\n"
    "{\n"
    '  "title": "short title of the change",\n'
    '  "summary": "1-3 plain sentences: what changes for a caller of the API, and what stays the same",\n'
    '  "api_surface": "which API(s) change and HOW — new request fields, new response codes/shape; '
    'note if there is no new endpoint (the change rides an existing flow)",\n'
    '  "flow": ["ordered, plain-language steps of what happens at runtime when the change is exercised"],\n'
    '  "decision_points": [{"code": "the branch/decline code if any (else short label)", "when": "the '
    'condition in plain words", "result": "what the caller gets"}],\n'
    '  "tester_scenarios": [{"id": 1, "scenario": "short name", "input": "what to send", "expected": '
    '"what should happen"}],\n'
    '  "caveats": ["honest heads-ups a tester needs from the code: config that must be on, partial '
    'behaviour, edge cases — only if visible in the diff"]\n'
    "}\n"
    "tester_scenarios MUST be concrete and runnable (a happy path, each decline/branch, and a "
    "regression that the change does not affect). Keep every field short and readable.\n\n"
    + ANTI_INJECTION_CLAUSE
)


def _empty(title: str = "Change walkthrough") -> dict:
    return {"title": title, "summary": "", "api_surface": "", "flow": [],
            "decision_points": [], "tester_scenarios": [], "caveats": []}


async def generate_walkthrough(*, intent: str, plan_text: str = "", diff_text: str = "",
                               title: str = "Change walkthrough") -> dict:
    """Produce the dev+tester walkthrough dict from the change's intent + plan + diff.
    Fail-open: returns an empty-shaped dict on any LLM/parse failure (never raises)."""
    if not (diff_text or "").strip():
        return {**_empty(title), "summary": "No code changes were found for this run."}

    user = (
        f"Change intent:\n{wrap_untrusted(intent or '(none)', 'INTENT')}\n\n"
        f"Approved plan (reference):\n{wrap_untrusted((plan_text or '(none)')[:8000], 'PLAN')}\n\n"
        f"Actual diff (the source of truth — describe ONLY what this changes):\n"
        f"{wrap_untrusted(diff_text[:40000], 'DIFF')}"
    )
    logger.info("change_walkthrough — generating (diff %d chars)", len(diff_text))
    try:
        raw = await call_llm(system=SYSTEM_PROMPT,
                             messages=[{"role": "user", "content": user}],
                             max_tokens=MAX_OUTPUT_TOKENS, agent_name="change_walkthrough")
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("change_walkthrough LLM call failed: %s", e)
        return _empty(title)

    data = await parse_llm_json(raw, fallback=_empty(title))
    if not isinstance(data, dict):
        return _empty(title)
    # normalise shape so the API/UI/CSV never KeyError
    out = _empty(title)
    out.update({k: data.get(k, out[k]) for k in out})
    if not isinstance(out["flow"], list):
        out["flow"] = []
    out["tester_scenarios"] = [s for s in (out.get("tester_scenarios") or []) if isinstance(s, dict)]
    out["decision_points"] = [d for d in (out.get("decision_points") or []) if isinstance(d, dict)]
    out["caveats"] = [c for c in (out.get("caveats") or []) if isinstance(c, str)]
    logger.info("change_walkthrough — produced %d flow steps, %d scenarios",
                len(out["flow"]), len(out["tester_scenarios"]))
    return out


def _csv_safe(value) -> str:
    """Neutralize spreadsheet formula injection (CWE-1236) in one cell.

    Excel/Sheets/Calc treat a cell opening with `= + - @` — or with a leading
    tab/CR, which those clients strip before parsing — as a formula, not text.
    The scenario fields here are LLM output derived from the change's diff and
    plan text, so an attacker who controls a commit message, code comment or
    PR description can steer what lands in a cell. `=HYPERLINK(...)` then
    exfiltrates on open, and DDE payloads (`=cmd|'/c calc'!A1`) can execute on
    unpatched clients.

    A leading apostrophe is the portable fix: spreadsheets read it as
    "the rest is literal text" and don't render it in the cell.
    """
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def scenarios_to_csv(walkthrough: dict) -> str:
    """Render tester_scenarios as a QA-sheet CSV (id, scenario, input, expected)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["#", "Scenario", "Input / what to send", "Expected result"])
    for i, s in enumerate(walkthrough.get("tester_scenarios") or [], start=1):
        w.writerow([_csv_safe(s.get("id", i)), _csv_safe(s.get("scenario", "")),
                    _csv_safe(s.get("input", "")), _csv_safe(s.get("expected", ""))])
    return buf.getvalue()
