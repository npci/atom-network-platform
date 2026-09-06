# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Anthropic review subagent (THE BOOK §10).

A READ-ONLY pass over the ChangeSet diff + XsdScope + intent that produces
structured ``ReviewFindings``. Anthropic-only (§10): it runs on the Claude model
(asserted to be an Anthropic id), via the same S6 loop but with a read-only tool
subset — it cannot edit. Blocking findings loop back to code-change for at most
``agentic_max_review_rounds`` rounds (orchestrated in S13), then a human
adjudicates.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import logging
from dataclasses import dataclass, field

from app.agents import workspace_local
from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.agents.agentic_runtime import run_agent_loop
from app.agents.agentic_subagents import build_system_segments, _PRIORITY_ORDER
from app.agents.agentic_tools import TOOL_SCHEMAS
from app.agents.platform_adapter import adapter
from app.core.config import settings
from app.core.json_recovery import parse_llm_json_sync
from app.core.llm import get_model

logger = logging.getLogger("app.agentic")

# Read-only tools — the reviewer observes, never mutates (no edit/create/delete).
_REVIEW_TOOL_NAMES = {"read_file", "grep", "glob", "find_existing_xsd", "symbol_graph", "ast_query",
                      "module_context", "code_search_semantic"}
REVIEW_TOOLS = [t for t in TOOL_SCHEMAS if t["name"] in _REVIEW_TOOL_NAMES]
# Tools a reviewer may NEVER hold, whatever a caller hands in as `extra_tools` — the
# read-only invariant above is the reason the reviewer can be trusted to judge the diff
# rather than quietly repair it. A caller that needs an editing tool wants the code
# agent, not this one.
_MUTATING_TOOL_NAMES = {"edit_file", "create_file", "delete_file"}


def _tools_for(extra_tools: list | None) -> list:
    """REVIEW_TOOLS plus caller-supplied schemas, first occurrence wins on name so an
    extra can never shadow a review tool's schema, and mutating tools are refused."""
    if not extra_tools:
        return REVIEW_TOOLS
    seen = {t["name"] for t in REVIEW_TOOLS}
    out = list(REVIEW_TOOLS)
    for t in extra_tools:
        name = t.get("name")
        if name in _MUTATING_TOOL_NAMES:
            raise ValueError(f"review tool {name!r} would let the read-only reviewer mutate "
                             "the workspace — extra_tools may not include an editing tool")
        if name in seen:
            continue
        seen.add(name)
        out.append(t)
    return out

_CATEGORIES = ("correctness", "security", "convention", "reuse", "regulatory", "directive")
_SEVERITIES = ("info", "warning", "error", "blocker")
# 30k starved the reviewer on any multi-file feature: NEW files rendered after the tracked
# diff, so on an ~89k change (Test 8) every new file — the feature itself — fell past the cap
# and the reviewer/judge reported them "missing" (13 phantom gaps, review↔code loop). 150k
# fits the model window; the manifest header below covers whatever still overflows.
_DIFF_CAP = 150_000

