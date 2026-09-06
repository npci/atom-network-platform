# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""XSD-Discovery + Code-Change subagents (THE BOOK §4/§8/§9).

The phase *bodies* that turn the pieces into a pipeline: each builds a cached
system prompt from the S8 ContextPack, picks its tool subset, drives the S6
bounded loop, and returns a typed artifact —

    ContextPack ──xsd_discovery──▶ XsdScope ──code_change──▶ ChangeSet

XSD-Discovery is reuse-first (``find_existing_xsd`` before creating); judgement
is agentic but the record is deterministic — after the agent edits schemas we
recompute the S7 element-index diff so ``[NEW]/[MODIFIED]/[DEPRECATED]`` is a
fact, not the model's narration (§7.4). Both are Anthropic-only (§10).
"""
from __future__ import annotations
from app.core.domain.registry import prompt_block as _PB
from app.core.prompts import load_prompt, render_prompt

import logging
import re
from dataclasses import dataclass, field

from app.agents import xsd_graph_builder
from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.agents.agentic_runtime import run_agent_loop
from app.agents.agentic_tools import TOOL_SCHEMAS, FileOp
from app.core.llm import tool_result_block
from app.agents.context_assembler import ContextPack
from app.agents.platform_adapter import adapter
from app.core.config import settings
from app.core.prompt_blocks import segments_for_anthropic_cache

logger = logging.getLogger("app.agentic")

# Tool subsets (§4). XSD-Discovery is scoped to schema work + reuse lookup; the
# code-change agent gets the full set. Both come from the one S6 registry.
_XSD_TOOL_NAMES = {"submit_plan", "read_file", "grep", "glob", "find_existing_xsd", "schema_guardian",
                   "edit_file", "create_file", "flag_concern", "propose_revision", "read_doc",
                   "module_context", "flow_context", "code_search_semantic",
                   "callers", "impact_analysis", "jaxb_accessors", "symbol_graph", "ast_query",
                   "show_diff", "git_history", "record_fact"}
XSD_TOOLS = [t for t in TOOL_SCHEMAS if t["name"] in _XSD_TOOL_NAMES]
# The code-change agent IMPLEMENTS — it must NOT have the decision/gate tools
# (propose_approach / propose_revision), which belong to XSD discovery + the
# approach gate. With them in scope the agent can call propose_approach, which
# stops the loop (awaiting_decision) BEFORE making any edit → an empty change-set
# and a misleading "no change required". flag_concern is a refine-loop tool too.
_CODE_GATE_TOOLS = {"propose_approach", "propose_revision", "flag_concern",
                    # Analysis-stage gates (S2) — these set awaiting_decision and STOP the
                    # loop; if the code agent had them it would halt before any edit
                    # (empty change-set + misleading "no change required").
                    "ask_clarifications", "propose_plan"}
CODE_TOOLS = [t for t in TOOL_SCHEMAS if t["name"] not in _CODE_GATE_TOOLS]
# Propose-only pass: read/analysis tools + propose_approach, deliberately NO edit/create —
# the agent physically cannot create an API/XSD before the human picks an approach.
_PROPOSE_TOOL_NAMES = {"read_file", "grep", "glob", "find_existing_xsd", "code_search_semantic",
                       "domain_docs_search",
                       "module_context", "flow_context", "read_doc", "ast_query", "symbol_graph",
                       "callers", "impact_analysis", "jaxb_accessors", "git_history",
                       "propose_approach", "record_fact"}
PROPOSE_TOOLS = [t for t in TOOL_SCHEMAS if t["name"] in _PROPOSE_TOOL_NAMES]
# Change-Analysis pass (kind='analysis', accuracy upgrade S2): read-only discovery +
# the two analysis gate tools. Deliberately NO edit/create/delete, NO run_command, NO
# propose_approach/propose_revision — this agent reads everything and decides NOTHING;
# it asks the PM (ask_clarifications) and proposes the plan (propose_plan).
_ANALYSIS_TOOL_NAMES = {"read_file", "grep", "glob", "find_existing_xsd", "code_search_semantic",
                        "domain_docs_search",
                        "module_context", "flow_context", "read_doc", "ast_query", "symbol_graph",
                        "callers", "impact_analysis", "jaxb_accessors", "git_history",
                        "ask_clarifications", "propose_plan", "record_fact"}
ANALYSIS_TOOLS = [t for t in TOOL_SCHEMAS if t["name"] in _ANALYSIS_TOOL_NAMES]

# Force-seed sections matching these (compliance/safety) into the prompt rather than
# leaving them behind a pull the model might skip; the rest is outline + read_doc.
_SEED_RE = re.compile(r"regulat|complian|error[\s-]?code|security|mandat|validation", re.I)
_SEED_MAX_SECTIONS = 6
_SEED_MAX_CHARS = 6 * 1024

# Deep-research / narrative sections that are NOISE for CODE generation — they feed the Product
# Canvas (market/ecosystem/MVP/risk), not the implementation. Dropped from the code agent's doc
# context. KEEP-precedence (`_SEED_RE`) guards code-critical rules so an over-broad skip can't
# wipe a mandatory-validation / error-code section (the regression noted in context_assembler §9).
_DOC_RESEARCH_SKIP = re.compile(
    r"market\s+research|market\s+analysis|scalab|ecosystem|go[\s-]?to[\s-]?market|adoption|"
    r"\bmvp\b|minimum\s+viable|risk\s+assessment|risk\s+analysis|background|"
    r"current\s+(?:process|state)|as[\s-]?is|executive\s+summary|glossary|abbreviat|key\s+takeaway",
    re.I)


def _is_research_noise(heading: str) -> bool:
    """True if a doc section is deep-research narrative (drop it for code-gen). A section that also
    carries code-critical rules (compliance/error-code/validation/…) is KEPT — KEEP beats SKIP."""
    h = (heading or "").lower()
    return bool(_DOC_RESEARCH_SKIP.search(h)) and not _SEED_RE.search(h)

# Static domain coding standards appended to both prefaces (§9 — carve the
# static guidance and append; do not rewrite the legacy _build_system_prompt).
# These are conventions the generated code/schemas must follow. The scaffold is
# domain-neutral (JAXB/reuse/multi-repo are platform truth); the scope label,
# flow example and exact-quantity rule come from the active pack.
# Pack-driven vocabulary for the subagent prefaces (genericisation sweep).
# The prefaces' methodology (reuse-first, evidence discipline, blast radius) is
# platform truth; the domain name, message examples, participant list and
# canonical-term examples are pack content. UPI bytes are unchanged
# (prompt-snapshot-verified).
_DOMAIN_NAME = _PB("domain_name", "platform")
_SWITCH_LABEL = _PB("central_switch_label", "the network")
_MESSAGE_EXAMPLES = _PB("example_message_names", "the domain's existing messages")
_COUNTERPARTY_EXAMPLES = _PB("counterparty_examples", "an external counterpart")
_CANONICAL_TERM_EXAMPLES = _PB(
    "canonical_term_examples", '{"term":"<enum name>","value":"<EXACT value>"}')


def _default_flow_parties() -> str:
    from app.core.domain.contract import participants_of
    from app.core.domain.registry import get_active_pack

    labels = [pt.label for pt in participants_of(get_active_pack())]
    if not labels:
        return "the domain's actual participants"
    return " / ".join(labels) + "; list only the parties this flow actually involves"


_FLOW_PARTIES_NOTE = _PB("party_flow_participants_note", _default_flow_parties())
# Domain-flavoured example of a good per-hop note in a party-flow diagram.
# Empty (the neutral default) simply omits the example — an invented one
# would put another ecosystem's user journey in this domain's plans.
_HOP_NOTE_EXAMPLES = _PB("hop_note_examples", "")

_STANDARDS = render_prompt(
    "agents/agentic_subagents/standards.md",
    STANDARDS_SCOPE=f"{_PB('authority', 'the platform')} {_PB('domain_name', '')}".strip(),
    TXN_MESSAGE_EXAMPLE=_PB("primary_txn_message", "Req/Resp"),
    DOMAIN_AMOUNT_RULE=_PB(
        "domain_amount_rule",
        "Quantities the domain treats as exact (amounts, counts, durations) "
        "are integers — never floating point."),
)
# Self-healing mandate (§8): the agent owns recovery — using ITS OWN reasoning over the
# REAL output, not pre-baked heuristics. We give it the tools + full logs; it diagnoses.
_SELF_HEAL = load_prompt("agents/agentic_subagents/self_heal.md")
# Efficiency + context preservation (§4/§8) — user-requested, not diagnostic heuristics.
_EFFICIENCY = load_prompt("agents/agentic_subagents/efficiency.md")
_XSD_PREFACE = (
    "You are the XSD-Discovery agent. Work the schema graph REUSE-FIRST: call "
    "find_existing_xsd and read before creating, and prefer EXTENDING an existing "
    "schema over adding a new one. submit_plan first with reuse_decisions. Only "
    "edit XSD/.xjb files; never edit generated sources.\n\n"
    "CAPTURE THE FULL SCHEMA / PAYLOAD SURFACE HERE — any change to the message schema "
    "or to an API request/response payload is the SCHEMA's responsibility and MUST be "
    "modeled in the XSD in THIS phase: added/changed/removed request or response fields, "
    "complex/simple types, enums and value sets, cardinality (minOccurs/maxOccurs, "
    "optional vs required), attributes, and namespaces. If it crosses the wire to or from "
    "a partner, it belongs in the .xsd — make the edit here so the schema is the complete, "
    "shareable contract. (The Java/consumer wiring for those changes follows later, in the "
    "code phase — do not write it now.)\n\n"
    "NEVER INVENT A WIRE VALUE WITHOUT CHECKING IT FIRST. Before you add ANY new literal to the "
    "schema — an xs:enumeration value, a purpose/status/response code, a fixed attribute value — "
    "grep for that exact literal (quoted, e.g. grep '\"BG\"') across ALL selected repos and READ "
    "every hit. Wire codes live in Java constants files (CommonConstant and friends) as much as in "
    "the schema, so a value that looks unused in the .xsd can already be bound to a completely "
    "different meaning in code. If the literal is taken, pick a genuinely free one; if you must "
    "reuse it, say so explicitly in your summary with the reason. This is not optional and grep is "
    "cheap: an unchecked code value that collides ships a mislabeled transaction, and the schema is "
    "FROZEN after this phase — the code phase cannot correct your choice. (The platform also sweeps "
    "every added enum literal automatically and shows the human what it found, so an unchecked "
    "collision will be visible at the approval gate regardless.)\n\n"
    "KNOW THE BLAST RADIUS BEFORE A BREAKING CHANGE. Renaming an element, changing its cardinality "
    "(single→repeating flips getX() into List<X> getXs()), or changing its type BREAKS every existing "
    "Java consumer. Before such a change, enumerate those consumers — jaxb_accessors() for the "
    "element's real accessors, then callers()/symbol_graph across ALL repos for who uses them. If "
    "in-use consumers would break, prefer EXTENDING (add a new optional element) over a breaking "
    "edit, and call propose_revision to surface the safer option to the human.\n\n"
    "Pin load-bearing findings with record_fact the moment you learn them (an existing "
    "value/enum binding with its file, a verified constraint, a human decision) — the sheet "
    "stays in your context all run; never re-derive a recorded fact from memory."
    + _STANDARDS + _SELF_HEAL + _EFFICIENCY
    + "\n\n" + ANTI_INJECTION_CLAUSE
)
_COMPLETENESS = load_prompt("agents/agentic_subagents/completeness.md")
# The decided package is ONE coherent spec — the PM's answered CLARIFICATIONS, the XSD changes
# already applied in Phase A, and the APPROVED PLAN were all ratified together for THIS change.
# Stating the authority order stops the agent silently arbitrating between layers (or re-deriving
# from the BRD/TSD), and gives it an escape hatch instead of blindly shipping a plan the code
# contradicts. This is precedence/coherence, not extra context.
_AUTHORITY = load_prompt("agents/agentic_subagents/authority.md")
# SDLC review gaps 1/2/3/6 — a citable, priority-ordered conflict-resolution rule for the
# flat, unordered directives below (completeness/standards/contract-discipline/self-heal/
# efficiency previously had no tie-breaker when two conflicted). Swappable prompt file
# (same pattern as docgen's architecture_principles.md) so a fork can restate its own
# priority order without a code change; empty file is a valid choice (no-op).
_PRIORITY_ORDER = load_prompt("agents/agentic_subagents/priority_order.md")
# Prefer the code GRAPH over text search (gated by agentic_prefer_code_intelligence_prompt).
# The backbone tools are always offered and the intel gate already blocks blind .java edits;
# this just steers the agent to reach for them PROACTIVELY instead of grepping its way to an
# answer the graph already has (and missing inheritance/cross-repo consumers grep can't see).
_INTELLIGENCE = load_prompt("agents/agentic_subagents/intelligence.md")
# Data-flow correctness disciplines (ALWAYS ON). The two bug classes that survived six review
# rounds on cbabbf9c and that the call-graph tools cannot see (they index method-call edges, not
# string-keyed state, and exempt brand-new files): a field READ that nothing writes, and a
# declared code that is never EMITTED. Prompt-only nudge; contract_gate catches these deterministically.
_CONTRACT_DISCIPLINE = load_prompt("agents/agentic_subagents/contract_discipline.md")
# Pre-commit to the expected build outcome (gated by agentic_record_verify_expectation).
# Devin's finding: committing to the expectation up-front makes it much harder to rationalize
# a failing build as not-your-problem. Prompt-only nudge; the deterministic verdict is
# unchanged — this only shapes how the agent reasons about a failure.
_EXPECTATION = load_prompt("agents/agentic_subagents/expectation.md")
_CODE_PREFACE = (
    "You are the Code-Change agent. submit_plan first (with reuse_decisions), read before "
    "you edit, and reuse existing code over adding new. Make the smallest change that FULLY "
    "implements the intent end-to-end — not the smallest change that merely compiles. "
    "To orient quickly, consult flow_context (the flow map — which API carries the money/"
    "transaction leg) and module_context for any module you intend to touch: these are "
    "generated, possibly-stale hints that point you at the right code FAST — treat them as "
    "leads to verify against the real source, not as ground truth."
    + _AUTHORITY + _COMPLETENESS + _STANDARDS + _CONTRACT_DISCIPLINE + _SELF_HEAL + _EFFICIENCY
    + _PRIORITY_ORDER
    + (_INTELLIGENCE if settings.agentic_prefer_code_intelligence_prompt else "")
    + (_EXPECTATION if settings.agentic_record_verify_expectation else "")
    # E1 — staged generation: the whack-a-mole evidence (each fix round minting new bugs) traces
    # to writing the whole feature in one pass, then patching under review pressure. Stages force
    # a compile-verified foundation before the hard wiring goes on top of it.
    + "\n\nSTAGED GENERATION. Implement in dependency order, and verify_change (compile) after "
      "EACH stage before starting the next — never write the whole feature and compile once: "
      "(1) state/data layer (session/state services, enums, config keys); "
      "(2) validation + message assembly (validators, builders — with amounts/fields wired); "
      "(3) core flow wiring (controller endpoints, listener cases, handlers); "
      "(4) terminal/atomic transitions + expiry/schedulers; "
      "(5) tests for each binding directive and the legacy-path regression. "
      "A stage that fails to compile gets fixed BEFORE the next stage begins."
    + "\n\n" + ANTI_INJECTION_CLAUSE
)


def _patterns_clause() -> str:
    """D1 — golden implementation patterns, injected as NORMATIVE for the code agent. Read at
    call time from ``<knowledge_base_dir>/agent_patterns.md`` (operator-curated; per-deployment)
    so pattern fixes apply to the next run without a redeploy. Empty/missing file → no-op.
    Rationale: the Test-8 fix rounds traded one reactive-orchestration bug for another for five
    rounds because the agent re-invented the dispatch/persist pattern each time instead of
    copying the house pattern."""
    try:
        from pathlib import Path
        p = Path(settings.knowledge_base_dir) / "agent_patterns.md"
        text = p.read_text(encoding="utf-8").strip() if p.is_file() else ""
        if not text:
            return ""
        return ("\n\nGOLDEN IMPLEMENTATION PATTERNS (NORMATIVE — for any integration point these "
                "cover, follow the pattern EXACTLY; a deviation requires an explicit justification "
                "comment citing why the pattern cannot apply):\n" + text[:6000])
    except Exception:  # noqa: BLE001 — prompt enrichment must never break the run
        return ""


def _tests_clause() -> str:
    # The agent authors tests when EITHER the legacy AGENTIC_WRITE_UNIT_TESTS flag is on OR the WS3a
    # feature-test gate is enabled (AGENTIC_REQUIRE_FEATURE_TESTS) — the gate FAILS a behavioural change
    # that ships no test, so the agent MUST be allowed to write one or it cannot self-heal. Read at call
    # time so a config flip applies to the next run without a redeploy.
    if settings.agentic_write_unit_tests or getattr(settings, "agentic_require_feature_tests", False):
        return ("\nWrite or extend JUnit tests covering the behaviour you changed — and for any new "
                "VALIDATION or REJECTION rule, include a NEGATIVE-PATH test (an invalid/rejected input "
                "asserting the exact error), not just the happy path. Follow the module's existing test "
                "conventions. This is part of definition-of-done: a behavioural change that ships no test "
                "fails the feature-test gate and loops back.\n"
                "INVARIANT TESTS FOR BINDING DIRECTIVES: for EVERY money-movement / atomicity / "
                "ordering directive in the plan's BINDING DIRECTIVES, write at least one test that "
                "asserts the invariant itself — e.g. sum of merchant credits for a session equals the "
                "original total (money conservation), a participant share is credited exactly once, "
                "the terminal response dispatches exactly once under concurrent completion, COMPLETE "
                "and CANCELLED are mutually exclusive, and untouched legacy paths still behave "
                "identically (a regression test on the non-feature path). These invariant tests are "
                "the machine-verified proof the directives are obeyed.\n"
                "TSD TRACEABILITY: for every test you write that verifies a claim the Tech Spec makes "
                "(an API contract, error code, state transition, validation rule, or config-driven "
                "behaviour), add a one-line comment `// tsd-ref: <the TSD section/heading it verifies>` "
                "immediately above the test method — this is how test coverage of the TSD is measured; "
                "a test with no TSD-derived claim behind it does not need this marker.")
    return ("\nDo NOT create or modify unit-test files (src/test/**) in this run — tests "
            "are handled separately. Spend the effort on the production change; "
            "verification means the build compiles, not new tests.")
# Reuse-first decision pass (THE BOOK v3.4): map the existing flows, decide where this
# fits, present OPTIONS for a human — DO NOT create anything. Deliberately does NOT name
# any specific API: the agent must discover the transaction-carrying flow itself.
_PROPOSE_PREFACE = (
    "You are the Solution-Architecture agent for a " + _DOMAIN_NAME + " system. A requirement has been given. "
    "Before ANY schema or code is created, MAP how the existing system already works. START by "
    "calling flow_context for each repo (the index first, then pull specific flows by name) to "
    "get the index-time flow map (which API carries the transaction leg vs the meta APIs). The "
    "map is a LOOKUP AID, not truth: it may be stale or INCOMPLETE — verify what it claims, and "
    "actively check the code for flows it does NOT mention. Deepen it with "
    "find_existing_xsd, code_search_semantic, module_context, grep and read_file "
    "to discover (a) which existing API actually carries the financial transaction — the leg where "
    "the real debit/credit happens — versus (b) the metadata/initiation/status APIs around it, and "
    "(c) how multi-leg flows are composed. Money movement almost always belongs in the EXISTING "
    "transaction API; only the surrounding meta APIs may differ. VERIFY this against the code — do "
    "not assume, and do not invent a new API by default.\n\n"
    "Then decide: can this requirement RIDE an existing flow (reuse/extend), or does it genuinely "
    "need a new one? Call propose_approach with 2-3 CONCRETE options. At least one option MUST be a "
    "fit-into-existing-flow option that names the specific existing API/flow it rides (e.g. route the "
    "money leg through the existing transaction flow, adding only a thin initiation/eligibility meta "
    "API) — describe how the requirement maps onto that flow and the tradeoffs. Mark ONE recommended, "
    "and PREFER reuse/extend whenever the code supports it: spinning up a parallel API + controller + "
    "state machine for something an existing flow can carry is the failure mode to avoid.\n\n"
    "NAME THE REAL CONSUMERS. For a reuse/extend option, call callers() on the handler that carries "
    "the transaction leg to show which existing code actually rides that flow — a reuse option must "
    "point at real consumers, not a flow name. Justify a 'new' option only when callers()/the code "
    "show NO existing flow fits.\n\n"
    "PLAN ALIGNMENT: a ratified analysis PLAN may be provided below — it already recommended a "
    "direction. For EACH option set `diverges_from_plan`: 'yes' ONLY if the option CONTRADICTS that "
    "recommendation (e.g. plan recommended a new API but this rides an existing flow, or vice-versa), "
    "else 'no'. When 'yes', `divergence_note` MUST state in one plain sentence what the plan "
    "recommended vs what this option does AND why this option is still worth choosing. You may still "
    "recommend a diverging option (reuse-first often beats the plan's first guess) — just flag it "
    "honestly. If NO plan is provided, set every diverges_from_plan='no'.\n\n"
    "GROUNDING (mandatory): propose_approach REQUIRES `evidence` — ≥2 citations to files you ACTUALLY "
    "read_file'd this run (the handler/flow carrying the debit-credit leg + a consumer or message). "
    "The tool REJECTS citations to files you didn't open and will not record an ungrounded proposal — "
    "so READ the real flow first (find_existing_xsd → grep the transaction leg → read_file the handler "
    "and its consumers), then cite exactly those files. This codebase is large and confusing; a "
    "decision you can't cite from code you read is a guess. Do NOT create, edit, or generate any schema "
    "or code in this pass. Stop after propose_approach; a human chooses."
    + _STANDARDS
)
# Phase-A (XSD stage) propose pass: the question is ONLY about the UPI XML schemas, asked
# in plain language for a product owner — never code architecture, never a new service.
_PROPOSE_PREFACE_XSD = (
    "You are helping a PRODUCT OWNER (not a developer) decide one thing: what changes, if any, "
    "do the " + _DOMAIN_NAME + " XML message schemas (XSDs) need for this requirement?\n\n"
    # No assessment instruction here: the prior document-only assessment exists only on
    # legacy (non-agentic) changes, so ordering the fetch unconditionally sent every run to
    # read_doc for a document that isn't there. _docs_block already advertises it — and only
    # when ctx.assessment_sections is non-empty.
    "First VERIFY against the indexed code and flow map (flow_context, find_existing_xsd, "
    "code_search_semantic, grep, read_file). The flow map may be incomplete; check for flows "
    "it doesn't mention.\n\n"
    "Then call propose_approach with 2-3 options drawn ONLY from these schema choices:\n"
    "- approach 'reuse': NO schema change — the requirement rides the existing " + _DOMAIN_NAME + " messages "
    "unchanged (name them, e.g. " + _MESSAGE_EXAMPLES + ");\n"
    "- approach 'extend': add optional elements/attributes to an EXISTING message schema;\n"
    "- approach 'new': a genuinely new message type needs a new schema (rare — justify hard).\n\n"
    "PLAN ALIGNMENT: a ratified analysis PLAN may be provided below — it already recommended a "
    "schema direction. For EACH option set `diverges_from_plan`: 'yes' ONLY if the option "
    "CONTRADICTS that recommended direction (plan said NEW but this reuses/extends, or plan said "
    "reuse but this adds a new schema), else 'no'. When 'yes', `divergence_note` MUST state in one "
    "plain sentence what the plan recommended vs what this option does AND why this option is still "
    "worth choosing. You may still recommend a diverging option (reuse-first often beats the plan's "
    "first guess) — just flag it honestly. If NO plan is provided, set every diverges_from_plan='no'.\n\n"
    "STRICT RULES: (1) Do NOT offer implementation/architecture choices — schedulers, queues, "
    "storage, durability, retries mechanics, refactors — those are decided later in the code "
    "phase, inside the existing services. (2) NEVER propose a new service/microservice. "
    "(3) Option TITLES and the summary must be PLAIN LANGUAGE a business user understands — "
    "" + _DOMAIN_NAME + " message names are fine; class/file/infra names are NOT (put any technical evidence in "
    "how_it_fits/tradeoffs, which stay collapsed in the UI). (4) summary = 1-3 short sentences. "
    "Mark ONE option recommended. (5) GROUNDING: propose_approach REQUIRES `evidence` — ≥2 citations "
    "to message/XSD/handler files you ACTUALLY read_file'd this run that justify the schema decision; "
    "the tool rejects citations to files you didn't open. READ the relevant message schemas + their "
    "consumers before deciding. Do NOT create or edit anything in this pass; stop after "
    "propose_approach — the human chooses." + _STANDARDS
)
# Refine-loop guardrail (THE BOOK v3.4): when the human requests changes to the generated XSDs,
# the DEFAULT is COMPLY — the human owns the schema. Push back (propose_revision) only on a
# change that genuinely BREAKS something, and only once; an explicit request here supersedes
# earlier gate decisions, including a ratified reuse/extend/new approach choice. (The prod
# 58ab724c loop: three refine rounds refusing an ADDITIVE new-schema request as "contradicting
# the ratified reuse decision" — that is the agent enforcing its opinion, not safety.)
_REFINE_GUARDRAIL = load_prompt("agents/agentic_subagents/refine_guardrail.md")
# The human picked one of the agent's OWN safer alternatives — the conversation is over.
# Injected (instead of the guardrail) on via_revision rounds so the agent implements the
# settled choice rather than re-litigating it (the prod dead-end: conditional alternatives
# chosen → agent stops again asking for the inputs its own option demanded).
_ALTERNATIVE_CHOSEN = load_prompt("agents/agentic_subagents/alternative_chosen.md")
# Explicit human override: the human saw the danger and chose to proceed anyway.
_RISK_ACCEPTED = load_prompt("agents/agentic_subagents/risk_accepted.md")


@dataclass
class XsdScope:
    decisions: list = field(default_factory=list)        # reuse|extend|new + rationale (from the plan)
    edits_applied: list[str] = field(default_factory=list)   # "repo_id:path"
    created: list[str] = field(default_factory=list)     # "repo_id:path" of files this round ADDED
    diff_record: dict = field(default_factory=dict)      # "repo_id:path" -> {new, modified, deprecated}
    java_links: list = field(default_factory=list)       # existing element→Java links (advisory)
    determinism_ok: bool = True
    # Occupancy verdict for every enum literal this round ADDED to a schema (advisory,
    # server-executed git grep). {} when the round added none. See enum_occupancy_report.
    enum_occupancy: dict = field(default_factory=dict)
    final_text: str = ""
    concerns: list = field(default_factory=list)         # disruptive changes the agent declined (refine loop)
    # Set when the refine pass STOPPED to converse: the human's request was disruptive and
    # the agent proposed safer alternatives (propose_revision). No edits were applied.
    revision_proposal: dict | None = None


def xsd_scope_to_dict(scope: "XsdScope") -> dict:
    """JSON-safe snapshot of an XsdScope for the Phase-A → Phase-B handoff
    (``AgenticRun.handoff_json``). All fields are already JSON primitives."""
    return {
        "decisions": list(scope.decisions or []),
        "edits_applied": list(scope.edits_applied or []),
        "created": list(scope.created or []),
        "diff_record": dict(scope.diff_record or {}),
        "java_links": list(scope.java_links or []),
        "determinism_ok": bool(scope.determinism_ok),
        "enum_occupancy": dict(scope.enum_occupancy or {}),
        "final_text": scope.final_text or "",
    }


def xsd_scope_from_dict(d: dict | None) -> "XsdScope":
    """Rebuild an XsdScope from a persisted handoff snapshot (Phase B resume).
    Tolerant of a missing/partial record — returns an empty scope rather than raising."""
    d = d or {}
    return XsdScope(
        decisions=list(d.get("decisions") or []),
        edits_applied=list(d.get("edits_applied") or []),
        created=list(d.get("created") or []),
        diff_record=dict(d.get("diff_record") or {}),
        java_links=list(d.get("java_links") or []),
        determinism_ok=bool(d.get("determinism_ok", True)),
        enum_occupancy=dict(d.get("enum_occupancy") or {}),
        final_text=d.get("final_text") or "",
    )


@dataclass
class ChangeSet:
    operations: list[FileOp] = field(default_factory=list)
    plan: dict | None = None
    reused: list = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    stopped: str = "completed"      # "completed" | "max_iterations" | "cancelled"
    iterations: int = 0
    read_files: list = field(default_factory=list)   # (repo_id, path) explored this batch
    final_text: str = ""            # the agent's closing notes — carried as memory into the next round
    decision_request: dict | None = None   # A3 — ask_decision payload; parks the run for a human answer
    # Code-phase schema writes captured as amendment proposals instead of applied (fix 2).
    # Non-empty ⇒ park at awaiting_schema_amendment so a human rules on the exact hunk.
    schema_amendments: list = field(default_factory=list)
    transcript: list = field(default_factory=list)   # full conversation — replayed so the next round
                                                     # keeps the author's memory (see run_code_change)
    # SDLC review gaps 7/8/9/11 — structural-intel tokens queried this batch
    # (RuntimeResult.intel_queried passthrough), consumed by the cross-module
    # analysis gate to check whether a shared symbol this round touched had
    # callers()/impact_analysis()/symbol_graph() called on it first.
    intel_queried: list = field(default_factory=list)


# ── Prompt assembly ───────────────────────────────────────────────────────────

def _sections_block(title: str, sections: dict) -> str:
    if not sections:
        return ""
    return f"## {title}\n" + "\n\n".join(f"### {h}\n{b}" for h, b in sections.items())


def _module_block(ctx: ContextPack) -> str:
    if not ctx.module_notes:
        return ""
    return ("## Module index — names only; pull a module's full context with "
            "module_context(module=…)\n" + "\n\n".join(
                f"### repo {rid}\n{notes}" for rid, notes in ctx.module_notes.items()))


def _doc_outline(label: str, sections: dict) -> str:
    heads = [h for h in (sections or {}) if not _is_research_noise(h)]
    if not heads:
        return ""
    return f"### {label} outline\n" + "\n".join(f"- {h}" for h in heads)


def _intent_terms(intent: str) -> set[str]:
    return {t for t in re.findall(r"\w+", (intent or "").lower()) if len(t) > 3}


def _seed_sections(ctx: ContextPack) -> str:
    """Full body of compliance/regulatory sections + sections whose heading matches the intent —
    never left behind a pull (§hybrid). Compliance/regulatory/error-code/security/mandate/validation
    (`_SEED_RE`) is seeded FIRST so the shared budget can never drop a MANDATORY section in favour of
    an intent match; the overflow remains reachable as outline + read_doc. Capped to avoid re-bloating."""
    terms = _intent_terms(ctx.intent)
    pairs = [(label, h, body)
             for label, secs in (("BRD", ctx.brd_sections), ("Tech Spec", ctx.tsd_sections))
             for h, body in (secs or {}).items() if not _is_research_noise(h)]   # never seed research narrative
    compliance = [(l, h, b) for (l, h, b) in pairs if _SEED_RE.search(h.lower())]
    intent_only = [(l, h, b) for (l, h, b) in pairs
                   if not _SEED_RE.search(h.lower()) and any(t in h.lower() for t in terms)]
    out: list[str] = []
    used = 0
    skipped: list[str] = []
    for label, h, body in compliance + intent_only:     # compliance first; intent matches fill the remainder
        if len(out) >= _SEED_MAX_SECTIONS:
            skipped.append(f"{label}: {h}")
            continue
        block = f"### {label} — {h}\n{wrap_untrusted(body, f'{label.upper()}_SECTION')}"
        if used + len(block) > _SEED_MAX_CHARS:
            # CONTINUE, not break: one oversized compliance section must not starve every
            # later (smaller, still mandatory) section out of the seed.
            skipped.append(f"{label}: {h}")
            continue
        out.append(block)
        used += len(block)
    if skipped:
        out.append("(seed budget reached — these matched but are NOT shown; consult each via "
                   "read_doc(heading=…) before finishing: " + "; ".join(skipped[:12])
                   + (" …" if len(skipped) > 12 else "") + ")")
    return "\n\n".join(out)


def _docs_block(ctx: ContextPack) -> str:
    """Pull-based docs: a section OUTLINE for BRD+TSD + a force-seed of compliance/
    intent-relevant sections. Everything else is fetched on demand via read_doc.
    The assessment/plan pointers render even with NO BRD/TSD outline (quick-start and
    legacy changes) — an early return here silently hid an available assessment."""
    outline = "\n\n".join(p for p in (_doc_outline("BRD", ctx.brd_sections),
                                      _doc_outline("Tech Spec", ctx.tsd_sections)) if p)
    parts: list[str] = []
    if outline:
        parts += ["## Requirements (BRD / Tech Spec) — outline only; fetch any section's "
                  "full text with read_doc(heading=…) or search with read_doc(query=…)", outline]
    if ctx.assessment_sections:
        # Pointer only — pull-based + advisory, so the agent consults it without
        # inheriting a document-only conclusion (it must verify against code).
        parts.append("A prior DOCUMENT-ONLY XSD assessment exists for this change "
                     "(written before any code was examined). ADVISORY orientation only — "
                     "pull it with read_doc(doc='assessment'), verify its claims against "
                     "the code, and do NOT inherit its conclusion.")
    if getattr(ctx, "plan_sections", None):
        parts.append("The FULL ratified Change-Analysis plan is available via "
                     "read_doc(doc='plan') — use it whenever a plan digest in your prompt "
                     "says CLIPPED/OMITTED.")
    if outline:
        seed = _seed_sections(ctx)
        if seed:
            parts += ["## Key sections included up-front (compliance + intent-relevant)", seed]
    return "\n\n".join(parts)


def _stale_banner(ctx: ContextPack) -> str:
    stale = [rid for rid, s in ctx.stale_index.items() if s]
    return ("⚠ Index may be STALE for: " + ", ".join(stale) +
            " — index-derived results are advisory; confirm with read_file.") if stale else ""


_CENSUS_MAX_EXTS = 14


def _census_block(ctx: ContextPack) -> str:
    """Render `ctx.file_census` — every extension actually present per repo.

    The agent chooses its own search patterns, and an omission is invisible to it:
    a sweep of .java/.xml/.properties/.yml/.yaml reads as exhaustive while silently
    skipping the repo's only .xsd and .md. Stating the inventory makes the gap
    visible without telling the agent WHICH extensions matter — that judgement stays
    with the model, and nothing here names a language, schema format or domain.

    Rarest-first: the long tail is where the single-file contracts and READMEs live,
    and a head-first list buries them under thousands of source files.
    """
    census = getattr(ctx, "file_census", None) or {}
    if not census:
        return ""
    label = {r["id"]: (r.get("label") or r["id"]) for r in (getattr(ctx, "repos", None) or [])}
    lines = []
    for rid, counts in census.items():
        ranked = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]))
        shown = ranked[:_CENSUS_MAX_EXTS]
        tail = len(ranked) - len(shown)
        row = ", ".join(f"{ext} ×{n}" for ext, n in shown)
        if tail > 0:
            row += f", (+{tail} more extensions)"
        lines.append(f"- {label.get(rid, rid)}: {row}")
    return ("Files present in the clones, by extension (rarest first — this is the COMPLETE "
            "inventory, counted from the checkout):\n" + "\n".join(lines) +
            "\nYour searches must account for every extension listed. A one-file extension is "
            "usually a contract, schema or spec, not noise — the fact that it is rare is why it "
            "is easy to miss and expensive to get wrong. Before you conclude, check that you "
            "opened, or can justify skipping, each class of file above.")


def build_system_segments(ctx: ContextPack, preface: str) -> list[dict]:
    """Cached Anthropic system segments. ≤3 cacheable blocks (role / module
    orientation / docs) so the tools cache breakpoint (S5) still fits the budget."""
    docs = _docs_block(ctx)
    roster = getattr(ctx, "repos", None) or []
    if roster:
        _ROLE_HINT = {"core": "framework/XSDs — schemas + shared types live here; builds first",
                      "app": "business logic/flows — consumes the core repo's artifacts",
                      "legacy": "legacy"}
        repos = "Selected repos:\n" + "\n".join(
            f"- {r['id']} · {r.get('label') or '?'} · role={r.get('role') or 'app'}"
            f" ({_ROLE_HINT.get(r.get('role') or 'app', '')})"
            for r in roster)
        if len(roster) > 1:
            repos += ("\nThe change likely SPANS repos: schema/type definitions in the core repo, "
                      "consumers in the app repo(s). Search ALL repos (omit repo_id in "
                      "grep/glob/find_existing_xsd) before concluding something doesn't exist.")
    else:
        repos = "Selected repos: " + ", ".join(ctx.selected_repo_ids)
    impact = ("Likely-affected files (advisory):\n- " + "\n- ".join(ctx.impact_files)
              ) if ctx.impact_files else ""
    return segments_for_anthropic_cache([
        (preface, True),
        (_module_block(ctx), True),
        (docs, True),
        ("\n".join(p for p in (repos, _census_block(ctx), impact, _stale_banner(ctx)) if p), False),
    ])


def _xsd_user_prompt(ctx: ContextPack, intent: str, decision: dict | None = None,
                     change_request: str | None = None, accepted_risk: bool = False,
                     required_schema: str = "", via_revision: bool = False) -> str:
    base = (f"Change intent:\n{wrap_untrusted(intent, 'CHANGE_INTENT')}\n\n"
            "Discover the XSD scope for this change. Reuse-first; submit_plan with "
            "reuse_decisions, then apply the schema edits.\n"
            "Scope: edit ONLY .xsd/.xjb schema files in this phase. Do NOT touch Java, "
            "validators, or any other code — those consumer changes are made later in the "
            "code phase, after the human approves the schema.")
    if decision:
        # Apply mode: the human picked an approach at the gate — implement exactly that.
        chosen = decision.get("custom_direction") or _format_decision(decision)
        base += ("\n\n✅ The human chose this approach — implement EXACTLY this; reuse/extend the "
                 "named existing API/XSD as decided, and only create new schema if the choice says "
                 f"so:\n{wrap_untrusted(str(chosen), 'HUMAN_DECISION')}")
    if required_schema:
        # The ratified plan lists CONCRETE .xsd deliverables. A 'reuse' approach chooses the API
        # TOPOLOGY (ride an existing message, don't add a new one) — it does NOT waive schema
        # EXTENSIONS the plan requires on that existing message. Without this the 'reuse = no
        # schema change' framing above silently zeroed a run's schema phase (0 files), and the
        # code phase then illegally rewrote the .xsd to compensate. Make the plan authoritative for
        # schema, with an evidence-gated escape hatch so a truly-already-satisfied item isn't
        # spuriously re-edited (e.g. an enum value that genuinely already exists).
        base += ("\n\n⚠ REQUIRED SCHEMA — from the ratified plan; MANDATORY and NOT waived by the "
                 "approach choice. A 'reuse' decision means do NOT introduce a NEW API/message "
                 "type; it does NOT exempt these extensions to the EXISTING schema. Apply every "
                 "one. If a specific item is genuinely ALREADY satisfied by the current schema "
                 "(e.g. the enum value already exists), you may skip THAT item — but only if you "
                 "state so in your plan with the exact file+line evidence proving it:\n"
                 f"{wrap_untrusted(required_schema, 'REQUIRED_SCHEMA')}")
    if change_request:
        base += f"\n\n📝 The human reviewed your XSDs and requested changes:\n{wrap_untrusted(change_request, 'CHANGE_REQUEST')}"
        base += _RISK_ACCEPTED if accepted_risk else (_ALTERNATIVE_CHOSEN if via_revision
                                                      else _REFINE_GUARDRAIL)
    return base


def _format_decision(decision: dict) -> str:
    opt = decision.get("option") or {}
    if opt:
        return (f"[{opt.get('approach', '?')}] {opt.get('title', opt.get('id', ''))} → "
                f"{opt.get('target_api', '')}\n{opt.get('how_it_fits', '')}")
    return decision.get("selected_option_id") or "(reuse-first, per the approved approach)"


def _approach_block(approach: dict | None) -> str:
    """The human's reuse-vs-new decision, injected into the code prompt so Phase B
    implements EXACTLY the chosen path and never drifts to a rejected alternative."""
    if not approach:
        return ""
    opt = approach.get("option") or {}
    title = opt.get("title") or approach.get("custom_direction") or "(chosen approach)"
    lines = [f"\n\n── Human's approach decision (binding) ──\nChosen: {wrap_untrusted(title, 'CHOSEN_APPROACH')}"]
    # WHY this approach was chosen — the rationale the human saw, not just the title, so the
    # implementer rides the same flow for the same reason (and can resolve ambiguity itself).
    why = " ".join(s for s in (opt.get("how_it_fits"),
                               (f"(rides {opt['target_api']})" if opt.get("target_api") else ""),
                               opt.get("tradeoffs")) if s).strip()
    if why:
        lines.append("Why it fits: " + wrap_untrusted(why[:700], "APPROACH_RATIONALE"))
    if approach.get("directive"):
        lines.append(wrap_untrusted(approach["directive"], "APPROACH_DIRECTIVE"))
    # Rejected alternatives WITH the reason they lost — so the agent knows what NOT to drift to
    # and why, instead of only a bare title.
    rej = []
    for r in (approach.get("rejected") or []):
        if not isinstance(r, dict):
            continue
        name = r.get("title") or r.get("id") or "?"
        reason = r.get("why_not") or r.get("tradeoffs") or r.get("how_it_fits")
        rej.append(f"{name} — {str(reason)[:160]}" if reason else str(name))
    if rej:
        lines.append("REJECTED alternatives (do NOT implement these): " + "; ".join(rej))
    ev = [e.get("file") for e in (approach.get("evidence") or []) if isinstance(e, dict) and e.get("file")]
    if ev:
        lines.append("Grounded in (the human saw these as evidence): " + ", ".join(ev[:6]))
    return "\n".join(lines)


def _changed_elements(diff_record: dict) -> set[str]:
    """Element/type names that Phase A added/changed/deprecated, from the XSD diff record."""
    out: set[str] = set()
    for rec in (diff_record or {}).values():
        if isinstance(rec, dict):
            for k in ("new", "modified", "deprecated"):
                out.update(str(e) for e in (rec.get(k) or []))
    return out


def _render_xsd_diff(diff_record: dict) -> str:
    """Render the deterministic XSD element diff as structured per-schema lines instead
    of a raw Python dict repr — so the code agent reads NEW/MODIFIED/DEPRECATED clearly
    and acts on the machine-derived facts, not a stringified dict (plan-fidelity §7.4)."""
    lines = []
    for target, rec in (diff_record or {}).items():
        if not isinstance(rec, dict):
            continue
        parts = [f"{k.upper()}: {', '.join(str(e) for e in (rec.get(k) or []))}"
                 for k in ("new", "modified", "deprecated") if rec.get(k)]
        lines.append(f"  {target} — " + ("; ".join(parts) if parts else "no element changes"))
    return "\n".join(lines)


def _jaxb_links_block(java_links: list, diff_record: dict) -> str:
    """The EXISTING element→Java accessor links for the elements Phase A changed — so the
    implementer updates the real consumers/getters instead of guessing JAXB names. Scoped to
    changed elements (high signal), capped, and flagged advisory (confirm before relying)."""
    changed = _changed_elements(diff_record)
    rows = [f"  - {l['xpath']} → {l.get('symbol', '?')}" for l in (java_links or [])
            if isinstance(l, dict) and l.get("xpath") in changed]
    if not rows:
        return ""
    return ("\n\nExisting JAXB element→Java accessors for the elements you changed (advisory — these "
            "consumers/getters already exist; UPDATE them, and confirm exact names with read_file/"
            "verify_change before relying on them):\n" + "\n".join(rows[:40]))


def _xsd_concerns_block(concerns: list) -> str:
    """Disruptive schema changes Phase A DECLINED as breaking (flag_concern) — surfaced so the
    implementer knows what is fragile and does not silently re-introduce a breaking change."""
    items = []
    for c in (concerns or []):
        if not isinstance(c, dict):
            continue
        line = c.get("message", "")
        if c.get("declined_change"):
            line += f" [declined: {c['declined_change']}]"
        if line:
            items.append(f"  - {line}")
    if not items:
        return ""
    return ("\n\n⚠ RISKY schema changes flagged in Phase A (the schema agent DECLINED these as "
            "breaking and did NOT apply them — do not silently re-introduce them; handle dependent "
            "consumers carefully):\n" + "\n".join(items[:20]))


def _enum_occupancy_block(report: dict) -> str:
    """Phase-A enum literals that the platform found ALREADY BOUND elsewhere in the code.

    Phase B inherits the schema as a fixed baseline, so a colliding wire value is something it
    must implement AROUND (scope the check by message type / flow) rather than discover halfway
    through and stall on. Stating the collision up front — with the git-grep evidence — is what
    stops the implementer from assuming the value is free."""
    occupied = [o for o in (report or {}).get("occupied") or [] if isinstance(o, dict)]
    if not occupied:
        return ""
    items = []
    for o in occupied[:10]:
        sample = "; ".join((o.get("sample") or [])[:3])
        items.append(f"  - '{o.get('value')}' (declared in {', '.join(o.get('declared_in') or [])}) "
                     f"already appears at {o.get('hits')} code location(s): {sample}")
    return ("\n\n⚠ SCHEMA ENUM VALUES THAT COLLIDE WITH EXISTING CODE — the platform git-grepped every "
            "enum literal Phase A added; these are ALREADY IN USE:\n" + "\n".join(items)
            + "\nThe schema is FIXED for this phase, so do NOT plan on changing the value. Read each "
              "cited site to learn what the existing binding means, then make your new logic "
              "unambiguous against it (scope the check to the specific message type / flow rather "
              "than matching the bare value). If the collision genuinely cannot be resolved in Java, "
              "say so explicitly in your summary instead of shipping an ambiguous check.")


def _plan_looks_research_shaped(plan_block: str) -> bool:
    """True when the upstream plan reads like a research / Product-Canvas deliverable
    (future / reference-only framing) rather than a concrete build spec — the signal that
    Phase B was handed a strategy document, not an implementation plan. Heuristic and
    lossless: it only ADDS a corrective directive, never drops the plan, so a false positive
    merely reinforces 'implement concretely' on an already-good plan."""
    t = (plan_block or "").lower()
    if not t.strip():
        return False
    research = sum(t.count(k) for k in (
        "reference only", "referenced only", "not modified", "future design", "future-design",
        "does not exist", "not modelled today", "net-new construct", "product canvas"))
    impl = sum(t.count(k) for k in ("create ", "modify ", "add to ", "new file", "edit "))
    return research >= 2 and research > impl


def _code_user_prompt(ctx: ContextPack, xsd_scope: XsdScope | None, intent: str,
                      feedback: dict | None = None, continuation: dict | None = None,
                      approach: dict | None = None, plan_block: str = "") -> str:
    # The human-ratified Change-Analysis plan IS the spec for Phase B: implement it in full.
    # BRD/TSD are reference only (pull a section to clarify, never to override the plan) — this
    # is what keeps the plan, not the docs, authoritative and stops the LLM drifting.
    plan = ""
    if plan_block:
        corrective = ""
        if _plan_looks_research_shaped(plan_block):
            corrective = ("\n⚠ The plan below is written in a RESEARCH / Product-Canvas style "
                          "(future / 'reference only'). IGNORE that framing — you are BUILDING this "
                          "feature in the CURRENT code now: implement every described capability as "
                          "concrete file changes in the selected repos that compile and verify; never "
                          "treat a repo as reference-only or defer work to a 'future' phase.")
        plan = ("\n\n── Approved implementation plan (BINDING — implement THIS, in full) ──"
                + corrective + "\n"
                + wrap_untrusted(plan_block, "APPROVED_PLAN")
                + "\nThis plan is the single source of truth for WHAT to build. The BRD / Tech "
                  "Spec are REFERENCE ONLY — read a section to clarify a detail, never to add, "
                  "drop, or override what the plan says. Cover every item; do not stop until the "
                  "whole plan is implemented.")
    xsd = ""
    if xsd_scope and xsd_scope.edits_applied:
        xsd = ("\n\nXSD scope already applied:\n- " + "\n- ".join(xsd_scope.edits_applied)
               + (f"\nElement changes (per schema):\n{wrap_untrusted(_render_xsd_diff(xsd_scope.diff_record), 'XSD_DIFF')}" if xsd_scope.diff_record else "")
               + "\n\nJAXB discipline — the classes for these schemas are REGENERATED by "
                 "verify_change's build (the changed schema module rebuilds first). Before "
                 "writing any Java that uses new/changed elements: call verify_change ONCE to "
                 "regenerate, then READ the regenerated classes under the schema module's "
                 "target/generated-sources/ to learn the EXACT class and accessor names. "
                 "xjc naming is not guessable (numeric vs String bindings, wrapper classes, "
                 "attribute vs element accessors) — never write code against guessed getters.")
    if xsd_scope:
        # Pass through what Phase A already learned: the pre-change accessor map for the elements
        # it touched, and any breaking changes it flagged — so the implementer doesn't re-derive
        # JAXB names or walk into a known-fragile change blind.
        xsd += _jaxb_links_block(getattr(xsd_scope, "java_links", None) or [], xsd_scope.diff_record)
        xsd += _xsd_concerns_block(getattr(xsd_scope, "concerns", None) or [])
        xsd += _enum_occupancy_block(getattr(xsd_scope, "enum_occupancy", None) or {})
    cont = _continuation_block(continuation)
    fb = _feedback_block(feedback)
    tail = ("Implement the change. submit_plan first"
            + (" (covering EVERY item in the approved plan above)" if plan_block else "")
            + ", read before editing.")
    # Order = the decided chain: intent → XSD already applied → chosen approach → the plan to
    # implement (placed last/most-prominent) → any continuation/fix context → the call to act.
    return (f"Change intent:\n{wrap_untrusted(intent, 'CHANGE_INTENT')}{xsd}{_approach_block(approach)}{plan}"
            f"{cont}{fb}\n\n{tail}")


def _continuation_block(continuation: dict | None) -> str:
    cont = ""
    if continuation:
        # Cap hit mid-change: CONTINUE your own prior work with memory of the plan, what's
        # already changed, and what's already explored — finish the REMAINING work; do not
        # restart or re-read what you've already read.
        stat = (continuation.get("diff_stat") or "").strip()
        plan_txt = (continuation.get("plan") or "").strip()
        read_txt = (continuation.get("read") or "").strip()
        cont = (f"\n\n⏩ CONTINUING (round {continuation.get('round', 2)}) — this is a CONTINUATION of "
                "YOUR OWN prior work, NOT a fresh start. Do NOT re-explore the codebase wholesale and "
                "do NOT redo files already changed. The plan / changed / explored lists below are a "
                "compact CHECKPOINT and may be INCOMPLETE — when you are unsure what a file contains "
                "NOW, re-read it: the working tree is ground truth, your recollection is not.")
        if plan_txt:
            cont += f"\n\nYour plan:\n{plan_txt}"
        if stat:
            cont += f"\n\nAlready changed (on disk — DONE):\n{stat}"
        if read_txt:
            cont += f"\n\nAlready explored (no need to re-read these):\n{read_txt}"
        notes_txt = (continuation.get("notes") or "").strip()
        if notes_txt:
            cont += ("\n\nYour own notes/findings from the last round (build on these — do NOT "
                     f"re-derive what you already worked out):\n{notes_txt}")
        gaps_txt = (continuation.get("gaps") or "").strip()
        if gaps_txt:
            cont += f"\n\n⚠ PLAN NOT FULLY IMPLEMENTED — {gaps_txt}"
        cont += ("\n\nResume from your plan and make ONLY the remaining edits. If you need a file's "
                 "exact current text to edit it, read just that one file. Call verify_change when complete.")
    return cont


def _feedback_block(feedback: dict | None) -> str:
    fb = ""
    if feedback:
        # Verification failed last round — hand the agent the PARSED file:line errors
        # (not just a gates dict) so it fixes the specific problems, and tell it to
        # self-check with verify_change before finishing (§9.3).
        errs = feedback.get("errors") or []
        err_block = ("\n".join(f"  - {e}" for e in errs[:40]) if errs
                     else "  (no parsed diagnostics — re-run the failing gate's own command "
                          "via run_command to inspect; plain 'mvn compile' can pass while "
                          "the scoped gate fails)")
        if len(errs) > 40:
            # A silent cap reads as the complete list — the dropped errors resurface next
            # round and the loop looks non-convergent (the prior_blockers lesson).
            err_block += (f"\n  - …plus {len(errs) - 40} MORE error(s) not listed for space "
                          "— none is resolved by omission; fix the ones above, then "
                          "re-verify to surface the rest")
        if feedback.get("source") == "review":
            # Verification passed but the reviewer BLOCKED: the change builds yet does not fully
            # deliver the plan. The agent must FINISH it, not just recompile. Every line below must
            # be cleared in THIS pass — fixing only a few and stopping is what made the loop run for
            # rounds without converging. Items tagged [advisory] are non-blocking but must also be
            # resolved so they don't re-surface and re-inflate the gap count next round.
            fb = ("\n\n⛔ The REVIEWER BLOCKED this change — it COMPILES but is NOT done. Each line below "
                  "is a gap between what you built and what the plan required. Fix EVERY one in THIS pass "
                  "— do NOT stop after the first few, and do NOT leave any flagged file untouched:\n"
                  f"{err_block}\n"
                  "TRACE-FIRST FIX PROTOCOL (mandatory for each BLOCKING finding above): before editing, "
                  "(1) read the cited code and state the exact execution path that triggers the bug, "
                  "(2) state the root cause, (3) choose the SMALLEST fix that removes it, (4) state what "
                  "existing behaviour must remain unchanged and re-read the callers to confirm your fix "
                  "preserves it. Do NOT rewrite a whole subsystem to fix one finding — a file that had "
                  "no finding against it must not change unless a fix genuinely requires it. Rewrites "
                  "are how one fix creates four new bugs.\n"
                  "After editing, RE-READ each file you changed and confirm the fix is REAL — actual logic, "
                  "not a comment/stub/TODO claiming behaviour the code doesn't have — and that you introduced "
                  "no new break (e.g. a new DB migration needs its baseline; no stray/non-ASCII bytes in "
                  "config files). Then call verify_change to confirm it still compiles before you finish.")
        else:
            fb = (f"\n\n⚠ Previous verification FAILED (gates={feedback.get('gates')}). "
                  f"Fix these specific errors:\n{err_block}\n"
                  "Then call verify_change to confirm it compiles before you finish.")
        # Anti-oscillation: show the signatures of EARLIER failed attempts so the agent doesn't
        # re-apply a fix that already regressed (fix-A-breaks-B-breaks-A).
        hist = feedback.get("history") or []
        if hist:
            prior = "; ".join(", ".join((h.get("errors") or [])[:2]) for h in hist[-3:] if isinstance(h, dict))
            fb += (f"\n⟳ This change has already failed verification {len(hist) + 1}× — earlier failures: "
                   f"{prior[:400]}. If an error here is one you already 'fixed', that fix REGRESSED — "
                   "step back and change approach instead of repeating the same edit.")
        # P2 — strategist verdict (attached by the orchestrator after repeated failed rounds):
        # a one-shot structural recommendation. BINDING for this round — another round of the
        # same approach is exactly what has already failed N times.
        strategy = (feedback.get("strategy") or "").strip()
        if strategy:
            fb += ("\n\n🧭 STRATEGIST (this approach has failed repeatedly — CHANGE APPROACH, "
                   "don't re-patch): " + strategy[:1500] +
                   "\nApply THIS structural change this round instead of another variant of the "
                   "previous fix. If you believe it is wrong, say why explicitly and call "
                   "ask_decision — do not silently repeat the failed approach.")
    return fb


# ── Deterministic XSD record (§7.4) ───────────────────────────────────────────

def _git_show(run_id: str, repo_id: str, sha: str | None, path: str) -> str | None:
    from app.agents import workspace_local
    if not sha:
        return None
    res = adapter.run_command(workspace_local.repo_dir(run_id, repo_id), ["git", "show", f"{sha}:{path}"])
    return res.stdout if res.ok else None


def _xsd_diff_record(ctx: ContextPack, run_id: str, ops: list[FileOp]) -> dict:
    out: dict = {}
    for op in ops:
        if not op.path.lower().endswith(".xsd") or op.op == "delete":
            continue
        key = f"{op.repo_id}:{op.path}"
        if op.op == "add":
            base = ""                       # genuinely new file → every element is NEW
        else:
            base = _git_show(run_id, op.repo_id, ctx.repo_base_sha.get(op.repo_id), op.path)
            if base is None:
                # Could not read the pre-edit version (missing/invalid base SHA).
                # Do NOT diff against "" — that would falsely report every element
                # as NEW for a MODIFY. Flag the gap instead.
                out[key] = {"new": [], "modified": [], "deprecated": [], "base_unavailable": True}
                continue
        try:
            d = xsd_graph_builder.diff_schema(base, op.content or "")
            out[key] = {"new": d.new, "modified": d.modified, "deprecated": d.deprecated}
        except Exception as e:  # noqa: BLE001 — a diff hiccup must never terminate the run
            logger.warning("xsd diff failed for %s (recorded as base_unavailable): %s", key, e)
            out[key] = {"new": [], "modified": [], "deprecated": [], "base_unavailable": True}
    return out


def _added_enum_literals(ctx: ContextPack, run_id: str, ops: list[FileOp]) -> list[dict]:
    """Every enum literal this Phase-A round ADDS to a schema, as
    ``[{repo_id, path, type_key, value}]``. Empty when the round added none.

    Deliberately re-reads the base via ``_git_show`` rather than reusing the diff record:
    the diff record stores only type KEYS, and the occupancy gate needs the VALUES.
    """
    added: list[dict] = []
    for op in ops:
        if not op.path.lower().endswith(".xsd") or op.op == "delete":
            continue
        base = "" if op.op == "add" else _git_show(
            run_id, op.repo_id, ctx.repo_base_sha.get(op.repo_id), op.path)
        if base is None:
            continue                     # base unreadable — the diff record already flags it
        try:
            for type_key, vals in xsd_graph_builder.added_enum_values(base, op.content or "").items():
                for v in vals:
                    added.append({"repo_id": op.repo_id, "path": op.path,
                                  "type_key": type_key, "value": v})
        except Exception as e:  # noqa: BLE001 — never terminate the run over an advisory scan
            logger.warning("enum extraction failed for %s:%s: %s", op.repo_id, op.path, e)
    return added


# Cap the sweep: each literal is a repo-wide `git grep`, and a schema round that adds a
# 200-value code list must not turn the freeze into a multi-minute stall.
_MAX_ENUM_OCCUPANCY_CHECKS = 40


def enum_occupancy_report(ctx: ContextPack, run_id: str, ops: list[FileOp]) -> dict:
    """Occupancy-check every enum literal this schema round ADDS, against the real code.

    This is the deterministic guard for the failure that motivated it: Phase A added
    ``<xs:enumeration value="BG"/>`` to ``txnPurpose`` while ``CommonConstant.java`` already
    bound ``TRANSIT_UTP_PURPOSE_CODE = "BG"``. Nothing checked, the human approved a schema
    whose structured diff was empty, and Phase B then spent an entire run deadlocked against
    a locked schema it could not correct.

    Server-executed (``git grep``), so it cannot be forgotten or reasoned away by a model.
    ADVISORY: a hit is evidence the value is already in use — the human decides, the gate
    does not auto-block. Returns ``{}`` when the round adds no enum literal.
    """
    added = _added_enum_literals(ctx, run_id, ops)
    if not added:
        return {}
    from app.agents.agentic_tools import occupancy_in_roots
    from app.agents import workspace_local
    roots = {rid: workspace_local.repo_dir(run_id, rid) for rid in (ctx.selected_repo_ids or [])}
    occupied: list[dict] = []
    clean: list[dict] = []
    unchecked: list[dict] = []
    # De-dupe by VALUE: the same literal added to two schemas needs one sweep, not two.
    by_value: dict[str, list[dict]] = {}
    for item in added:
        by_value.setdefault(item["value"], []).append(item)
    for value in sorted(by_value)[:_MAX_ENUM_OCCUPANCY_CHECKS]:
        sites = by_value[value]
        where = sorted({f"{s['path']} ({s['type_key']})" for s in sites})
        try:
            occ = occupancy_in_roots(roots, value)
        except Exception as e:  # noqa: BLE001 — advisory scan, never fatal
            logger.warning("enum occupancy sweep failed for %r: %s", value, e)
            unchecked.append({"value": value, "declared_in": where, "reason": str(e)[:200]})
            continue
        rec = {"value": value, "declared_in": where, "hits": occ["hits"],
               "sample": occ["sample"]}
        if not occ["complete"]:
            unchecked.append({**rec, "reason": "one or more repos could not be scanned"})
        elif occ["hits"]:
            occupied.append(rec)
        else:
            clean.append(rec)
    truncated = max(0, len(by_value) - _MAX_ENUM_OCCUPANCY_CHECKS)
    return {"occupied": occupied, "clean": clean, "unchecked": unchecked,
            "checked": len(by_value) - truncated, "truncated": truncated}


def _existing_links(db, repo_ids: list[str]) -> list:
    if db is None:
        return []
    from app.models.xsd_graph import XsdJavaLink
    rows = db.query(XsdJavaLink).filter(XsdJavaLink.repo_id.in_(repo_ids)).limit(200).all()
    return [{"xpath": r.xpath, "source": r.source, "confidence": r.confidence,
             "symbol": r.symbol_chunk_id_or_path} for r in rows]


_ANALYSIS_PREFACE = (
    "You are the Change-Analysis agent for a " + _DOMAIN_NAME + " system (accuracy upgrade S2). A product "
    "requirement is given. Read the ACTUAL code to understand how the system works today, then "
    "produce an implementation PLAN. You DECIDE NOTHING yourself. Work REUSE-FIRST: call "
    "flow_context, find_existing_xsd, module_context, and read_file on the real flow + its "
    "consumers BEFORE forming any view. Pin load-bearing findings with record_fact the moment "
    "you learn them (an existing value/constant binding, a verified constraint, a human "
    "decision) — the sheet stays in your context all run and travels to later phases; a fact "
    "on the sheet is never re-derived from memory.\n\n"
    "IMPLEMENTATION, NOT A CANVAS. You are planning to BUILD this feature in the CURRENT code, now. "
    "The BRD / Product Canvas / assessment you can read are REFERENCE for WHAT the feature is — they "
    "are often written as research, market strategy, or 'future design'. Do NOT inherit that framing: "
    "never mark a target repo 'reference only / not modified', and never defer the work to a 'future' "
    "phase. If the capability does not exist yet, your plan is precisely HOW to make it exist — the "
    "concrete files to add/modify in these repos to implement it now. A plan whose conclusion is "
    "'reference only / future design' is a FAILED plan.\n\n"
    "DECIDE IT YOURSELF BY DEFAULT. For almost every choice, make the most reasonable, code-"
    "grounded decision and RECORD it in the plan's `assumptions` list for the PM to confirm or "
    "override at ratification — do NOT stop to ask. Call ask_clarifications ONLY on a genuine "
    "need basis: you are actually blocked (cannot pick a safe default because the options diverge "
    "materially in business behaviour, e.g. 'reject vs silently ignore an invalid code') OR the "
    "decision is CRITICAL and a PM must own it (money movement, compliance, a partner-visible "
    "contract) where getting it wrong is costly. If you can reasonably default it, DEFAULT it and "
    "move on — an unnecessary question is a defect. When you do ask, send ONE batch of functional "
    "questions, each with 2-4 options and a plain-language consequence sourced from the code you "
    "read; mark a recommended option ONLY when you have verified grounds for it (never recommend "
    "a value or claim you could not verify in the code — omitting the recommendation is always "
    "acceptable). If an option proposes a concrete NEW identifier/value (an enum value, code, "
    "constant, API/element name), set its proposed_value and cite the file(s) defining the "
    "existing value space in `evidence` — the platform occupancy-checks every proposed value; "
    "when the real value is for an authority/PM to assign, offer a defer option instead of "
    "inventing candidates. NEVER ask pure code-MECHANISM questions (class/method "
    "structure, retry tactics) — those are deferred to Phase B. Code findings that merely "
    "CONSTRAIN the design are STATEMENTS in the plan, not questions.\n\n"
    "ENUMERATE THE BLAST RADIUS. For every element/type/signature/flow you propose to change, find "
    "the EXISTING consumers that will break and name them: call callers() on the affected handlers "
    "(across ALL repos) and jaxb_accessors() on any changed XSD element to get its real Java "
    "accessors. A plan that changes a schema without listing the consumers that must be updated is "
    "incomplete. Your plan MUST be FILE-LEVEL: list each file to add/modify with a one-line intent, "
    "so Phase B can implement it EXACTLY without re-deriving the consumer set.\n\n"
    "CRITICAL DECISIONS ARE NEVER ASSUMPTIONS. technical_analysis MUST contain a "
    "`critical_decisions` list covering EVERY one of these dimensions that the change touches: "
    "settlement_model, money_movement_legs (enumerate every debit/credit leg and who pays whom), "
    "atomicity_mechanism (how concurrent state transitions are made single-winner), "
    "idempotency_keys, event_ordering (persist-vs-publish order), expiry_semantics, limits, "
    "error_codes, backward_compat_scope (which existing paths must stay byte-identical), "
    "external_counterparty_contract (ANY behaviour this change ASSUMES of an external party — "
    "" + _COUNTERPARTY_EXAMPLES + " calling us back, honouring a payload shape or callback endpoint, "
    "meeting a timing/retry obligation — beyond what an XSD or spec in these repos already pins. "
    "The decision MUST name: the counterparty, the direction (they-call-us / we-call-them), the "
    "artifact that pins the contract (circular §, XSD, spec section), and the failure path when "
    "the counterparty does not comply — timeout, reconciliation, or decline code. An external "
    "obligation left as a free-text assumption is exactly the gap that ships a callback handler "
    "nothing ever calls). Each "
    "entry: {\"dimension\", \"decision\", \"source\": \"requirement|code_verified|human_decision\", "
    "\"evidence\" (the requirement sentence / file you read / ledger answer), \"directive\" (ONE "
    "imperative sentence the implementer must obey, e.g. 'Each child transaction performs the "
    "participant debit and merchant credit for its share; no consolidated merchant-credit "
    "transaction may be generated.')}. If you cannot source a touched dimension from the "
    "requirement or the code you actually read, you MUST ask_clarifications for it — a critical "
    "dimension may never be defaulted into `assumptions`; the PM's answer becomes its "
    "human_decision source. The plan gate REJECTS ratification while any touched dimension is "
    "missing or unsourced.\n\n"
    "When you have enough to plan, call propose_plan with:\n"
    "  • functional_plan — a DETAILED, PM-facing plan in business language (every statement "
    "traceable to a technical finding). Return it as an OBJECT with these keys:\n"
    "      - `overview`: 2-4 sentences on what is changing and why.\n"
    "      - `steps`: an ORDERED, end-to-end walkthrough of EACH flow the change touches — the "
    "happy path AND the key branches/failure cases — one clear business-language sentence per "
    "step. This is the HEART of the plan: be thorough and specific about the actual flow (who "
    "does what, in what order, what the system decides), NOT a one-line summary. A terse plan is "
    "a defect.\n"
    "      - `implementation_approach`: a short, readable summary of HOW it will be built "
    "technically (which components change and in what sequence), digestible by a PM — this is the "
    "'technical details' view, kept separate from the business flow.\n"
    "      - `assumptions`: every decision you made WITHOUT asking, each a plain-language "
    "statement the PM can confirm or override.\n"
    "    You MUST explicitly INCORPORATE the PM's clarification answers into `overview` and "
    "`steps` so the PM sees their own answers reflected in the plan — do not merely restate them "
    "as assumptions.\n"
    "  • technical_analysis (full fidelity: impacted repos/modules/flows, real XSD files+"
    "namespaces, data_model_changes, reuse findings, constraints, risks, the `critical_decisions` "
    "list above, and the per-file change list with the consumers to update). It MUST ALSO include a "
    "`canonical_terms` list — the FIXED enums/values this change standardizes on that EVERY "
    "downstream document (BRD/TSD/XSD) must reproduce VERBATIM: credential type/subType values, "
    "amount/limit values, canonical " + _DOMAIN_NAME + " API names, error codes, and enum/state names. Each entry: "
    "{\"term\": \"<what it is>\", \"value\": \"<the EXACT canonical form — correct casing, spelling, "
    "number>\", \"note\": \"<optional>\"}, e.g. " + _CANONICAL_TERM_EXAMPLES + ". "
    "This is the SINGLE SOURCE OF TRUTH for spelling — the BRD/TSD copy from "
    "here and must never re-case, re-spell, or drop a fixed value.\n"
    "  • flow_spec (actors/steps/messages/states with stable step ids). It MUST also carry "
    "`party_flows`: one entry PER API/message this change touches — EXISTING or NEW. Each entry: "
    "{api, classification: 'new'|'existing_modified', parties: [only the parties actually "
    "involved — " + _FLOW_PARTIES_NOTE + "], "
    "hops: [{from, to, message, evidence, confidence, note?}] in order — the request AND its "
    "response/callback. `note` is optional: a short plain-language detail of what happens at "
    "that hop (what the user does, what the app/switch decides"
    + (" — e.g. " + _HOP_NOTE_EXAMPLES if _HOP_NOTE_EXAMPLES else "") +
    "), rendered under that "
    "arrow in the flow diagram}. Evidence discipline per hop: `evidence` names a code file you actually read "
    "(flow_context/handler/forwarder) or an official doc from domain_docs_search (source name). "
    "Docs may CONFIRM a hop; only code may ORIGINATE one. A hop backed by neither is "
    "confidence='assumed' — state it plainly instead of filling the gap from memory; otherwise "
    "'confirmed'. For an EXISTING API derive the route from the code and confirm with docs; for "
    "a NEW API design the route (initiator, each hop, what reaches each party) mirroring how "
    "existing " + _DOMAIN_NAME + " messages route through " + _SWITCH_LABEL + ". A touched API with no party_flows entry is "
    "an incomplete plan. A fully-confirmed flow needs NO clarification question; only a "
    "genuinely assumed or code-vs-doc-conflicting hop may join the single clarification batch.\n"
    "    functional_plan MUST surface each party_flows entry to the PM: append at the END of "
    "`overview` one plain-language line per API, exactly in this shape — \"Party flow — "
    "<API>: <initiator> → <hop> → … (<parties NOT involved> not involved)\" — so the PM "
    "ratifies the flow explicitly. Put it in `overview` itself, not a separate key: only "
    "overview/steps/implementation_approach/assumptions are shown to the PM.\n"
    "Ground BOTH gates in evidence: cite files you actually read. NEVER edit or create anything.\n\n"
    + ANTI_INJECTION_CLAUSE
)

# Comply-first at the plan loop (mirrors the XSD refine guardrail): the PM's clarification
# answers / reopen feedback may RECTIFY the plan's direction. Feasibility is re-checked
# against code; a technically feasible functional choice is implemented as asked — never
# overridden back to the agent's own preference — with the repercussions on record.
_RECTIFICATION_CLAUSE = load_prompt("agents/agentic_subagents/rectification_clause.md")


# ── Runners ───────────────────────────────────────────────────────────────────

def _analysis_resume_messages(transcript: list[dict], followup: str) -> list[dict] | None:
    """Turn a persisted FIRST-DRIVE transcript into a valid continuation so a re-drive
    picks up where it left off instead of re-reading the codebase from turn 1.

    The saved transcript ends with the assistant's GATE tool_use (ask_clarifications /
    propose_plan) but no matching tool_result (the loop broke at the gate before appending
    one). We append a user turn whose tool_result for that gate call carries the PM answers /
    plan feedback — so the answers arrive exactly where the model expects them. Returns None
    if the transcript isn't shaped as expected, so the caller falls back to fresh exploration."""
    if not transcript:
        return None
    msgs = [dict(m) for m in transcript]
    last = msgs[-1]
    if last.get("role") != "assistant" or not isinstance(last.get("content"), list):
        return None
    tool_uses = [b for b in last["content"]
                 if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]
    if not tool_uses:
        return None
    # Anthropic requires a tool_result for EVERY tool_use in the prior assistant turn. The
    # gate tool gets the follow-up as its result; any sibling gets a benign stub.
    gate = {"ask_clarifications", "propose_plan"}
    results, matched = [], False
    for b in tool_uses:
        if not matched and b.get("name") in gate:
            results.append(tool_result_block(b["id"], followup)); matched = True
        else:
            results.append(tool_result_block(b["id"], "(superseded — continue and call propose_plan)"))
    if not matched:                       # gate tool wasn't the trailing call → give it to the first
        results[0] = tool_result_block(tool_uses[0]["id"], followup)
    msgs.append({"role": "user", "content": results})
    return msgs


