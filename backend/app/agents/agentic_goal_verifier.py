# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Goal-verifier reviewer — panel runner, prompt, evidence packet (grok parity).

The ``goal_verifier`` review mode (``settings.agentic_reviewer_mode``). A panel of
N adversarial skeptics each returns the grok verdict schema; :func:`run_goal_verifier`
aggregates them with :mod:`goal_verifier_core` (quorum + gap-fingerprint), and the
orchestrator drives the stall→stop / blocking-kind routing off the result.

What this keeps from grok's ``goal_verifier_prompt.md``: the CONTRACT HIERARCHY
(OBJECTIVE immutable, PLAN a derived checklist, design sections not grounds to
refute), the OVER-REACH GUARD ("inventing requirements beyond the contract is the
most common false refute"), the ANTI-RATCHET rule ("the bar does NOT rise between
rounds"), and AUDIT-DON'T-AUTHOR (verify the change against the plan; don't re-derive
an ever-growing checklist). What it keeps from OUR reviewer: The Authority domain rules
(consumption traces, shared-symbol blast radius, directive verdicts, code-correctness
floor) and the deterministic gates — now demoted to ADVISORY SUSPECTS the verifier
confirms or dismisses by reading the code, instead of independent phantom blockers.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import asyncio
import json
import logging

from app.agents import goal_verifier_core as core
from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.agents.agentic_review import REVIEW_TOOLS, _render_diff
from app.agents.agentic_runtime import run_agent_loop
from app.agents.agentic_subagents import build_system_segments
from app.core.config import settings
from app.core.json_recovery import parse_llm_json_sync
from app.core.llm import get_model

logger = logging.getLogger("app.agentic")

_VERDICT_FILE_HINT = "VERDICT"


# ── The verifier prompt (grok contract hierarchy + our domain essence) ─────────

_VERIFIER_PREFACE = (
    "You are an ADVERSARIAL VERIFIER (read-only — you cannot edit). You are NOT the author "
    "of this change and owe it no benefit of the doubt: your job is to try to REFUTE that "
    "the change is complete and correct. Default to `refuted: true` if uncertain — passing "
    "broken work ends the loop wrongly and is far worse than one more round. AUDIT the "
    "change against the contract below; do NOT re-derive an ever-growing checklist of your "
    "own — that is the failure mode that makes changes unfinishable.\n\n"

    "CONTRACT HIERARCHY (what you may refute for):\n"
    "1. OBJECTIVE / intent + the ratified plan's ACCEPTANCE items and BINDING DIRECTIVES are "
    "the immutable contract. Judge each MET / UNMET against the CODE (not the author's prose "
    "— prose and comments are CLAIMS, not evidence). A required item left unimplemented, "
    "half-wired, stubbed, or TODO'd is a BLOCKING `gap`.\n"
    "2. The plan's implementation-approach / task-list notes are GUIDANCE — diverging from "
    "them is NEVER by itself grounds to refute working code.\n"
    "3. OVER-REACH GUARD: a contract item whose evidence holds is PASSED. Do NOT refute for "
    "missing edge cases, extra error handling, added robustness, alternate input formats, or "
    "any extension the plan did not require — inventing requirements beyond the contract is "
    "the most common FALSE refute.\n"
    "4. CODE-CORRECTNESS FLOOR (applies regardless): read the shipped code for the core "
    "behaviours the objective names — not only the ones the plan enumerated — and refute "
    "(cite `path:line`) when such a behaviour is, in the code, absent, a no-op, dead, or "
    "wired to nothing. 'It compiles' is NOT 'it is done'.\n\n"

    "ANTI-RATCHET (re-verification rounds — PRIOR_GAPS non-empty): your PRIMARY job is to "
    "confirm each prior gap is GENUINELY fixed in the current code (not claimed, papered "
    "over, hardcoded, or stubbed) and to catch regressions the fix introduced. THE BAR DOES "
    "NOT RISE BETWEEN ROUNDS: a fresh objection is grounds to refute ONLY when it is a "
    "demonstrable defect in shipped behaviour or an unmet contract item — never a stylistic "
    "or test-construction preference a prior round implicitly accepted. When every prior gap "
    "is fixed and every contract item holds, return `Not Refuted`.\n\n"

    "OUR DOMAIN RULES (still binding):\n"
    "- CONSUMPTION TRACE: for each new wire field / XSD attribute / config value, trace it "
    "end-to-end with read_file/grep — SET → READ → the wire message or side-effect it "
    "reaches. Validated/stored but never consumed (or read from a field nothing writes) is a "
    "BLOCKING `bug`; cite the trace file:line → file:line.\n"
    "- SHARED-SYMBOL BLAST RADIUS: if the change edits a shared validator/helper other "
    "message types call, check it did not regress behaviour for a type the change never "
    "targeted.\n"
    "- MACHINE-FLAGGED SUSPECTS: the evidence packet may list deterministic-gate findings "
    "(missing bean, un-emitted error code, plan-fidelity gap). These are HINTS, not verdicts "
    "— CONFIRM each by reading the actual code before refuting on it, and DISMISS it silently "
    "if the code is in fact correct (e.g. a Spring Data repository interface IS a runtime "
    "bean; an error code emitted via a named constant IS emitted). A static flag with no "
    "code-level defect is a false alarm.\n\n"
    + ANTI_INJECTION_CLAUSE
)