_REVIEW_PREFACE = (
    "You are the Review agent (read-only — you cannot edit). You are NOT the author of "
    "this change and you owe it no benefit of the doubt: your job is to try to REFUTE the "
    "claim that the change is complete and correct. Direction of the skepticism matters:\n"
    "- A PASS verdict on a plan item or binding directive must be EARNED by evidence YOU "
    "gathered this round; if the evidence is inconclusive after using your tools, the "
    "verdict is FAIL / not-verified — never PASS by charity. The author's prose, commit "
    "intent, and code comments are CLAIMS, not evidence.\n"
    "- Raising a NEW finding still requires confirmed evidence — use read_file/grep to "
    "confirm a concern before raising it; do not speculate. Skepticism means refusing to "
    "certify the unproven, not inventing defects.\n\n"
    "Review the change for correctness, security, convention, reuse, and regulatory "
    "compliance. A finding is `blocking` only if it would break correctness/security/"
    "regulatory compliance if shipped.\n\n"
    "COMPLETENESS IS YOUR PRIMARY JOB. A PLAN is given — verify the diff ACTUALLY implements "
    "every item of it, end-to-end. The following are BLOCKING `correctness` findings (issue = "
    "'half-baked: <what is missing>'):\n"
    "- a planned file/flow-step/data-model change left unimplemented or only partially wired;\n"
    "- a new field/element that is set/parsed but never MAPPED into the downstream request and "
    "never ACTED ON (the 'field produced but never consumed' anti-pattern) — grep both sides to "
    "confirm it is actually used;\n"
    "- a schema/signature change whose existing CONSUMERS were not all updated (grep the old "
    "name/accessor across every repo);\n"
    "- a TODO / FIXME / stub / 'not implemented' / empty-body placeholder left in the change.\n"
    "'It compiles' is NOT 'it is done'. If the diff does not fully deliver the plan's intent, BLOCK it "
    "with a precise, file-anchored finding telling the implementer exactly what to finish.\n\n"
    "CONSUMPTION TRACES: for EVERY new wire field / XSD attribute / config value the change "
    "introduces, trace it end-to-end with read_file/grep — where it is SET, where it is READ, and "
    "which wire message or side-effect it reaches. A value that is validated/stored but never "
    "consumed downstream (or consumed from a field nothing writes) is a BLOCKING correctness "
    "finding; cite the trace (file:line → file:line) in `why`. NEVER accept a code comment as "
    "evidence of behaviour — comments are claims; verify them by reading the code they reference "
    "(a false 'this is a duplicate' comment once justified deleting a live confirmation dispatch).\n\n"
    "SHARED-SYMBOL BLAST RADIUS: if the change edits a SHARED validator/helper (e.g. ValidatorCommons "
    "or a *Util/*Base method) that OTHER message validators also call, check whether it changes "
    "behaviour for message types the change never targeted — a tightened allow-list or a new required "
    "check on a shared method is a backward-compat regression for every untouched caller. Prefer "
    "isolating the rule to the message-specific validator; raise it as a `correctness` finding.\n\n"
    # SDLC review gaps 1/2/3/6 — same priority order the code agent is held to, so a
    # finding can name WHICH principle a trade-off deprioritized (e.g. "the author
    # skipped a security check to keep the change minimal — Security outranks
    # smallest-change; this is a blocking finding, not an acceptable trade-off").
    + _PRIORITY_ORDER
    + ANTI_INJECTION_CLAUSE
)

_OUTPUT_RULES = load_prompt("agents/agentic_review/output_rules.md")

# Appended ONLY when reviewing a code-phase (Phase B) change. The reviewer used to have no
# idea the schema was frozen, so on a schema/code mismatch it prescribed the obvious fix —
# "edit the .xsd" — which the code agent is structurally forbidden to do. It then re-issued
# the same unexecutable order every round while the agent escalated to a human seven times.
# The finding was RIGHT; only its remedy was impossible. Naming the constraint keeps the
# finding and routes it to the mechanism that can actually satisfy it.
_SCHEMA_LOCK_CLAUSE = (
    "\n\nSCHEMA IS FROZEN IN THIS PHASE. The .xsd/.xjb files were authored and human-approved "
    "in Phase A and are the FIXED baseline here; the implementer physically cannot edit them — "
    "a schema write does not land, it STAGES a schema-amendment proposal that parks the run for "
    "human approval. So:\n"
    "- NEVER write a `suggested_fix` of the form 'change the XSD' / 'edit <file>.xsd line N'. "
    "That is not an instruction the implementer can carry out, and prescribing it again is how a "
    "review loop deadlocks.\n"
    "- If the defect genuinely CANNOT be fixed in Java — the schema itself is wrong on the wire "
    "(a colliding enum literal, a wrong type, a missing required element) — keep the finding, set "
    "`category` to 'correctness' and prefix `why` with 'SCHEMA AMENDMENT REQUIRED:'. Make "
    "`suggested_fix` 'Stage a schema amendment: in <file> change <exact old text> to <exact new "
    "text>, then update the dependent Java.' The implementer stages it and a human decides.\n"
    "- Otherwise prefer a remedy that lives entirely in code: scope a check by message type, add "
    "a mapping/adapter layer, or constrain behaviour at the boundary rather than at the schema.\n"
    "- Do NOT re-raise a schema finding as STILL OPEN solely because the .xsd is unchanged when a "
    "prior round already staged an amendment for it or a human already ruled on it. Check the "
    "binding directives first; a decision already taken is not an open defect.")