async def run_analysis(db, *, run_id: str, ctx: ContextPack, intent: str = "",
                       model: str | None = None, cancel_check=None, heartbeat=None,
                       clarification_answers: str | None = None, plan_feedback: str | None = None,
                       workspace_run_id: str | None = None,
                       resume_transcript: list[dict] | None = None,
                       facts: list | None = None,
                       ) -> tuple[dict | None, list[dict], list[dict]]:
    """Read-only Change-Analysis pass (S2). Reads code, then either asks the PM
    (ask_clarifications) or proposes the dual-view plan (propose_plan). Returns
    ``(proposal, transcript, facts)`` — the proposal dict (``kind`` in
    {"clarifications","plan"}) or None, the full conversation so the orchestrator can
    persist it for replay on the next drive, and the fact sheet (record_fact + platform
    auto-facts) for cross-phase handoff. No edits.

    On a re-drive (answers / plan revision) with ``resume_transcript`` set and replay enabled,
    the first drive's conversation is CONTINUED (the answers arrive as the gate tool's result)
    instead of restarting — eliminating the re-exploration. On any problem it falls back to a
    fresh exploration, which is the legacy behaviour."""
    # One clarification batch only. On any re-drive (answers given, or a plan revision),
    # ask_clarifications is REMOVED so the agent converges to a plan instead of looping
    # rounds of questions — anything still uncertain goes into the plan's `assumptions`.
    followup = None
    if clarification_answers:
        followup = ("The PM answered your clarifications:\n"
                    + wrap_untrusted(clarification_answers, "PM_ANSWERS")
                    + "\nIncorporate these and call propose_plan NOW. You get ONE clarification round — "
                      "ask_clarifications is no longer available. Any remaining unknown becomes an entry "
                      "in the plan's `assumptions` list for the PM to confirm or override at ratification."
                    + _RECTIFICATION_CLAUSE)
    elif plan_feedback:
        followup = ("Your prior plan needs revision:\n"
                    + wrap_untrusted(plan_feedback, "PLAN_FEEDBACK")
                    + "\nAddress this and call propose_plan again. Do not ask further clarifications."
                    + _RECTIFICATION_CLAUSE)

    resume_msgs = None
    if followup and resume_transcript and settings.agentic_analysis_replay_transcript:
        resume_msgs = _analysis_resume_messages(resume_transcript, followup)
        if resume_msgs is not None:
            logger.info("run_analysis run=%s REPLAYING drive-1 transcript (%d msgs) — no re-exploration",
                        run_id, len(resume_msgs))

    common = dict(
        run_id=run_id, selected_repo_ids=ctx.selected_repo_ids,
        system=build_system_segments(ctx, _ANALYSIS_PREFACE),
        model=model, agent_name="analysis", db=db,
        doc_sections={"brd": ctx.brd_sections, "tsd": ctx.tsd_sections,
                      "assessment": ctx.assessment_sections, "plan": ctx.plan_sections},
        thinking_budget=settings.agentic_thinking_budget_tokens, require_plan=False,
        max_iterations=settings.agentic_analysis_max_iterations,
        cancel_check=cancel_check, heartbeat=heartbeat, workspace_run_id=workspace_run_id,
        initial_facts=facts,   # prior drive's sheet (handoff-persisted) — seeded + re-pinned
    )
    async def _fresh_exploration():
        """First-drive / fallback path: explore the codebase from turn 1."""
        parts = [f"Requirement:\n{wrap_untrusted(intent, 'REQUIREMENT')}"]
        tools = ANALYSIS_TOOLS
        if followup:
            parts.append(followup)
            tools = [t for t in ANALYSIS_TOOLS if t.get("name") != "ask_clarifications"]
        parts.append("Read the real code first; decide nothing yourself; do not edit or create anything.")
        return await run_agent_loop(user_prompt="\n\n".join(parts), tools=tools, **common)

    if resume_msgs is not None:                       # REPLAY — continue the prior conversation
        try:
            res = await run_agent_loop(
                user_prompt=followup, initial_messages=resume_msgs,
                tools=[t for t in ANALYSIS_TOOLS if t.get("name") != "ask_clarifications"], **common)
        except Exception as e:  # noqa: BLE001 — a well-shaped transcript can still be rejected
            # at call time (thinking-signature / compaction / cache validation). The docstring
            # promises fresh-exploration fallback "if unusable" — a shape check alone doesn't
            # deliver that, so honour it here: never let a bad replay hard-fail the analysis.
            logger.warning("run_analysis run=%s replay REJECTED (%s) — falling back to fresh "
                           "exploration", run_id, e)
            res = await _fresh_exploration()
    else:                                             # FRESH exploration (first drive / fallback)
        res = await _fresh_exploration()
    return res.proposal, res.transcript, res.facts


