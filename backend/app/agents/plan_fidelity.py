# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Plan-fidelity gate — after code-gen, verify the change actually DELIVERED the ratified plan,
not merely that it compiles. It catches two failure modes the adversarial reviewer reliably misses
(it grades correctness of what IS there, not what's MISSING):

  1. COVERAGE (deterministic): a file the plan said to create/change that the diff never touched is
     a DROPPED deliverable (e.g. the Risk-visibility SinkService edit, the config seed). Pure set
     logic on normalised paths — no LLM, no false confidence.
  2. BEHAVIOURAL (LLM): a planned behaviour / PM success-criterion the diff does NOT implement, and
     the single HARDEST requirement implemented in NAME ONLY — a stub, a hardcoded constant,
     degenerate logic that can never realistically trigger (e.g. a per-user baseline that compiles
     but is statistically broken).

Fail-open everywhere: any error → no findings. The gate never blocks a run on its own failure.
A 'blocker'-severity gap is merged into the review verdict so the run loops back to fix it.
"""
from __future__ import annotations

import logging
import re

from app.core.llm import call_llm
from app.core.config import settings
from app.core.json_recovery import parse_llm_json
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 1600

# A planned file whose intent is explicitly hedged is NOT a dropped deliverable when absent.
_OPTIONAL_RE = re.compile(
    r"\b(optional|may need no edit|no edit needed|confirm|to confirm|if needed|only if|future|"
    r"later|phase b|tbd|nice to have|may not)\b", re.I)
# Only treat plan entries that name a REAL source file as deterministically checkable; vague
# pseudo-paths like "dataaccessor (config_param seed / DDL)" are left to the behavioural check.
_REAL_FILE_RE = re.compile(r"\.(java|xsd|xjb|xml|sql|properties|ya?ml|kt|kts|js|jsx|ts|tsx|py|json|cfg)\b", re.I)

# ── Deterministic corroboration of the behavioural judge (gated by agentic_fidelity_corroborate) ──────
# A call-symbol (camelCase method w/ an internal capital) introduced on an ADDED ('+') diff line: hard
# proof the change DOES reference it — used to refute the LLM judge's "X is missing" hallucinations.
_CALL_ON_ADD_RE = re.compile(r"(?:\.|\b)([a-z][A-Za-z0-9]{3,})\s*\(")
# Wording that asserts pure ABSENCE — the only kind of claim a presence-check can legitimately refute.
_ABSENCE_RE = re.compile(
    r"\b(missing|absent|not\s+(?:forwarded|set|copied|implemented|present|added|propagat\w*|assign\w*|"
    r"invok\w*|call\w*|handl\w*)|no\s+such|does\s+not|doesn'?t|fail\w*\s+to|lacks?|"
    r"never\s+(?:set|forwarded|copied|called|added|propagat\w*))\b", re.I)
# Partial / location-specific claims (one leg done, the other not): the symbol appearing SOMEWHERE can't
# refute these, so they are NEVER auto-downgraded — a real "debit leg drops it" stays blocking.
_PARTIAL_RE = re.compile(r"\b(debit|credit|one\s+leg|both\s+legs?|only|but\s+not|partial\w*|first|second|"
                         r"each\s+leg|either\s+leg|reversal|timeout|fallback|branch|scenario|"
                         r"error\s+path|happy\s+path)\b", re.I)
# Security / financial / regulatory findings are NEVER auto-downgraded — they stay blocking regardless.
_SENSITIVE_RE = re.compile(r"\b(secur\w*|auth\w*|inject\w*|credential|secret|token|encrypt\w*|financial|"
                           r"settlement|fraud|frm|sgf|regulat\w*|complian\w*|pii|dpdp|signature|hmac|"
                           r"tls|mtls|tamper\w*)\b", re.I)
# Uppercase-initial too: class names (SplitPayOrchestrationService) must be matchable, else a
# "service X is missing" phantom can never be refuted (Test 8: 12 such phantoms looped the run).
_NAMED_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{3,})\b")
# Type declarations added by the change — class-level ground truth alongside call symbols.
_TYPE_DECL_RE = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Z][A-Za-z0-9]{2,})")


def added_call_symbols(diff_text: str) -> set[str]:
    """Distinctive symbols verifiably introduced by the change — deterministic 'this was added'
    ground truth. Two sources:
      · call-shaped camelCase tokens (``foo(`` / ``.foo(``) on ADDED ('+') diff lines;
      · type declarations (``class X`` …) on added lines.
    New untracked files are rendered by ``_render_diff`` as RAW content under a
    ``# repo <id> NEW FILE <path>`` marker — no '+' prefixes — so inside those blocks every
    line counts as added (skipping them blinded this to the whole feature in Test 8).
    Never raises."""
    out: set[str] = set()
    try:
        in_new_file = False
        for ln in (diff_text or "").splitlines():
            if ln.startswith("# repo "):
                in_new_file = " NEW FILE " in ln
                continue
            if ln.startswith(("diff --git", "--- ", "+++ ", "@@")):
                in_new_file = False
                added = False
            elif in_new_file:
                added = True
            else:
                added = ln.startswith("+")
            if not added:
                continue
            for m in _CALL_ON_ADD_RE.finditer(ln):
                tok = m.group(1)
                if any(c.isupper() for c in tok):
                    out.add(tok)
            for m in _TYPE_DECL_RE.finditer(ln):
                out.add(m.group(1))
    except Exception:  # noqa: BLE001 — purely additive ground truth; never break the gate
        return set()
    return out


def corroborate(findings: list[dict], added_syms: set[str],
                touched_stems: set[str] | None = None) -> list[dict]:
    """Downgrade a 'missing_behavior' BLOCKER to advisory when it asserts the ABSENCE of a symbol that is
    verifiably ADDED in the diff — and the claim is neither leg/partial-specific nor security/financial.
    Downgrade-ONLY (kept visible, never deleted, never escalated); high-precision; fail-open. The
    deterministic authority over the non-deterministic judge for the unambiguous 'you say X is missing but
    X is right here' case; everything ambiguous is deliberately left to the judge + human gate.

    Two evidence tiers, deliberately NOT merged:
      · ``added_syms``     — call/type symbols genuinely added in the diff (strong: the exact symbol exists).
      · ``touched_stems``  — changed-file basenames (proves the FILE/CLASS exists, e.g. refutes "class X is
                             missing" when X's declaration isn't in the diff excerpt).
    A touched file-stem must NOT refute a claim that a specific MEMBER is missing from that file
    ("validateSplit not added to ReqTransferValidator"): the container existing does not prove the member was
    added. So if the finding names any unaccounted member (a camelCase, lowercase-initial token that is NOT
    among ``added_syms``), the blocker is kept regardless of a file-stem match."""
    touched_stems = touched_stems or set()
    if not added_syms and not touched_stems:
        return findings
    out: list[dict] = []
    for f in findings:
        try:
            if f.get("severity") == "blocker" and f.get("kind") == "missing_behavior":
                txt = f"{f.get('item', '')} {f.get('detail', '')}"
                if (_ABSENCE_RE.search(txt) and not _PARTIAL_RE.search(txt)
                        and not _SENSITIVE_RE.search(txt)):
                    named = set(_NAMED_TOKEN_RE.findall(txt))
                    # A claimed-missing MEMBER is a camelCase, lowercase-initial token (e.g. validateSplit)
                    # not among the genuinely-added symbols. If one exists, a container file-stem can't
                    # refute it — keep the blocker.
                    unaccounted = {t for t in named
                                   if t[:1].islower() and any(c.isupper() for c in t) and t not in added_syms}
                    hit = sorted(named & (added_syms | touched_stems))
                    if hit and not unaccounted:
                        f = {**f, "severity": "warning", "kind": "missing_behavior_refuted",
                             "detail": (f.get("detail", "") + "  [deterministic check: " + ", ".join(hit[:4])
                                        + " IS added in the diff — absence claim not upheld; downgraded to "
                                        "advisory, still shown to the human, no longer a blocker]")}
                        logger.info("plan_fidelity: deterministic corroboration downgraded a missing_behavior "
                                    "blocker (symbols added in diff: %s)", hit)
        except Exception:  # noqa: BLE001 — per-finding guard: never drop/alter a finding on error
            pass
        out.append(f)
    return out


def _norm(path: str) -> str:
    """Normalise to the last two path segments (lowercased) so repo-prefix / absolute-vs-relative
    differences between the plan and the diff don't read as 'missing'."""
    p = (path or "").replace("\\", "/").strip().strip("/")
    segs = [s for s in p.split("/") if s]
    return "/".join(segs[-2:]).lower() if segs else ""


def file_coverage(planned_files: list[dict], touched_files: list[str],
                  reconciliations: list[dict] | None = None) -> list[dict]:
    """Deterministic coverage. ``planned_files`` = ``[{path, intent}]`` from the plan's
    per_file_changes; ``touched_files`` = the diff's paths. Returns a finding per planned, real
    file the diff never touched. Severity is calibrated on the plan's OWN intent text:
      · substantive + un-hedged intent ("Add validateSplit to ReqTransferValidator") → 'blocker'
        (a clearly-required deliverable the diff dropped);
      · hedged intent ("optional", "confirm in Phase B", "if needed", "may need no edit") → 'warning';
      · empty/terse intent (plan listed the path but didn't describe a change) → 'warning'
        (too little evidence to hard-block — avoids trapping the run on a reuse-no-edit file).
    Never raises on bad input."""
    touched = {_norm(t) for t in (touched_files or []) if t}
    # D2 — explicit reconciliations from the code agent's submitted plan: a planned file whose
    # logic was deliberately consolidated elsewhere is NOT a dropped deliverable, provided the
    # declared actual_path really is in the change. Forcing edits into the plan's exact layout
    # made the generator scatter code against its own architecture (both Test-8 clean runs).
    recon: dict[str, tuple[str, str]] = {}
    for r in (reconciliations or []):
        if isinstance(r, dict) and r.get("planned_path") and r.get("actual_path"):
            recon[_norm(str(r["planned_path"]))] = (str(r["actual_path"]), str(r.get("why") or ""))
    findings: list[dict] = []
    seen: set[str] = set()
    for pf in (planned_files or []):
        if not isinstance(pf, dict):
            continue
        # The plan's per_file_changes schema is INCONSISTENT across runs — some use "path",
        # some "file" (also seen: "filepath"). Accept any so coverage never silently no-ops.
        path = (pf.get("path") or pf.get("file") or pf.get("filepath") or "").strip()
        if not path or not _REAL_FILE_RE.search(path):
            continue   # not a concrete source file → behavioural check covers it
        key = _norm(path)
        if not key or key in seen:
            continue
        seen.add(key)
        if key in touched:
            continue
        if key in recon:
            actual, why = recon[key]
            if _norm(actual) in touched:
                findings.append({
                    "severity": "info", "kind": "reconciled_file", "item": path,
                    "detail": (f"Planned logic delivered in '{actual}' per the agent's declared "
                               f"reconciliation ({why[:120] or 'no reason given'}) — verify the "
                               "behaviour there, not a missing file."),
                })
                continue
            # A reconciliation pointing at a file NOT in the change is a false claim — keep
            # the normal missing-file handling below (severity by intent hedging).
        # Naming near-miss: the plan's basename is a substring of a changed file's basename (or
        # vice versa) — almost certainly the same deliverable under a slightly different name
        # (Test 8: plan `SessionExpiryScheduler.java`, code `SplitSessionExpiryScheduler.java`).
        # Hard-blocking it tells the code agent to CREATE A DUPLICATE; downgrade to advisory
        # naming it, so the human (or agent) confirms/renames instead of re-implementing.
        stem = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]

        def _near(t: str) -> bool:
            ts = t.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            # the substring side must be ≥8 chars, so short stems can't false-positive
            return ((len(stem) >= 8 and stem in ts) or (len(ts) >= 8 and ts in stem))

        near = next((t for t in touched if _near(t)), None) if stem else None
        if near:
            findings.append({
                "severity": "warning",
                "kind": "missing_file_near_miss",
                "item": path,
                "detail": (f"Plan named this file but the change contains a similarly-named file "
                           f"('{near}') — likely the SAME deliverable under a different name. "
                           "Confirm/rename rather than re-creating it — advisory."),
            })
            continue
        intent = str(pf.get("intent") or "").strip()
        # Severity turns on the plan's OWN description. A BLOCKER requires a substantive, un-hedged
        # intent — the plan clearly asked for this file's change ("Add validateSplit to ReqTransferValidator")
        # and the diff dropped it. A hedged intent ("confirm in Phase B", "optional", "if needed",
        # "or replicate in X") OR a terse/empty one (path listed, no change described) → warning: too
        # little evidence to hard-block, and blocking a reuse-no-edit file once trapped runs in a
        # review↔code loop. This is the fix for a run that dropped 5 load-bearing files (each with a
        # substantive intent) yet shipped clean. Escape valves: (1) this calibration, and (2) the
        # self-heal loop-back + stall→human escalation in the orchestrator — a mis-fire never traps
        # the run or silently vanishes; it either self-heals or surfaces.
        hedged = (not intent) or bool(_OPTIONAL_RE.search(intent))
        findings.append({
            "severity": "warning" if hedged else "blocker",
            "kind": "missing_file",
            "item": path,
            "detail": (("Plan named this file with a hedged intent and the change never touched it "
                        "— advisory."
                        if hedged else
                        "Plan named this file as a required deliverable but the change never touched "
                        "it — dropped deliverable. EITHER edit it, OR — if you deliberately implemented "
                        "its logic elsewhere / it needs no edit (e.g. auto-registration) — re-call "
                        "submit_plan adding a reconciliation entry {planned_path, actual_path, why}; "
                        "that clears this gap. Looping without doing one of the two cannot converge.")
                       + (f" Intent: {intent[:160]}" if intent else "")),
        })
    return findings


_BEHAVIORAL_SYSTEM = (
    "You are a COMPLETENESS auditor for an AI-generated code change in the network codebase. You are "
    "given (1) the ratified PLAN (intended behaviours), (2) the PM's SUCCESS CRITERIA, and (3) a SUMMARY "
    "of the actual code DIFF. Your job is NOT to find ordinary bugs — it is to find PROMISED-BUT-MISSING "
    "work and FAKED implementations:\n"
    "  - A planned behaviour or PM success-criterion the diff does NOT implement → severity 'blocker', "
    "kind 'missing_behavior'.\n"
    "  - The single HARDEST requirement implemented in NAME ONLY — a stub, a hardcoded constant where "
    "config was required, degenerate logic that can never realistically trigger, a TODO/placeholder → "
    "severity 'blocker', kind 'faked_logic'.\n"
    "  - A minor planned nicety that's missing → severity 'warning'.\n\n"
    "Judge ONLY against the plan + success criteria. Do NOT flag style, naming, or correctness of code "
    "that IS present and plausibly works. If the diff genuinely covers the plan, return an empty list — "
    "that is the expected good case.\n\n"
    "The DIFF text may be an EXCERPT. A separate COMPLETE FILE LIST is deterministic ground truth: "
    "every file the change touched is on it. NEVER claim a file, class, or service on that list is "
    "missing/absent from the change — if its content is not visible to you, you cannot call it "
    "missing; you may only flag visible content as faked or wrong.\n\n"
    "Respond with ONLY a JSON object:\n"
    "{\n"
    '  "findings": [{"severity": "blocker|warning", "kind": "missing_behavior|faked_logic", '
    '"item": "<short name of the gap>", "detail": "<one sentence: plan wanted X, diff does Y>"}]\n'
    "}\n"
    + ANTI_INJECTION_CLAUSE
)


async def behavioral_coverage(*, plan_text: str, success_criteria: str, diff_summary: str,
                              verified_present: set[str] | None = None,
                              touched_files: list[str] | None = None) -> list[dict]:
    """LLM completeness + faked-logic check. Fail-open: returns [] on any LLM/parse failure or when
    there's nothing to compare against. ``verified_present`` (deterministic — symbols verifiably
    added by the change) and ``touched_files`` (the COMPLETE changed-file list) are fed as ground
    truth so the judge does not hallucinate that an added call / created file is missing — the
    18k blind slice made it report 12 phantom 'missing service' gaps on Test 8 (the new files
    rendered past the cut) and loop the run."""
    if not (plan_text or "").strip() or not (diff_summary or "").strip():
        return []
    files_block = ""
    if touched_files:
        files_block = (
            "COMPLETE FILE LIST — deterministic ground truth: EVERY file this change touched. "
            "A planned deliverable matching one of these files EXISTS; do NOT report it (or its "
            "class/service) as missing, even if the diff excerpt below does not show it:\n"
            + "\n".join(f"  {p}" for p in sorted(set(touched_files))[:80]) + "\n\n")
    present_block = ""
    if verified_present:
        present_block = (
            "VERIFIED PRESENT — deterministic: the following calls/classes ARE added by the "
            "diff above. Treat them as IMPLEMENTED; do NOT report any as missing / not-forwarded / not-set. "
            "You MAY still flag one as faked or used INCORRECTLY, but only by citing the specific wrong "
            f"line:\n{', '.join(sorted(verified_present)[:100])}\n\n")
    def _mark(s: str, cap: int, what: str) -> str:
        # LOUD clip: the judge must know its inputs are excerpts — a silent slice let it
        # verdict against a plan whose operative items fell past the cut.
        return s if len(s) <= cap else (
            s[:cap] + f"\n… [⚠ {what} CLIPPED — {len(s) - cap} of {len(s)} chars omitted]")

    user = (f"RATIFIED PLAN (intended behaviours):\n{wrap_untrusted(_mark(plan_text, 12000, 'PLAN'), 'PLAN')}\n\n"
            f"PM SUCCESS CRITERIA:\n{wrap_untrusted(_mark(success_criteria or '(none provided)', 4000, 'CRITERIA'), 'SUCCESS')}\n\n"
            f"{files_block}"
            f"ACTUAL CODE DIFF (may be an excerpt — the file list above is complete):\n"
            f"{wrap_untrusted(_mark(diff_summary, 60000, 'DIFF'), 'DIFF')}\n\n"
            f"{present_block}"
            "List planned behaviours / success-criteria the diff does NOT implement, plus any hardest-"
            "requirement that is faked or stubbed. Empty list if the plan is fully delivered.")
    # Infrastructure failure must NOT read as "clean": [] is the good-case verdict, so an
    # errored gate returns a visible gate_error finding instead (warning — surfaced in the
    # review output without weaponizing transient LLM failures into run-blockers).
    _gate_error = [{"severity": "warning", "kind": "gate_error",
                    "item": "plan-fidelity gate errored",
                    "detail": "behavioural coverage was NOT verified this round (LLM/parse "
                              "failure) — treat 'no findings' as UNKNOWN, not clean."}]
    try:
        raw = await call_llm(system=_BEHAVIORAL_SYSTEM, messages=[{"role": "user", "content": user}],
                             max_tokens=MAX_OUTPUT_TOKENS, agent_name="plan_fidelity")
    except Exception as e:  # noqa: BLE001 — visible fail-open (gate_error), never a crash
        logger.warning("plan_fidelity behavioral check failed (%s) — surfacing gate_error", e)
        return _gate_error
    data = await parse_llm_json(raw, fallback=None)
    if not isinstance(data, dict):
        return _gate_error
    out: list[dict] = []
    for f in (data.get("findings") or []):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "warning").lower()
        if sev not in ("blocker", "warning"):
            sev = "warning"
        out.append({"severity": sev,
                    "kind": str(f.get("kind") or "")[:32],
                    "item": str(f.get("item") or "")[:120],
                    "detail": str(f.get("detail") or "")[:300]})
    if len(out) > 15:
        clipped = out[15:]
        out = out[:15]
        out.append({"severity": "warning", "kind": "findings_clipped",
                    "item": f"+{len(clipped)} further findings clipped",
                    "detail": "kept the first 15; also reported: "
                              + "; ".join(str(c.get("item") or "?") for c in clipped[:8])})
    return out


async def check_plan_fidelity(*, plan_text: str, success_criteria: str,
                              planned_files: list[dict], touched_files: list[str],
                              diff_summary: str,
                              reconciliations: list[dict] | None = None) -> dict:
    """Run both checks and merge. Returns
    ``{findings:[{severity,kind,item,detail}], has_gap, missing_files}``. ``has_gap`` is True iff a
    'blocker'-severity gap exists (the caller blocks the run on it). Fail-open throughout."""
    findings: list[dict] = []
    try:
        findings += file_coverage(planned_files, touched_files, reconciliations=reconciliations)
    except Exception as e:  # noqa: BLE001 — deterministic check must never break the gate
        logger.warning("plan_fidelity file_coverage failed: %s", e)
    _corrob = getattr(settings, "agentic_fidelity_corroborate", True)
    added_syms = added_call_symbols(diff_summary) if _corrob else set()
    touched_stems: set[str] = set()
    if _corrob:
        # Changed-file basenames prove the FILE/CLASS exists ("class/service X is missing" cannot stand
        # when a file named X is in the change-set — the un-refutable Test-8 phantom). Kept SEPARATE from
        # added_syms: a file existing does not prove a specific member was added (see corroborate()).
        for t in (touched_files or []):
            stem = t.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if len(stem) > 3:
                touched_stems.add(stem)
    beh = await behavioral_coverage(
        plan_text=plan_text, success_criteria=success_criteria, diff_summary=diff_summary,
        verified_present=((added_syms | touched_stems) or None), touched_files=touched_files)
    if _corrob:
        beh = corroborate(beh, added_syms, touched_stems)
    findings += beh
    if len(findings) > 20:
        # Blockers must survive the cap — a silent tail-cut could drop a blocker behind
        # 20 warnings and flip has_gap. Keep all blockers first, fill with warnings, and
        # say what was cut.
        blockers = [f for f in findings if f.get("severity") == "blocker"]
        rest = [f for f in findings if f.get("severity") != "blocker"]
        kept = (blockers + rest)[:20]
        kept.append({"severity": "warning", "kind": "findings_clipped",
                     "item": f"+{len(findings) - 20} findings clipped",
                     "detail": "merged finding list capped at 20 (all blockers retained)"})
        findings = kept
    has_gap = any(f["severity"] == "blocker" for f in findings)
    missing_files = [f["item"] for f in findings if f.get("kind") == "missing_file"]
    return {"findings": findings, "has_gap": has_gap, "missing_files": missing_files}