@dataclass
class Finding:
    severity: str
    category: str
    why: str
    suggested_fix: str = ""
    file: str | None = None
    line: int | None = None
    blocking: bool = False
    done_when: str = ""            # checkable completion condition — what closes this finding


@dataclass
class ReviewFindings:
    findings: list[Finding] = field(default_factory=list)
    # True only when an AUTHOR-ACTIONABLE blocker exists — the signal that routes the run
    # back to a code round. reviewer_gaps below still hold the push (fail-closed) but must
    # never send the code agent chasing them: they describe a REVIEWER deficiency.
    blocking: bool = False
    reviewer_model: str = ""
    rounds: int = 1
    # Findings that are the REVIEWER's failure, not the author's: synthesized [Dn]
    # NOT-VERIFIED placeholders, the unparseable-output sentinel, and blocking findings
    # with no actionable content. Populated only when they SURVIVE the corrective judge
    # retry inside run_review — the caller routes these to human adjudication.
    reviewer_gaps: list[Finding] = field(default_factory=list)


def _is_reviewer_gap(f: Finding) -> bool:
    """A finding the CODE AGENT cannot act on because the deficiency is in the REVIEW, not
    the code (215ead25 post-mortem: 21 synthesized [Dn] NOT-VERIFIED blockers sent the
    author to 'fix' a reviewer formatting failure for two full rounds)."""
    why = f.why or ""
    if "NOT VERIFIED — the reviewer did not return a verdict" in why:
        return True                       # _directive_coverage synthesis (reviewer silence)
    if "review produced no parseable JSON findings" in why:
        return True                       # parse_findings sentinel (reviewer output broken)
    return f.blocking and not _actionable(f)


def _actionable(f: Finding) -> bool:
    """Deterministic floor for a blocking finding to count as a WORK ORDER: an ANCHOR —
    a file, or an explicit MISSING:-artifact in the why. Deliberately minimal: this
    exists to catch contentless placeholders (the 215ead25 synths had no anchor at all),
    not to grade prose quality — a file-anchored 'hardcoded token' with no suggested_fix
    is still perfectly fixable, and a false 'unactionable' would bounce a real defect
    back to the reviewer and delay its fix. suggested_fix/done_when are demanded by the
    OUTPUT contract (prompt-enforced) and by the corrective retry, not by this floor."""
    return bool(f.file) or "MISSING" in (f.why or "")[:200].upper()


# ── Diff rendering (read-only) ────────────────────────────────────────────────

def _render_diff(run_id: str, change_set, *, cap: int | None = _DIFF_CAP) -> str:
    """Human-readable diff of the agent's uncommitted edits, per repo. Tracked
    modifications/deletions come from `git diff`; created (untracked) files are
    rendered as new-file blocks (git diff omits untracked).

    Layout (order is load-bearing):
      1. CHANGE MANIFEST — every file in the change (op/path/bytes). Always complete,
         always first, so no truncation can ever make a file invisible: a checker that
         doesn't see a file inline still sees that it EXISTS and must read_file it
         before claiming it is missing (the Test-8 phantom-gap loop).
      2. NEW FILE blocks — new code is where the feature lives; it must survive the cap.
      3. Tracked `git diff HEAD` hunks (HEAD, not bare, so staged edits and tracked
         deletions are visible too).

    ``cap`` bounds the blob for LLM-prompt callers (the reviewer — which has read_file/grep
    tools to pull anything the inline diff omits, so a bounded prompt is safe). Pure
    deterministic checkers (acceptance predicates, contract gate) pass ``cap=None``: they grep
    the WHOLE change locally at no token cost, so a global truncation would silently drop
    deliverables that sort past the cut and manufacture phantom 'missing' gaps — a 30k cap hid
    ~75% of a 120k/25-file change and enforce-blocked a correct run."""
    by_repo: dict[str, list] = {}
    for op in change_set.operations:
        by_repo.setdefault(op.repo_id, []).append(op)

    # Manifest lines start with "#  " (never "# repo ", never containing " NEW FILE ") so the
    # deterministic parsers in acceptance_predicates/contract_gate can't mistake them for
    # section markers.
    manifest: list[str] = [
        "# CHANGE MANIFEST — every file in this change (ground truth, never truncated).",
        "# A file listed here but not shown inline below was omitted for size ONLY —",
        "# read_file it before judging; NEVER report a listed file/class as missing.",
    ]
    new_parts: list[str] = []
    mod_parts: list[str] = []
    for rid, ops in by_repo.items():
        rdir = workspace_local.repo_dir(run_id, rid)
        for op in ops:
            size = len(op.content) if getattr(op, "content", None) is not None else None
            manifest.append(f"#  {op.op:<7} [{rid}] {op.path}"
                            + (f"  ({size} bytes)" if size is not None else ""))
        res = adapter.run_command(rdir, ["git", "diff", "HEAD"])
        if res.stdout.strip():
            mod_parts.append(f"# repo {rid} (changes vs base)\n{res.stdout}")
        for op in ops:
            if op.op == "add" and op.content is not None:
                new_parts.append(f"# repo {rid} NEW FILE {op.path}\n{op.content}")
            elif op.op == "delete":
                new_parts.append(f"# repo {rid} DELETED {op.path}")
    blob = "\n".join(manifest) + "\n\n" + "\n\n".join(new_parts + mod_parts)
    if cap is not None and len(blob) > cap:
        return (blob[:cap]
                + "\n…[diff truncated — the CHANGE MANIFEST at the top lists EVERY file in this "
                  "change; read_file anything not fully shown inline before claiming it is "
                  "missing or incomplete]…")
    return blob