async def run_approach_proposal(db, *, run_id: str, ctx: ContextPack, intent: str = "",
                                model: str | None = None, cancel_check=None, heartbeat=None,
                                workspace_run_id: str | None = None,
                                plan_block: str = "", scope: str = "full",
                                resume_transcript: list[dict] | None = None) -> dict | None:
    """Reuse-first decision pass: map existing flows + propose OPTIONS for a human.
    ``scope='xsd'`` (Phase A) restricts the question to SCHEMA choices in plain language —
    no architecture options, no new services. Read-only (PROPOSE_TOOLS) — no edits.
    ``plan_block`` is the ratified analysis plan (empty on legacy/no-plan runs); when present
    each option is flagged for divergence from it so the human sees what differs and why.
    ``resume_transcript`` is the ratified analysis run's conversation: when set (and enabled),
    propose CONTINUES it — the discovery sweep analysis already did (same 14 read tools) stays
    in context instead of being redone from turn 1. Returns the proposal dict, or None."""
    # The plan is per-run (not cacheable), so it rides the user prompt, not the cached preface.
    plan_seg = (("\n\nRATIFIED PLAN (the analysis recommended this direction — for EACH option set "
                 "diverges_from_plan + divergence_note relative to it):\n" + plan_block)
                if plan_block else "")
    if scope == "xsd":
        preface = _PROPOSE_PREFACE_XSD
        ask = ("Decide what changes (if any) the UPI XML schemas need — verify against the code "
               "first, then call propose_approach with plain-language schema options + a "
               "recommendation. Do not create or edit anything.")
    else:
        preface = _PROPOSE_PREFACE
        ask = ("Map how the existing system handles this kind of flow, then call propose_approach "
               "with reuse-vs-new options + a recommendation. Do not create or edit anything.")
    common = dict(
        run_id=run_id, selected_repo_ids=ctx.selected_repo_ids,
        system=build_system_segments(ctx, preface),
        tools=PROPOSE_TOOLS, model=model, agent_name="approach_proposal", db=db,
        doc_sections={"brd": ctx.brd_sections, "tsd": ctx.tsd_sections,
                      "assessment": ctx.assessment_sections, "plan": ctx.plan_sections},
        thinking_budget=settings.agentic_thinking_budget_tokens, require_plan=False,
        cancel_check=cancel_check, heartbeat=heartbeat, workspace_run_id=workspace_run_id,
    )
    resume_msgs = None
    if resume_transcript and settings.agentic_propose_replay_transcript:
        # The analysis transcript ends at its propose_plan gate (tool_use, no result). The
        # ratification notice arrives as that gate's tool_result — the model keeps every file
        # it read and continues straight to option-framing. Its evidence gate still demands
        # citations, so the followup names the new gate tool explicitly.
        followup = ("Your plan was recorded and RATIFIED by the PM and tech-lead. NEW TASK — "
                    "using everything you already read: " + ask +
                    " propose_approach requires ≥2 evidence citations to files read in THIS "
                    "session — the files you already opened count; cite the flow/consumer files "
                    "your options rest on." + plan_seg)
        resume_msgs = _analysis_resume_messages(resume_transcript, followup)
    res = None
    if resume_msgs is not None:
        logger.info("run_approach_proposal run=%s REPLAYING analysis transcript (%d msgs) — "
                    "no re-exploration", run_id, len(resume_msgs))
        try:
            res = await run_agent_loop(user_prompt=followup, initial_messages=resume_msgs, **common)
        except Exception as e:  # noqa: BLE001 — replay can be rejected at call time
            # (thinking-signature / cache validation); never hard-fail the gate over it.
            logger.warning("run_approach_proposal run=%s replay REJECTED (%s) — falling back to "
                           "fresh exploration", run_id, e)
            res = None
    if res is None:
        user = f"Requirement:\n{wrap_untrusted(intent, 'REQUIREMENT')}\n\n{ask}{plan_seg}"
        res = await run_agent_loop(user_prompt=user, **common)
    return res.proposal