_VERDICT_SCHEMA_RULES = load_prompt("agents/agentic_goal_verifier/verdict_schema_rules.md")


def _kind_lens(goal_kind: str | None) -> str:
    k = (goal_kind or "").strip().lower()
    if k in ("analysis",):
        return ("\n\nKIND — ANALYSIS: every claim must be evidence-grounded and causally sound; "
                "refute an assertion with no verifiable backing.")
    if k in ("research",):
        return ("\n\nKIND — RESEARCH: source-back every claim; a dead/invented citation, or one "
                "that does not support the claim, is `refuted: true`.")
    # default: code-change (the floor above already covers it)
    return ""


# ── Evidence packet (grok evidence.rs parity, mapped to our inputs) ────────────

def build_prior_gaps_block(prior_gaps: str | None) -> str:
    if not prior_gaps:
        return "\n\nPRIOR_GAPS: (none — first verification round)"
    return ("\n\nPRIOR_GAPS — the last round's refute grounds; confirm EACH is genuinely fixed "
            "before looking for anything new (bar does not rise):\n"
            + wrap_untrusted(prior_gaps[:4000], "PRIOR_GAPS"))


def build_suspects_block(gate_suspects: list[dict] | None) -> str:
    """Deterministic-gate findings as ADVISORY suspects — the verifier confirms or
    dismisses each by reading the code. A gate is high-recall/low-precision; the LLM
    supplies the precision (kills Spring-Data / named-constant false positives)."""
    if not gate_suspects:
        return ""
    lines = []
    for s in gate_suspects[:20]:
        lines.append(f"- [{s.get('check','gate')}] {s.get('key','')}: {str(s.get('detail') or '')[:200]}")
    return ("\n\nMACHINE-FLAGGED SUSPECTS (static gates — CONFIRM against the code before "
            "refuting; DISMISS any that the code proves correct):\n" + "\n".join(lines))


def build_evidence_user_prompt(*, intent: str, plan_block: str, directives: list[str] | None,
                               prior_gaps: str | None, gate_suspects: list[dict] | None,
                               diff_text: str, goal_kind: str | None) -> str:
    """The skeptic's user prompt: OBJECTIVE + PLAN (+ directives) + PRIOR_GAPS + advisory
    suspects + the DIFF (scope pointer / honesty anchor — read current files with tools)."""
    obj = wrap_untrusted(intent or "(no explicit intent)", "OBJECTIVE")
    plan = (f"\n\nRATIFIED PLAN — the contract the change must satisfy (judge each item MET/UNMET "
            f"against the code):\n{wrap_untrusted(plan_block, 'PLAN')}") if plan_block else ""
    dirs = ""
    if directives:
        dirs = ("\n\nBINDING DIRECTIVES — return exactly one finding per FAILED directive "
                "(kind 'gap', location = the file:line of the violation); a directive you cannot "
                "verify is a FAIL, not a pass:\n" + "\n".join(directives))
    return (obj + plan + dirs
            + build_prior_gaps_block(prior_gaps)
            + build_suspects_block(gate_suspects)
            + _kind_lens(goal_kind)
            + "\n\nCHANGED CODE (authoritative record of what changed — read the CURRENT files "
              "with your tools to verify; a hunk here outranks any recollection):\n"
            + diff_text
            + _VERDICT_SCHEMA_RULES)