# ── Finding parsing ───────────────────────────────────────────────────────────

def _to_line(v) -> int | None:
    if isinstance(v, bool):              # bool is an int subclass — never a line
        return None
    if isinstance(v, (int, float)):      # JSON numbers may arrive as float (12.0)
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v)
    return None


def _coerce_finding(d: dict) -> Finding | None:
    if not isinstance(d, dict) or not d.get("why"):
        return None
    sev = str(d.get("severity", "warning")).lower()
    cat = str(d.get("category", "correctness")).lower()
    return Finding(
        severity=sev if sev in _SEVERITIES else "warning",
        category=cat if cat in _CATEGORIES else "correctness",
        why=str(d["why"]),
        suggested_fix=str(d.get("suggested_fix", "")),
        file=d.get("file") or None,
        line=_to_line(d.get("line")),
        blocking=bool(d.get("blocking", False)) or sev == "blocker",
        done_when=str(d.get("done_when", "") or ""),
    )


# The sentinel `why` of the parse-failure marker finding. The orchestrator keys its
# parse-stall loop-breaker off this exact string — keep them in sync.
UNPARSEABLE_WHY = "review produced no parseable JSON findings"


def parse_findings(text: str) -> list[Finding]:
    """Extract the findings JSON array from the model's output, reusing the shared
    LLM-JSON recovery (handles fences, trailing commas, and — crucially —
    brackets inside string values, which a hand-rolled bracket counter corrupts).

    A parse failure yields a single non-blocking 'unparseable' finding rather than
    a silent empty (clean) result, so a lost review is visible."""
    if not text:
        return []
    data = parse_llm_json_sync(text, expect_array=True, fallback=None)
    if data is None:
        return [Finding("info", "convention", UNPARSEABLE_WHY, blocking=False)]
    if not isinstance(data, list):
        return []
    return [f for f in (_coerce_finding(d) for d in data) if f]


def _is_unparseable(findings: list[Finding]) -> bool:
    return len(findings) == 1 and findings[0].why == UNPARSEABLE_WHY


async def _reextract_findings(review_text: str) -> list[Finding] | None:
    """Bounded salvage when the reviewer buried its verdict in prose: ONE cheap extraction
    call over the TAIL of the review (the verdict block is emitted last), strictly parsed.
    Returns a non-empty findings list or None — the caller keeps the unparseable marker on
    None/empty so a lost review stays visible (and counts toward the parse-stall breaker).
    Without this, every unparsed verdict synthesized [Dn] NOT-VERIFIED blockers and
    redispatched the CODE agent against a plumbing failure it cannot fix."""
    from app.core.json_recovery import parse_llm_json
    from app.core.llm import call_llm
    raw = await call_llm(
        system=("You extract structured verdicts. The user message is a code-review transcript "
                "that should end with a JSON array of finding objects. Return ONLY that JSON "
                "array, verbatim — no prose, no markdown fences. If the text contains no "
                "findings JSON at all, return []."),
        messages=[{"role": "user", "content": review_text[-24000:]}],
        max_tokens=8000, agent_name="review_verdict_extract")
    data = await parse_llm_json(raw, expect_array=True, fallback=None, llm_self_correct=False)
    if not isinstance(data, list) or not data:
        return None
    return [f for f in (_coerce_finding(d) for d in data) if f] or None