async def run_xsd_discovery(db, *, run_id: str, ctx: ContextPack, intent: str = "",
                            model: str | None = None, cancel_check=None, heartbeat=None,
                            decision: dict | None = None, change_request: str | None = None,
                            accepted_risk: bool = False, decisions_block: str = "",
                            required_schema: str = "", via_revision: bool = False,
                            workspace_run_id: str | None = None) -> XsdScope:
    # S7-style: bind the XSD agent to the ratified Decision Ledger (empty → no-op).
    _xsd_preface = _XSD_PREFACE + (
        "\n\nDECISIONS (BINDING — human-ratified; the schema you design MUST honour these; "
        "do NOT contradict, re-derive, or reopen them):\n" + decisions_block
        if decisions_block else "")
    # A settled conversation cannot be reopened: once the human accepted the risk or chose one
    # of the agent's own safer alternatives, propose_revision is REMOVED from the toolset — the
    # prompt directive alone did not stop the prod 58ab724c re-litigation loop.
    _tools = ([t for t in XSD_TOOLS if t["name"] != "propose_revision"]
              if (accepted_risk or via_revision) else XSD_TOOLS)
    res = await run_agent_loop(
        run_id=run_id, selected_repo_ids=ctx.selected_repo_ids,
        system=build_system_segments(ctx, _xsd_preface),
        user_prompt=_xsd_user_prompt(ctx, intent, decision, change_request, accepted_risk,
                                     required_schema=required_schema, via_revision=via_revision),
        tools=_tools, model=model, agent_name="xsd_discovery", db=db,
        doc_sections={"brd": ctx.brd_sections, "tsd": ctx.tsd_sections,
                      "assessment": ctx.assessment_sections, "plan": ctx.plan_sections},
        thinking_budget=settings.agentic_thinking_budget_tokens,
        cancel_check=cancel_check, heartbeat=heartbeat, workspace_run_id=workspace_run_id,
        schema_only=True,   # Phase A edits .xsd/.xjb only — Java is the code phase's job
    )
    ops = res.change_set
    diff_record = _xsd_diff_record(ctx, run_id, ops)
    try:
        _enum_occ = enum_occupancy_report(ctx, run_id, ops)
    except Exception as e:  # noqa: BLE001 — an advisory gate must never break the XSD phase
        logger.warning("enum occupancy gate failed (skipped): %s", e)
        _enum_occ = {}
    return XsdScope(
        decisions=(res.plan or {}).get("reuse_decisions", []),
        edits_applied=[f"{o.repo_id}:{o.path}" for o in ops],
        created=[f"{o.repo_id}:{o.path}" for o in ops if o.op == "add"],
        diff_record=diff_record,
        java_links=_existing_links(db, ctx.selected_repo_ids),    # EXISTING (pre-change) links, advisory
        # The deterministic record is trustworthy iff every edited XSD got a real
        # base-vs-edited diff (no base_unavailable gap). Independent of how the
        # loop stopped — the lxml diff is deterministic regardless.
        determinism_ok=all("base_unavailable" not in v for v in diff_record.values()),
        enum_occupancy=_enum_occ,
        final_text=res.final_text,
        concerns=res.concerns,
        # The refine pass stopped to converse (disruptive request → safer alternatives).
        revision_proposal=res.proposal if res.stopped == "awaiting_decision" else None,
    )


