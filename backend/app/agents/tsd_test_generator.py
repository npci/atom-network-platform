# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""TSD-derived test generation (SDLC review gap 10).

Replaces the mocked UAT path's methodology gap, not its execution
infrastructure: the old ``uat_mock.py`` (since removed — the UAT step now
runs an operator script, see ``services/uat_script.py``) shipped 20
hardcoded UPI SmartEscrow cases, all unconditionally marked PASS with
randomised fake latency, NOT generated from the change or the TSD at all.

This module is the extraction half of that replacement: given an APPROVED
TSD's content, extract every CONCRETE, checkable assertion it makes (API
contracts, error codes, state transitions, validation rules, config
behaviour) via one structured LLM call — the same "text-in -> JSON-out,
fail-open" shape ``acceptance_predicates.extract_predicates`` already uses
for plan predicates.

The coverage side lives in :func:`assertion_coverage` — given the set of
extracted TSD assertions and the actual generated test files (as unified
diff text, or a list of test source contents), it computes what fraction
of assertions are REFERENCED by a test via a `# tsd-ref: <section>` /
`// tsd-ref: <section>` marker convention. This is wired into the
orchestrator's post-codegen gate section (``agentic_orchestrator.py``, same
place as ``contract_gate``/``cross_module_gate``) as a shadow-first
``tsd_test_coverage_gate`` check — a behavioural change whose test coverage
of the approved TSD's assertions is below a configurable threshold is
measured (and, once ``agentic_tsd_test_coverage_gate_enforce`` is flipped,
blocked) — closing the review's own complaint that "the feature test gate
is satisfied by the existence of any test file, regardless of what it
tests." Kept separate from the synchronous ``verification_plan.
feature_test_gate`` (which stays a fast, LLM-free "a test file exists"
check) because assertion extraction is an LLM call and that gate's call
site is synchronous.

**Authoring half (added 2026-08-25 — closes SDLC-A22).**
:func:`assertions_block` renders the extracted assertions INTO the
code-generation prompt. This was the missing link, and its absence made
the coverage gate structurally unenforceable:

  * `agentic_subagents._tests_clause()` already told the agent to add a
    ``// tsd-ref: <section>`` comment above each TSD-derived test.
  * But the agent was never shown WHICH assertions the extractor had
    found, so it had to invent section names — and a marker only counts
    as coverage when it matches an assertion's ``id`` or ``tsd_section``
    exactly. The agent was being graded against a list it could not see.

So coverage stayed near zero for reasons that had nothing to do with
test quality, and ``agentic_tsd_test_coverage_gate_enforce`` could never
responsibly be turned on. Two changes fix that:

  1. :func:`assertions_block` gives the agent the numbered assertion list
     with the EXACT marker string to copy for each one.
  2. :func:`referenced_tsd_refs` / :func:`assertion_coverage` now match
     markers through :func:`_normalise_ref`, so trivial formatting
     differences (case, surrounding punctuation, a ``§``/``#`` prefix,
     collapsed whitespace) do not read as a miss. Exact-string matching
     turned a cosmetic difference into a false coverage gap, which would
     have made an enforcing gate punish correct work.