# ── Runner ────────────────────────────────────────────────────────────────────

def _persist(db, run_id: str, findings: list[Finding], reviewer_model: str, round_i: int) -> None:
    if db is None:
        return
    from app.models.agentic import ReviewFinding
    for f in findings:
        db.add(ReviewFinding(
            run_id=run_id, round=round_i, severity=f.severity, category=f.category,
            file=f.file, line=f.line, why=f.why, suggested_fix=f.suggested_fix,
            blocking=f.blocking, reviewer_model=reviewer_model,
        ))
    db.flush()


def _directive_coverage(findings: list[Finding], directives: list[str]) -> list[Finding]:
    """B1 enforcement: every '[D<n>]' directive must have an explicit verdict finding. A
    directive the reviewer never addressed gets a synthesized BLOCKING finding — silence
    on a money-movement contract item must never read as PASS."""
    if not directives:
        return findings
    addressed = set()
    for f in findings:
        for i in range(1, len(directives) + 1):
            if f"[D{i}]" in (f.why or ""):
                addressed.add(i)
    out = list(findings)
    for i, d in enumerate(directives, start=1):
        if i not in addressed:
            out.append(Finding(
                severity="blocker", category="directive",
                why=(f"[D{i}] NOT VERIFIED — the reviewer did not return a verdict for this "
                     f"binding directive: {d[:220]}"),
                suggested_fix="Re-review: verify this directive against the diff and return "
                              "an explicit [Dn] PASS (with file:line evidence) or FAIL finding.",
                blocking=True))
    return out


def _annotate_phantom_paths(findings: list[Finding], ws_run_id: str,
                            repo_ids: list[str], change_set) -> None:
    """Deterministic fabrication guard (grok-build parity): a finding that cites a file which
    exists neither in any selected repo's workspace nor in the change set is anchored to a
    path the reviewer may have hallucinated — the fix round would chase a phantom. Annotate
    (never demote: the defect may be real with a misremembered path) so the code agent and
    the human adjudicator see the anchor is unverified. Fail-open."""
    try:
        changed = {op.path for op in change_set.operations}
        for f in findings:
            if not f.file or f.file in changed:
                continue
            exists = False
            for rid in repo_ids or []:
                try:
                    if (workspace_local.repo_dir(ws_run_id, rid) / f.file).is_file():
                        exists = True
                        break
                except Exception:  # noqa: BLE001 — a missing repo dir must not kill the check
                    continue
            if not exists:
                f.why += (" [⚠ cited path not found in the workspace or the change set — "
                          "verify the real location before acting on this finding]")
    except Exception as e:  # noqa: BLE001 — annotation must never break the review verdict
        logger.debug("phantom-path annotation skipped: %s", e)