def _code_resume_messages(transcript: list[dict], followup: str) -> list[dict] | None:
    """Turn a prior code-round transcript into a valid continuation so the next round KEEPS the
    author's memory — files read, reasoning, edits, failed attempts — instead of restarting from
    a ~4KB string summary (the root non-convergence gap vs Claude Code: the fixer must be the
    same conversation that wrote the code). Handles every tail shape a code round can end in;
    returns None when the transcript is unusable (caller falls back to a fresh round)."""
    if not transcript or not followup:
        return None
    msgs = [dict(m) for m in transcript]
    last = msgs[-1]
    content = last.get("content")
    if last.get("role") == "user":
        # Cap-hit tail: the loop already appended the final tool_results — fold the follow-up
        # in as a trailing text block (a second consecutive user message would break alternation).
        if isinstance(content, list):
            msgs[-1] = {"role": "user", "content": list(content) + [{"type": "text", "text": followup}]}
        else:
            msgs[-1] = {"role": "user", "content": f"{content}\n\n{followup}"}
        return msgs
    if last.get("role") == "assistant":
        results = []
        if isinstance(content, list):
            # Completed/gate tail may still carry unanswered tool_use blocks — Anthropic requires
            # a tool_result for every one before the conversation can continue.
            pending = [b for b in content
                       if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]
            results = [tool_result_block(b["id"], "(superseded — continue with the instructions below)")
                       for b in pending]
        msgs.append({"role": "user",
                     "content": results + [{"type": "text", "text": followup}] if results else followup})
        return msgs
    return None