Execution infrastructure is a separate concern and is untouched here —
this closes the methodology gap the review names, not the execution one
(now the script-based UAT step in ``services/uat_script.py``).
"""
from __future__ import annotations

import logging
import re

from app.core.domain.registry import prompt_block
from app.core.prompts import render_prompt

logger = logging.getLogger("app.agentic")

# Identity nouns come from the active domain pack; under the default UPI pack
# this renders byte-identically to the previous hardcoded file.
_EXTRACT_SYSTEM = render_prompt(
    "agents/tsd_test_assertions/extract_system.md",
    AUTHORITY=prompt_block("authority", "ecosystem"),
    DOMAIN_LABEL=prompt_block("domain_name", "platform"),
)

_ASSERTION_KINDS = frozenset({
    "api_contract", "error_code", "state_transition", "validation_rule", "config_behavior",
})

# `# tsd-ref: <section>` / `// tsd-ref: <section>` / `-- tsd-ref: <section>` — comment-prefix
# agnostic so Java/Python/JS/SQL test sources can all carry the marker.
_TSD_REF_RE = re.compile(r'(?:#|//|--|\*)\s*tsd-ref\s*:\s*(.+?)\s*$', re.I | re.M)


async def extract_tsd_assertions(tsd_content: str, *, max_assertions: int = 25) -> list[dict]:
    """LLM: APPROVED TSD content -> structured, checkable assertions. Fail-open -> []
    on any LLM/parse failure or empty input, matching acceptance_predicates.extract_predicates's
    house pattern exactly (never let a checker failure become a false blocker)."""
    if not (tsd_content or "").strip():
        return []
    from app.core.llm import call_llm
    from app.core.json_recovery import parse_llm_json
    from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE
    user = (f"APPROVED TSD:\n{wrap_untrusted(tsd_content[:24000], 'TSD')}\n\n"
            "Emit the testable assertions this TSD makes.")
    try:
        raw = await call_llm(system=_EXTRACT_SYSTEM + ANTI_INJECTION_CLAUSE,
                             messages=[{"role": "user", "content": user}],
                             max_tokens=2400, agent_name="tsd_test_assertions")
    except Exception as e:  # noqa: BLE001 — fail-open: no assertions rather than a broken gate
        logger.warning("extract_tsd_assertions LLM call failed (%s) — returning no assertions", e)
        return []
    data = await parse_llm_json(raw, fallback=None)
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for a in (data.get("assertions") or []):
        if not isinstance(a, dict):
            continue
        kind = str(a.get("kind") or "").strip().lower()
        if kind not in _ASSERTION_KINDS:
            continue
        section = str(a.get("tsd_section") or "")[:200]
        out.append({
            "id": f"{kind}:{len(out) + 1}",     # stable within one extraction call — used as the tsd-ref key
            "kind": kind,
            "tsd_section": section,
            "title": str(a.get("title") or "")[:200],
            "description": str(a.get("description") or "")[:500],
            "endpoint": str(a.get("endpoint") or "")[:200],
            "expected_status": a.get("expected_status") if isinstance(a.get("expected_status"), int) else None,
            "expected_field_or_code": str(a.get("expected_field_or_code") or "")[:120],
            "pass_criteria": str(a.get("pass_criteria") or "")[:300],
        })
    return out[:max_assertions]


def _normalise_ref(value: str) -> str:
    """Canonical form for comparing a `tsd-ref` marker to an assertion key.

    Exact string matching was too brittle to build an ENFORCING gate on: a
    marker of ``// tsd-ref: §4.2 Error Codes.`` would not match a
    ``tsd_section`` of ``4.2 Error Codes``, and the agent would be blocked for a
    trailing period. That turns a cosmetic difference into a false coverage gap
    and punishes correct work, which is the fastest way to get a gate switched
    off permanently.

    Normalises: case, surrounding whitespace, a leading section marker
    (``§``/``#``/``-``), trailing punctuation, and internal whitespace runs.
    Deliberately does NOT strip digits or words — two genuinely different
    sections must still compare unequal.
    """
    s = (value or "").strip().lower()
    s = re.sub(r'^[\s§#*\-–—.]+', '', s)      # leading section/comment noise
    s = re.sub(r'[\s.,;:]+$', '', s)          # trailing punctuation
    s = re.sub(r'\s+', ' ', s)                # collapse internal whitespace
    return s


def referenced_tsd_refs(test_sources: list[str]) -> set[str]:
    """Every `tsd-ref: <value>` marker found across the given test source texts
    (typically the ADDED lines of test files in a diff, or full file contents).
    Values are matched against an assertion's ``tsd_section`` OR ``id`` — the
    author may cite either, whichever the code-gen prompt asks for.

    Returned values are normalised via :func:`_normalise_ref`; compare against
    normalised assertion keys, not raw ones."""
    refs: set[str] = set()
    for src in test_sources or []:
        for m in _TSD_REF_RE.finditer(src or ""):
            ref = _normalise_ref(m.group(1))
            if ref:
                refs.add(ref)
    return refs


def assertions_block(assertions: list[dict], *, max_render: int = 25) -> str:
    """Render extracted TSD assertions for the CODE-GENERATION prompt.

    This is the authoring half of SDLC-A22. Without it the agent is told to
    emit ``// tsd-ref:`` markers but never told which assertions exist, so it
    guesses section names that must match the extractor's output exactly —
    making measured coverage a function of luck rather than test quality.

    Each line gives the agent the LITERAL marker string to copy, so a covered
    assertion is unambiguous to both the author and the coverage checker.
    Returns "" for no assertions, so the caller can concatenate unconditionally.
    """
    if not assertions:
        return ""
    lines = [
        "\nTSD ASSERTIONS THIS CHANGE MUST BE TESTED AGAINST",
        "Each item below was extracted from the APPROVED Tech Spec. For every one "
        "that your change touches, write a test that verifies it and copy the "
        "marker EXACTLY as shown on the line above the test method. The marker is "
        "how coverage is measured — an invented or paraphrased marker does not "
        "count, and a missing one makes correct work look uncovered.",
    ]
    for a in assertions[:max_render]:
        marker = (a.get("tsd_section") or a.get("id") or "").strip()
        if not marker:
            continue
        title = (a.get("title") or a.get("description") or "").strip()
        criteria = (a.get("pass_criteria") or "").strip()
        endpoint = (a.get("endpoint") or "").strip()
        detail = " | ".join(p for p in (
            f"kind={a.get('kind')}",
            f"endpoint={endpoint}" if endpoint else "",
            f"expect={a.get('expected_status')}" if a.get("expected_status") else "",
            f"field/code={a.get('expected_field_or_code')}" if a.get("expected_field_or_code") else "",
        ) if p)
        lines.append(f"\n- {title}")
        if criteria:
            lines.append(f"  pass criteria: {criteria}")
        if detail:
            lines.append(f"  ({detail})")
        lines.append(f"  marker: // tsd-ref: {marker}")
    if len(lines) <= 2:
        return ""
    lines.append(
        "\nIf an assertion is genuinely outside this change's scope, do NOT invent a "
        "test for it — an unrelated test is worse than an uncovered assertion. Cover "
        "what you touched."
    )
    return "\n".join(lines) + "\n"


def assertion_coverage(assertions: list[dict], test_sources: list[str]) -> dict:
    """Compute coverage of ``assertions`` (from :func:`extract_tsd_assertions`) by
    the given test sources. Returns
    ``{total, covered, coverage_ratio, uncovered: [assertion, ...]}``.
    An assertion counts as covered if its ``id`` OR ``tsd_section`` (case-
    insensitive) appears as a `tsd-ref` marker in ANY test source. Empty
    ``assertions`` is FULLY covered (ratio=1.0) — nothing to cover is not a gap;
    the caller (feature_test_gate) is what decides whether SOME test must still
    exist for a behavioural change."""
    if not assertions:
        return {"total": 0, "covered": 0, "coverage_ratio": 1.0, "uncovered": []}
    refs = referenced_tsd_refs(test_sources)
    uncovered = []
    covered_n = 0
    for a in assertions:
        # Normalised on BOTH sides — see _normalise_ref for why exact matching
        # was too brittle to enforce on.
        key_id = _normalise_ref(str(a.get("id") or ""))
        key_section = _normalise_ref(str(a.get("tsd_section") or ""))
        if (key_id and key_id in refs) or (key_section and key_section in refs):
            covered_n += 1
        else:
            uncovered.append(a)
    total = len(assertions)
    return {
        "total": total,
        "covered": covered_n,
        "coverage_ratio": (covered_n / total) if total else 1.0,
        "uncovered": uncovered,
    }
