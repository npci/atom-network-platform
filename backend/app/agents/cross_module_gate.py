# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Mandatory cross-module analysis gate (SDLC review gaps 7, 8, 9, 11).

The SDLC review's central thesis: the platform HAS cross-module tools —
``callers()``, ``impact_analysis()``, ``symbol_graph()`` (``agentic_tools.py``)
— but calling them before editing a shared symbol is advisory, not mandatory.
``intel_gate_reason`` already blocks a blind ``.java`` edit with NO structural
intel queried *at all* this run, but it does not check that the intel queried
was actually about the SYMBOL being edited, and there is no re-analysis step
that catches a fix round introducing a NEW cross-module dependency.

This module is the deterministic, LLM-free, POST-codegen check that closes
that gap — same house pattern as ``contract_gate.py`` / ``di_wiring_gate.py``:
pure functions over (diff, intel-tokens) with no DB/app imports beyond the
shared ``Finding``/``GateResult`` shapes, shadow-first (measure via a
`cross_module_gate` event; only block when ``agentic_cross_module_gate_enforce``
is set), fail-open on any internal error.

What it checks, given the round's diff and the ``intel_queried`` token set
(threaded end-to-end: ``RunContext.intel_queried`` -> ``RuntimeResult`` ->
``ChangeSet`` -> the orchestrator's ``art["intel_queried_all"]``):

1. ``check_shared_symbol_intel`` — a changed Java method whose OWN definition
   line was modified (a signature-shaped edit, not just a body tweak) must have
   had ``callers()``/``impact_analysis()``/``symbol_graph()`` called for that
   exact symbol name at some point in the run. A symbol edited without ANY
   matching intel token is a blocking finding — "you changed X without ever
   checking who calls X."

2. ``check_new_dependency_since`` — given the PRIOR round's recorded consumer
   list for a symbol (from a previous ``cross_module_gate`` event, passed in by
   the caller) and the callers()/impact_analysis() result from THIS round for
   the same symbol, flags a NEWLY appeared consumer file that was not in the
   prior list — the deterministic embodiment of "did this fix round introduce
   a new cross-module dependency" (SDLC gap 9's convergence-target ask). This
   is intentionally a THIN, orchestrator-fed check (this module has no DB
   access) — the orchestrator supplies both consumer-list snapshots.

Threat model note (closes THREAT_MODEL.md T5 — "Prompt injection not
explicitly named as a threat class in gate design docs"): the deterministic
gates in this file and its siblings (`contract_gate.py`, `di_wiring_gate.py`,
`acceptance_predicates.py`) are this platform's structural defense against a
specific threat class — an LLM whose reasoning has been influenced by
adversarial content it was asked to summarize or act on (a prompt-injection
attempt embedded in retrieved RAG chunks, web research, or partner-supplied
free text flowing through `context_assembler.py`). These gates do not
detect injection directly; they make its PAYOFF structurally unavailable:
even if an LLM's stated reasoning is compromised, its ACTUAL code changes
still have to satisfy deterministic, non-LLM checks (a shared symbol was
actually checked via `callers()`, a hash field actually has a writer, a
`@Autowired` field actually resolves against the Spring context) before
they can pass review. This is why these gates are designed as pure
functions over the diff/AST rather than as a second LLM asked to judge the
first LLM's output — a second LLM call is exactly as susceptible to the
same injected content as the first. Treat this as the STANDING design
constraint for any future gate: if a proposed check requires trusting an
LLM's summary of untrusted content, it does not close this threat class,
no matter how good its prompt is.
"""
from __future__ import annotations

import re

from app.agents.contract_gate import Finding, GateResult, _file_sections

# A Java method/constructor DEFINITION line — signature-shaped, not a call.
# Matches `public ResponseEntity<X> route(Request r) {` etc. Deliberately loose
# (visibility optional, return type optional so constructors match too) —
# false POSITIVES here (treating a non-definition as one) only make this gate
# MORE cautious (it asks for intel on more symbols than strictly required),
# never less safe. False negatives (missing a real definition) mean the gate
# says nothing about that symbol, same as if the check didn't exist — fail-open
# by construction, not by exception handling.
_METHOD_DEF_RE = re.compile(
    r'^\s*(?:@\w+(?:\([^)]*\))?\s*)*'                    # annotations
    r'(?:public|protected|private)\s+'                    # visibility REQUIRED (skip local vars/calls)
    r'(?:static\s+|final\s+|abstract\s+|synchronized\s+)*'
    r'(?:[\w.<>\[\],? ]+?\s+)?'                            # return type (greedy-safe, non-capturing)
    r'(\w+)\s*\([^;{]*\)\s*(?:throws\s+[\w.,\s]+)?\s*\{',  # name(args) {
)

# A file-basename → class-name heuristic: strip the extension, last path segment.
_JAVA_EXT = ".java"


def _changed_method_defs(diff: str) -> list[tuple[str, str]]:
    """[(file_path, method_name)] for every Java method DEFINITION line that was
    ADDED or REMOVED in this diff (a signature-shaped line appearing in the
    added or removed set) — a body-only edit (no def-line touched) does not
    appear here, since renaming a local variable inside a method is not the
    class of change this gate cares about."""
    out: list[tuple[str, str]] = []
    try:
        for sec in _file_sections(diff):
            if not sec["path"].endswith(_JAVA_EXT):
                continue
            for ln in (sec["added"] or []) + (sec["removed"] or []):
                m = _METHOD_DEF_RE.match(ln)
                if m:
                    out.append((sec["path"], m.group(1)))
    except Exception:  # noqa: BLE001 — fail-open: a parse error yields no findings, not a crash
        return []
    return out


def check_shared_symbol_intel(diff: str, intel_queried: list[str] | None = None,
                               *, min_name_len: int = 4) -> list[Finding]:
    """Flag a changed Java method DEFINITION whose name never appears as a
    ``symbol:<name>`` token in ``intel_queried`` — i.e. the agent edited a
    method's signature without ever calling ``callers``/``impact_analysis``/
    ``symbol_graph`` on it this run. ``min_name_len`` skips short/common names
    (``get``, ``run``) that would otherwise dominate the finding list with
    near-certain false positives from getter/setter noise."""
    queried_symbols = {t.split(":", 1)[1] for t in (intel_queried or []) if t.startswith("symbol:")}
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    try:
        for path, name in _changed_method_defs(diff):
            if len(name) < min_name_len or name in seen:
                continue
            key = (path, name)
            if key in seen:
                continue
            seen.add(key)
            if name in queried_symbols:
                continue
            findings.append(Finding(
                check="shared_symbol_intel",
                severity="blocker",
                key=name,
                detail=(f'method "{name}" in {path} was added/changed/removed without ever '
                        f"calling callers()/impact_analysis()/symbol_graph() on it this run — "
                        f"its consumers (if any) were never enumerated before the edit"),
                file=path,
                suggested_fix=(f'Call impact_analysis(symbol="{name}") (or callers/symbol_graph) '
                               f"and confirm every consumer file still behaves correctly, or "
                               f"explain in your summary why {name} has no consumers to check "
                               f"(e.g. it is brand-new, private, and called only within this diff)."),
            ))
    except Exception:  # noqa: BLE001 — fail-open
        return []
    return findings


def check_new_dependency_since(symbol: str, prior_consumers: set[str] | None,
                               current_consumers: set[str] | None) -> list[Finding]:
    """SDLC gap 9 — flag a consumer file that appeared for ``symbol`` in THIS
    round's impact_analysis()/callers() result but was NOT present in the PRIOR
    round's recorded result for the same symbol. Both consumer sets are
    supplied by the caller (this module has no DB access) — typically the
    orchestrator's own prior ``cross_module_gate`` event payload vs. a fresh
    ``impact_analysis`` call made for the re-check. None/empty prior means "no
    prior snapshot to compare" — returns no findings (nothing to diff against),
    NOT "everything is new"."""
    if not prior_consumers or not current_consumers:
        return []
    new = sorted(current_consumers - prior_consumers)
    if not new:
        return []
    return [Finding(
        check="new_dependency_since",
        severity="warning",   # advisory: a new consumer can be entirely legitimate (e.g. the fix
                              # itself added a new caller on purpose) — surfaced for review, not auto-blocked
        key=symbol,
        detail=(f'symbol "{symbol}" has {len(new)} NEW consumer(s) not present in the prior round\'s '
                f"impact analysis — a fix round may have introduced a cross-module dependency: "
                + ", ".join(new[:8])),
        suggested_fix="Confirm the new consumer(s) are an intended part of this change, not an "
                     "accidental new coupling introduced while fixing something else.",
    )]


def run_cross_module_gate(diff: str, intel_queried: list[str] | None = None,
                          *, symbol_consumer_snapshots: dict[str, tuple[set, set]] | None = None,
                          min_name_len: int = 4) -> GateResult:
    """Run both deterministic cross-module checks. ``symbol_consumer_snapshots``
    is ``{symbol: (prior_consumers, current_consumers)}``, supplied by the
    caller when it has both snapshots available; omitted (the common case,
    since it requires a live DB re-query) safely skips
    ``check_new_dependency_since`` entirely."""
    result = GateResult()
    result.findings.extend(check_shared_symbol_intel(diff, intel_queried, min_name_len=min_name_len))
    for symbol, (prior, current) in (symbol_consumer_snapshots or {}).items():
        result.findings.extend(check_new_dependency_since(symbol, prior, current))
    return result