async def run_code_change(db, *, run_id: str, ctx: ContextPack, xsd_scope: XsdScope | None = None,
                          intent: str = "", model: str | None = None, cancel_check=None,
                          feedback: dict | None = None, continuation: dict | None = None,
                          approach: dict | None = None, decisions_block: str = "",
                          plan_block: str = "", heartbeat=None,
                          workspace_run_id: str | None = None,
                          resume_transcript: list[dict] | None = None,
                          completion_check=None,
                          tsd_assertions: list[dict] | None = None) -> ChangeSet:
    # S7 — the binding Decision Ledger: implement strictly within the human-ratified
    # decisions (empty until a Change-Analysis run records them → no-op on legacy runs).
    _decisions = (
        "\n\nDECISIONS (BINDING — human-ratified; implement strictly within these; do NOT "
        "contradict, re-derive, or reopen them):\n" + decisions_block +
        "\nIf a BINDING directive conflicts with what the code actually does, or a decision your "
        "work needs is MISSING from the directives and cannot be derived from the requirement or "
        "the code, call ask_decision — NEVER silently choose an interpretation for money movement, "
        "atomicity, ordering, or settlement. Do NOT use it for code mechanics you can decide, and "
        "NEVER to reopen a ratified directive."
    ) if decisions_block else ""
    # SDLC-A22 (authoring half) — show the agent the ACTUAL TSD assertions the
    # coverage gate will grade it against, with the exact `tsd-ref` marker to
    # copy for each. `_tests_clause()` already asks for the markers; without
    # this block the agent had to invent section names that then failed to match
    # the extractor's output, so measured coverage reflected luck rather than
    # test quality and the gate could never be enforced. Empty string when there
    # are no assertions (or extraction failed), so the prompt is unchanged for
    # runs with no approved TSD.
    _tsd_assertions_block = ""
    if tsd_assertions:
        try:
            from app.agents.tsd_test_generator import assertions_block
            _tsd_assertions_block = assertions_block(tsd_assertions)
        except Exception:  # noqa: BLE001 — a prompt enrichment must never break the run
            logger.debug("could not render tsd assertions block", exc_info=True)

    common = dict(
        run_id=run_id, selected_repo_ids=ctx.selected_repo_ids,
        system=build_system_segments(
            ctx,
            _CODE_PREFACE + _tests_clause() + _tsd_assertions_block
            + _patterns_clause() + _decisions,
        ),
        tools=CODE_TOOLS, model=model, agent_name="code_change", db=db,
        doc_sections={"brd": ctx.brd_sections, "tsd": ctx.tsd_sections,
                      "assessment": ctx.assessment_sections, "plan": ctx.plan_sections},
        thinking_budget=settings.agentic_thinking_budget_tokens,
        cancel_check=cancel_check, heartbeat=heartbeat, workspace_run_id=workspace_run_id,
        code_phase=True,   # Phase B: schema (.xsd/.xjb) is the approved Phase-A baseline — refuse edits to it
        completion_check=completion_check,   # in-loop convergence nudge (acceptance predicates)
    )
    # Transcript replay (same fix as run_analysis, applied to the loop that needs it most): a
    # continuation / verify-retry / review-fix round CONTINUES the author's own conversation —
    # the feedback arrives as the next user turn, with every file read and decision still in
    # context. History compaction bounds the growing transcript; on any replay problem we fall
    # back to the legacy fresh-prompt round (string-summary memory).
    resume_msgs = None
    if resume_transcript and settings.agentic_code_replay_transcript:
        followup = (_continuation_block(continuation) + _feedback_block(feedback)).strip() or (
            "Continue your prior work above and finish the change end-to-end; "
            "call verify_change when complete.")
        resume_msgs = _code_resume_messages(resume_transcript, followup)
    res = None
    if resume_msgs is not None:
        logger.info("run_code_change run=%s REPLAYING prior transcript (%d msgs) — author keeps its memory",
                    run_id, len(resume_msgs))
        try:
            res = await run_agent_loop(user_prompt=followup, initial_messages=resume_msgs, **common)
        except Exception as e:  # noqa: BLE001 — a well-shaped transcript can still be rejected at
            # call time (thinking-signature / cache validation). Never let a bad replay hard-fail
            # the round: edits already made are safe on disk; the fresh round resumes from there.
            logger.warning("run_code_change run=%s replay REJECTED (%s) — falling back to a fresh round",
                           run_id, e)
            res = None
    if res is None:
        res = await run_agent_loop(
            user_prompt=_code_user_prompt(ctx, xsd_scope, intent, feedback, continuation, approach, plan_block),
            **common)
    plan = res.plan or {}
    _dq = (res.proposal if res.stopped == "awaiting_decision"
           and (res.proposal or {}).get("kind") == "code_decision" else None)
    return ChangeSet(
        operations=res.change_set,
        plan=plan,
        reused=plan.get("reuse_decisions", []),
        created=[o.path for o in res.change_set if o.op == "add"],
        stopped=res.stopped,
        iterations=res.iterations,
        read_files=res.read_files,
        final_text=res.final_text or "",
        decision_request=_dq,
        transcript=res.transcript,
        intel_queried=list(getattr(res, "intel_queried", None) or []),
        schema_amendments=list(getattr(res, "schema_amendments", None) or []),
    )