async def run_review(db, *, run_id: str, ctx, change_set, xsd_scope=None, intent: str = "",
                     plan_block: str = "", reviewer_model: str | None = None, round: int = 1,
                     cancel_check=None, workspace_run_id: str | None = None,
                     directives: list[str] | None = None,
                     prior_blockers: list[dict] | None = None,
                     resume_read_files: list | None = None,
                     preface: str | None = None, agent_name: str = "review",
                     max_tokens: int | None = None, code_phase: bool = False,
                     extra_tools: list | None = None,
                     progress=None) -> ReviewFindings:
    # `preface` replaces the default reviewer system preface (governance stages pass
    # their stage framing + the verbatim skill block); `agent_name` labels the loop's
    # events/usage so stage reviews are distinguishable from the codegen review;
    # `extra_tools` adds caller-supplied schemas on top of the read-only REVIEW_TOOLS
    # (governance BUNDLE stages hand in run_skill_script + the sandboxed bash so
    # SKILL.md's own procedure runs verbatim) — the read-only invariant still holds
    # because no caller may add an editing tool; see _tools_for below;
    # `max_tokens` raises the output cap for verdict-heavy calls (a governance batch
    # emits one [Dn] verdict per rule — AiNxt strips finish_reason, so the cap must
    # FIT the verdict rather than rely on truncation detection).
    # Dedicated reviewer model (different eyes than the author) when configured — the
    # explicit reviewer_model argument still wins, blank setting = author's model.
    model = (reviewer_model or (settings.agentic_reviewer_model or "").strip()
             or get_model(provider="claude"))
    if "claude" not in str(model).lower():
        # Non-Claude reviewer (different-vendor eyes): setting the model id IS the opt-in —
        # no separate enable flag. Works only via the AiNxt anthropic-compat path, whose
        # translation layer carries the tool loop for OpenAI upstreams; the llm.py
        # truncation-synthesis + gateway-error guards are the required mitigations
        # (docs/ainxt_messages_compat.md §5, all always-on).
        # AiNxt B5: an id its prefix router doesn't recognize SILENTLY routes to Claude —
        # the reviewer would run on the author's model with no error, defeating the whole
        # different-eyes goal. Require a recognized OpenAI prefix so misroutes fail loudly.
        # ("claude" as a substring covers direct ids and gateway/bedrock anthropic.claude-… ids.)
        if not str(model).lower().startswith(("gpt", "o1", "o3", "o4")):
            raise ValueError(f"reviewer model {model!r} is neither a claude id nor an AiNxt-"
                             "recognized OpenAI id (gpt/o1/o3/o4) — the gateway would silently "
                             "route it to Claude (docs/ainxt_messages_compat.md B5)")
        # The prefix check validates the ID; this validates the ROUTE. Under any other
        # provider (llm_provider=claude, or ainxt in openai-compat mode) the id passes the
        # guard above and then hard-fails deep in call_claude_tools / at api.anthropic.com
        # with a non-transient NotFoundError — fail loudly HERE with the actual reason.
        from app.core.llm import normalize_provider, _ainxt_uses_anthropic
        if not (normalize_provider(settings.llm_provider) == "ainxt" and _ainxt_uses_anthropic()):
            raise ValueError(f"reviewer model {model!r} requires llm_provider='ainxt' with "
                             "ainxt_compat_mode='anthropic' (the gateway's translation layer "
                             f"carries the tool loop for OpenAI upstreams); current provider "
                             f"{settings.llm_provider!r} would send this id to the Anthropic API "
                             "and fail. Unset agentic_reviewer_model or fix the provider config.")

    xsd = ""
    if xsd_scope and getattr(xsd_scope, "diff_record", None):
        xsd = "\n\nXSD element changes:\n" + wrap_untrusted(str(xsd_scope.diff_record), "XSD_DIFF")
    # The PLAN the change was supposed to implement — so the reviewer can catch a half-baked
    # change (planned item not delivered), not just bugs in what WAS written.
    plan = (f"\n\nThe change was REQUIRED to implement this plan — verify the diff fulfils every "
            f"item:\n{wrap_untrusted(plan_block, 'REQUIRED_PLAN')}") if plan_block else ""
    # B1 — binding directives: the reviewer MUST return one explicit verdict per directive.
    dir_block = ""
    if directives:
        dir_block = ("\n\nBINDING DIRECTIVES — for EACH one output exactly one finding whose `why` "
                     "STARTS with its tag: '[Dn] PASS — <file:line evidence the diff obeys it>' "
                     "(category 'directive', severity 'info', blocking false) or '[Dn] FAIL — "
                     "<file:line evidence of the violation>' (category 'directive', severity "
                     "'blocker', blocking true). The diff below is the AUTHORITATIVE record of "
                     "what changed — SEARCH IT FIRST before any claim that something was not "
                     "added/changed/wired; a hunk in the diff outranks any recollection or prior "
                     "tool result. Use read_file/grep to verify what the diff CANNOT show: "
                     "pre-existing unchanged code, surrounding context, and true absence. A code "
                     "comment CLAIMING compliance is not evidence. An unverifiable directive is a "
                     "FAIL, not a PASS:\n" + "\n".join(directives))
    # B2 — round continuity: verify the previous round's blockers are truly fixed FIRST.
    prior_block = ""
    if prior_blockers:
        # List up to 20 in full; NEVER silently drop the overflow — every entry here is a
        # blocker by construction (there is no "sort blockers first" to save us like the
        # orchestrator's [:15] item caps), so a dropped one would escape re-verification
        # and read as resolved-by-omission.
        lines = [f"- ({b.get('severity')}) {str(b.get('why') or '')[:300]}"
                 + (f" [{b.get('file')}]" if b.get("file") else "") for b in prior_blockers[:20]]
        if len(prior_blockers) > 20:
            lines.append(f"- …plus {len(prior_blockers) - 20} MORE blocking finding(s) not listed "
                         "for space — the persisted review findings hold the full set. EVERY "
                         "blocker from the previous round must be re-verified; none is resolved "
                         "by being absent from this list.")
        prior_block = ("\n\nPREVIOUS ROUND'S BLOCKING FINDINGS — verify EACH is actually fixed in "
                       "the current code. START from the diff below: it is the authoritative record "
                       "of what changed, so if the fix for a finding appears as a hunk in the diff, "
                       "that alone settles 'was it changed' — never re-raise a finding the diff "
                       "visibly addresses without reading the changed file to explain why the hunk "
                       "is insufficient. Do not trust fix-CLAIMS (prose). RE-GATHER the evidence "
                       "FRESH this round: a finding that claims "
                       "something is absent / never called / not wired must be re-verified by a NEW "
                       "grep across ALL repos NOW — the fix may live in a different repo, a "
                       "different file than the one cited, or below the line range you read. Never "
                       "carry forward a previous round's grep results, and never treat a partial "
                       "(ranged) read as evidence of absence. Genuinely fixed → do NOT re-report "
                       "it. Not fixed / partially fixed → re-raise it with `why` prefixed "
                       "'STILL OPEN:', the same severity, and the FRESH evidence you just "
                       "gathered. This round is VERIFICATION-SCOPED: beyond the prior blockers, "
                       "review ONLY the code changed since the previous round (the fix hunks and "
                       "anything they touch) — report new findings there, including regressions "
                       "the fix introduced. Do NOT re-sweep areas a prior full round already "
                       "passed and the fixes did not touch: a fresh finding in untouched, "
                       "already-reviewed code is a full extra fix+review cycle and belongs in "
                       "the FIRST round's exhaustive sweep, not here:\n"
                       + "\n".join(lines))
    # RESUME WITH MEMORY: this round was interrupted (crash / cancel / laptop-sleep lease loss)
    # and re-entered. The files the reviewer already grepped/read this round are handed back so it
    # skips the file-DISCOVERY sweep (where an interrupted round spent most of its turns) and goes
    # straight to verifying + the verdict — the read-only counterpart of the code phase's code_resume.
    resume_block = ""
    if resume_read_files:
        _paths = sorted({x[1] for x in resume_read_files if isinstance(x, (list, tuple)) and len(x) > 1})
        if _paths:
            _rendered = "\n".join(f"  - {p}" for p in _paths[:200])
            resume_block = ("\n\nRESUMED REVIEW — you already began this SAME review round but were "
                            "interrupted; these files you ALREADY explored (their content is unchanged):\n"
                            + _rendered
                            + "\nDo NOT re-discover the change from scratch — you know where the relevant "
                              "code lives. Go straight to VERIFYING the plan/directives and writing the "
                              "verdict; re-open a specific file only if you need its exact current text.")
    # Phase B reviews the SHARED tree under its Phase-A parent's id — rendering the
    # diff (or reading files) under the run's own id would hit a nonexistent dir.
    user = (f"{wrap_untrusted(intent, 'CHANGE_INTENT')}{plan}{xsd}{dir_block}{prior_block}{resume_block}"
            f"\n\nDiff under review:\n"
            f"{_render_diff(workspace_run_id or run_id, change_set)}"
            + _OUTPUT_RULES)

    # Judge-retry loop (215ead25 post-mortem / grok parity): a malformed or incomplete
    # VERDICT is the REVIEWER's failure to correct — never converted into implementer work.
    # Pass 1 reviews normally; if the verdict has reviewer-gaps (unparseable output,
    # unaddressed [Dn] directives, contentless blockers), pass 2 re-runs the review with
    # each deficiency named. One retry only — a judge that can't produce a usable verdict
    # twice routes to human adjudication via `reviewer_gaps`, not to a code round.
    # Phase-aware preface: only a CODE-phase review is bound by the schema freeze. A Phase-A
    # (schema-authoring) review must keep prescribing .xsd edits — that phase's whole job is
    # editing them. An explicit `preface` (governance stages) is never modified.
    _preface = preface or _REVIEW_PREFACE
    if preface is None and code_phase:
        _preface += _SCHEMA_LOCK_CLAUSE

    tools = _tools_for(extra_tools)   # resolved before the first call so a bad extra fails fast
    findings: list[Finding] = []
    for attempt in (1, 2):
        res = await run_agent_loop(
            run_id=run_id, selected_repo_ids=ctx.selected_repo_ids,
            system=build_system_segments(ctx, _preface), user_prompt=user,
            tools=tools, model=model, agent_name=agent_name,
            max_tokens=max_tokens,
            db=db, require_plan=False, cancel_check=cancel_check,
            workspace_run_id=workspace_run_id, progress=progress,
        )
        findings = parse_findings(res.final_text)
        if _is_unparseable(findings) and res.final_text:
            try:
                salvaged = await _reextract_findings(res.final_text)
            except Exception as e:  # noqa: BLE001 — salvage must never break the review
                logger.warning("review verdict re-extraction failed: %s", e)
                salvaged = None
            if salvaged:
                logger.info("review verdict salvaged from prose: %d finding(s)", len(salvaged))
                findings = salvaged
        findings = _directive_coverage(findings, directives or [])
        gaps = [f for f in findings if _is_reviewer_gap(f)]
        if not gaps or attempt == 2:
            break
        logger.warning("review round %d verdict has %d reviewer-gap(s) — corrective judge "
                       "retry (not a code round)", round, len(gaps))
        user = user + _corrective_block(gaps)
    _annotate_phantom_paths(findings, workspace_run_id or run_id, ctx.selected_repo_ids, change_set)
    _persist(db, run_id, findings, model, round)
    gaps = [f for f in findings if _is_reviewer_gap(f)]
    return ReviewFindings(
        findings=findings,
        # Loop-back signal: only AUTHOR-actionable blockers send the run to a code round.
        # Surviving reviewer_gaps still hold the push (they stay blocking in `findings`,
        # fail-closed) but the orchestrator routes them to human adjudication instead.
        blocking=any(f.blocking and not _is_reviewer_gap(f) for f in findings),
        reviewer_model=model,
        rounds=round,
        reviewer_gaps=gaps,
    )