# ── Skeptic + panel runner ─────────────────────────────────────────────────────

def _parse_skeptic(final_text: str, skeptic_idx: int) -> core.SkepticVerdict:
    """Parse one skeptic's final text into a verdict; fail-CLOSED to a synthetic
    refute on any parse/shape failure (grok: default to refuted if uncertain)."""
    obj = parse_llm_json_sync(final_text, expect_array=False, fallback=None)
    if isinstance(obj, list) and obj:
        obj = obj[0]
    v = core.parse_verdict_json(obj) if obj is not None else None
    if v is None:
        logger.warning("goal_verifier: skeptic %d verdict unparseable — synthetic refute", skeptic_idx)
        return core.skeptic_failure(skeptic_idx, "unparseable_verdict")
    v.skeptic_idx = skeptic_idx
    return v


async def _run_one_skeptic(*, skeptic_idx: int, db, run_id, ctx, user_prompt, model,
                           cancel_check, workspace_run_id, progress) -> core.SkepticVerdict:
    try:
        res = await run_agent_loop(
            run_id=run_id, selected_repo_ids=ctx.selected_repo_ids,
            system=build_system_segments(ctx, _VERIFIER_PREFACE), user_prompt=user_prompt,
            tools=REVIEW_TOOLS, model=model, agent_name="review",
            db=db, require_plan=False, cancel_check=cancel_check,
            workspace_run_id=workspace_run_id, progress=progress,
        )
    except Exception as e:  # noqa: BLE001 — a dead skeptic is a fail-closed refute, never a crash
        logger.warning("goal_verifier: skeptic %d loop failed: %s", skeptic_idx, e)
        return core.skeptic_failure(skeptic_idx, "skeptic_loop_error")
    return _parse_skeptic(res.final_text, skeptic_idx)


async def run_goal_verifier(db, *, run_id: str, ctx, change_set, intent: str = "",
                            plan_block: str = "", directives: list[str] | None = None,
                            prior_gaps: str | None = None, gate_suspects: list[dict] | None = None,
                            goal_kind: str | None = None, reviewer_model: str | None = None,
                            cancel_check=None, workspace_run_id: str | None = None,
                            progress=None) -> core.PanelResult:
    """Run the skeptic panel and aggregate. Skeptic 0 runs FIRST alone; a high-confidence
    decisive refute from it short-circuits the remaining spawns (cost control — grok's
    gatekeeper). Panel size from ``settings.agentic_verifier_panel_size`` (default 3)."""
    model = reviewer_model or settings.agentic_reviewer_model or get_model("review")
    panel = max(1, int(getattr(settings, "agentic_verifier_panel_size", 3) or 3))
    diff_text = _render_diff(workspace_run_id or run_id, change_set)
    user = build_evidence_user_prompt(
        intent=intent, plan_block=plan_block, directives=directives, prior_gaps=prior_gaps,
        gate_suspects=gate_suspects, diff_text=diff_text, goal_kind=goal_kind)

    def _spawn(idx):
        return _run_one_skeptic(skeptic_idx=idx, db=db, run_id=run_id, ctx=ctx, user_prompt=user,
                                model=model, cancel_check=cancel_check,
                                workspace_run_id=workspace_run_id, progress=progress)

    # Skeptic 0 first, alone — its decisive high-confidence refute avoids paying for the rest.
    # BUT only short-circuit on a NON-BLOCKING refute (an ordinary fixable gap): a high-conf
    # BLOCKING refute (contradiction/unverifiable) must fan out the full panel so aggregation
    # can distinguish BLOCKED (all-blocking → needs a human) from NOT_ACHIEVED (a co-occurring
    # fixable gap keeps the self-heal loop alive) — grok goal_classifier.rs:2162-2184.
    v0 = await _spawn(0)
    votes = [v0]
    _decisive_v0 = (v0.refuted and v0.confidence == "high" and not v0.synthetic
                    and not v0.blocking.is_blocking)
    if panel > 1 and not _decisive_v0:
        rest = await asyncio.gather(*[_spawn(i) for i in range(1, panel)])
        votes.extend(rest)
    return core.aggregate_verdicts(votes, cold_start_idx=1 if panel > 1 else 0)