def _corrective_block(gaps: list[Finding]) -> str:
    """The judge-retry addendum: name each verdict deficiency so the re-review fixes ITS
    OWN output — the counterpart of the corrective feedback the tool loop gives the code
    agent for malformed tool calls."""
    lines = []
    for f in gaps[:12]:
        if "NOT VERIFIED — the reviewer did not return a verdict" in (f.why or ""):
            tag = (f.why or "").split(" ", 1)[0]           # "[Dn]"
            lines.append(f"- You returned NO verdict for directive {tag}: verify it against the "
                         f"diff NOW and output an explicit '{tag} PASS — <file:line evidence>' or "
                         f"'{tag} FAIL — <evidence>' finding.")
        elif f.why == UNPARSEABLE_WHY:
            lines.append("- Your previous output was not a parseable JSON findings array. Output "
                         "ONLY the JSON array this time — no prose before or after it.")
        else:
            lines.append(f"- This blocking finding is not actionable — \"{(f.why or '')[:160]}\": "
                         "re-state it with the exact `file` (or \"MISSING: <artifact>\"), a concrete "
                         "`suggested_fix` the implementer can produce, and a checkable `done_when` — "
                         "or withdraw it if you cannot anchor it.")
    return ("\n\nYOUR PREVIOUS VERDICT WAS DEFICIENT — this is a corrective re-review of the SAME "
            "diff. Fix each deficiency below in your output (the code has NOT changed; do not "
            "invent new findings to justify the retry):\n" + "\n".join(lines))
