# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Deterministic, LLM-free contract checks for a generated code change-set.

These gates catch two classes of *runtime* bug that the LLM-judgment gates
(``agentic_review``, ``plan_fidelity``, ``acceptance_predicates``) structurally
miss — those reason over text and grade "does it look like the plan", whereas
these cross-reference the code against itself and against the ratified plan.

Both were shipped by the $371 ``cbabbf9c`` code run after six clean review
rounds; each is a one-pass deterministic check here:

1. ``check_error_code_emission`` — every switch-side error code the plan declares
   must appear as an emitted string literal on some code path. cbabbf9c's
   ``SplitController.createErrorAck`` emitted ``msg.substring(0,2)`` instead of
   the network codes, so YA/U09/U16/XT/Z9/XD were declared everywhere and emitted
   nowhere.

2. ``check_field_consistency`` — a hash/map key read via ``.get("X")`` must be
   written somewhere (a literal field write, an enum constant written via
   ``.name()``, or a known framework field). cbabbf9c's ``GroupExpiryScheduler``
   read ``TOTAL_COUNT``/``PAID_COUNT``/``FAILED_COUNT`` that nothing writes — the
   persistence layer stores ``field=stage`` — so group expiry always closed
   ``CLOSED_NONE_PAID`` with zero counts.

Input is unified-diff text (only ``+`` lines are inspected) plus the plan's
declared error codes. No DB or app imports: pure functions, cheap to run every
round and trivially unit-testable.

The C3 static safety checks extend the same idea to four more deterministic
bug classes (publish-before-persist ordering, behaviour edits in files the plan
never named, undeclared consequential legs, plan-promised config keys nothing
binds). Each is individually fail-open: an internal error returns [] rather
than poisoning the gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# An UPPER_SNAKE hash-field / enum-constant token (>= 3 chars to skip noise like "ID").
_FIELD = r"[A-Z][A-Z0-9_]{2,}"

# Read of a string-keyed field: `.get("TOTAL_COUNT")`, `hget(key, "PAID_COUNT")`.
_READ_RE = re.compile(r'\.get\("(' + _FIELD + r')"\)')
_HGET_RE = re.compile(r'\bhget\([^,]+,\s*"(' + _FIELD + r')"')

# A literal field written by a persistence call on the same line.
_WRITE_VERB = re.compile(
    r'(?:\.put|\.hset|HSET|appendLog|createTransaction|setField)\b')
_LITERAL_TOKEN_RE = re.compile(r'"(' + _FIELD + r')"')

# An enum constant declared one-per-line (`OPEN,` / `PARTIAL_PROGRESS`) — these are
# the field names written via `<enum>.name()`, so they ARE writable fields.
_ENUM_CONST_RE = re.compile(r'^\s*(' + _FIELD + r')\s*,?\s*$')

# Fields the shared persistence layer writes for every hash, so a read of them is
# never a gap even though no producer in the change-set writes them explicitly.
# TransactionHashLogService's Lua HSETs LATEST_STAGE on every create/append.
DEFAULT_FRAMEWORK_FIELDS = frozenset({"LATEST_STAGE"})


@dataclass
class Finding:
    check: str
    severity: str  # "blocker" | "warning"
    key: str
    detail: str
    file: str = ""
    suggested_fix: str = ""


@dataclass
class GateResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_blocker(self) -> bool:
        return any(f.severity == "blocker" for f in self.findings)

    def summary(self) -> dict:
        blockers = [f for f in self.findings if f.severity == "blocker"]
        return {
            "findings": len(self.findings),
            "blockers": len(blockers),
            "by_check": {
                c: sum(1 for f in self.findings if f.check == c)
                for c in sorted({f.check for f in self.findings})
            },
        }


def _added_lines(diff: str) -> list[str]:
    """Return added source lines from EITHER a unified git diff ('+' lines) or the
    orchestrator's ``agentic_review._render_diff`` output, which appends untracked
    NEW files as raw content under a ``# repo <id> NEW FILE <path>`` marker with no
    '+' prefix. Handling both matters: a code run's brand-new files (e.g. a new
    controller/scheduler) are exactly where these bugs live."""
    out: list[str] = []
    in_new_file = False
    for ln in diff.splitlines():
        if ln.startswith("# ") and " NEW FILE " in ln:
            in_new_file = True                       # raw new-file content follows
            continue
        if ln.startswith("# repo "):                 # a 'changes vs base' header ends a new-file block
            in_new_file = False
            continue
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append(ln[1:])
        elif in_new_file:
            out.append(ln)
    return out


def _read_fields(lines: list[str]) -> set[str]:
    keys: set[str] = set()
    for ln in lines:
        keys.update(_READ_RE.findall(ln))
        keys.update(_HGET_RE.findall(ln))
    return keys


def _written_fields(lines: list[str], framework_fields: frozenset[str]) -> set[str]:
    keys: set[str] = set(framework_fields)
    for ln in lines:
        # (a) enum constant declared alone on a line -> written via `.name()`.
        m = _ENUM_CONST_RE.match(ln)
        if m and "//" not in ln:
            keys.add(m.group(1))
        # (b) literal field written by a persistence call on this line.
        if _WRITE_VERB.search(ln):
            keys.update(_LITERAL_TOKEN_RE.findall(ln))
    return keys


def check_field_consistency(
    diff: str, *, framework_fields: frozenset[str] = DEFAULT_FRAMEWORK_FIELDS,
    extra_write_text: str = "",
) -> list[Finding]:
    """Flag a hash/map field that is READ by the change but never WRITTEN anywhere.

    Reading a key nothing writes is almost always a silent-data bug: the read
    returns null/0 and downstream logic takes a wrong branch (cbabbf9c: expiry
    read absent counters -> always CLOSED_NONE_PAID).

    READS come from the change's added lines only. WRITES are searched over a BROADER
    scope — the added lines PLUS ``extra_write_text`` (the full text of every touched/
    modified file) — so a field read in an added hunk but written by UNCHANGED code in
    the same file is NOT a false positive. New files are already fully in the diff, so
    only modified files need supply extra text.
    """
    added = _added_lines(diff)
    reads = _read_fields(added)
    writes = _written_fields(added + extra_write_text.splitlines(), framework_fields)
    findings = []
    for key in sorted(reads - writes):
        findings.append(Finding(
            check="field_consistency",
            severity="blocker",
            key=key,
            detail=(f'field "{key}" is read via .get() but never written in the '
                    f"change-set (no literal write, enum-name write, or known "
                    f"framework field) — the read will return null/default"),
        ))
    return findings


def _entity_set(block: str) -> set[str]:
    """Upper-cased entity tokens from a comma-separated pack block."""
    from app.core.domain.registry import prompt_block

    raw = prompt_block(block, "")
    return {p.strip().upper() for p in raw.split(",") if p.strip()}


def _authority_entities() -> set[str]:
    """Every spelling of "the switch itself" an error-code table might carry.

    A UNION, deliberately, of three sources:

      * the pack's `contract_gate_authority_entities`;
      * the label of the participant the pack marks `is_authority` — the
        spelling the pack's own example error-code table uses, and therefore
        the one the extractor imitates (`Authority`);
      * `cert_roles.authority_role()` — the certification WIRE token (`NPCI`),
        which is what this function used to return *alone*.

    The union exists because the two spellings are genuinely both in play and
    are not the same string. Reading only the wire token — the old behaviour —
    meant the authority half matched nothing whenever the extractor followed
    the example table, which is the failure the previous comment here claimed
    to have already fixed. Reading only the label would have broken the
    reverse case, including plans already stored with `NPCI` in this column.

    Over-matching is the safe direction: a false positive here asks a human
    "should this code be emitted?", while a false negative is a gate that
    passes by being blind.
    """
    out = _entity_set("contract_gate_authority_entities")
    try:
        from app.core.domain.contract import participants_of
        from app.core.domain.registry import get_active_pack
        from app.services.cert_roles import authority_role

        out |= {p.label.upper() for p in participants_of(get_active_pack())
                if p.is_authority and p.label}
        out.add((authority_role() or "").upper())
    except Exception:  # noqa: BLE001 — a malformed pack must not break the gate
        pass
    return {t for t in out if t}


def _must_emit_codes(declared_codes: list[dict]) -> list[dict]:
    """Codes the SWITCH is responsible for emitting: authority-owned ones, plus
    technical-decline (TD) validation codes owned by the participants the pack
    names in `contract_gate_partner_td_entities`.

    Business declines raised by a counterparty (the network pack's
    U28/U30/U67/U69/RB) arrive on responses and are not literal-emitted here.

    All three inputs are pack data, including the name of the technical class
    itself: the extractor always writes the key `td_bd`, but the VALUE is the
    domain's own two-letter class, and packs do not agree on it — the network
    pack classifies TD/BD, the NLLN pack SE/PD. Matching a literal `"TD"` meant
    a domain whose technical class is spelled anything else classified every
    participant code as a business decline and dropped it.

    Hardcoding one ecosystem's spelling meant every OTHER domain's codes
    matched nothing and were silently never required to be emitted — a gate
    that passed by being blind. A pack that declares no partner entities gets
    an empty set rather than one ecosystem's hardcoded partner token: no findings is an
    honest answer for a domain whose vocabulary we do not know, a wrong finding
    is not.
    """
    from app.core.domain.registry import prompt_block

    out = []
    authority = _authority_entities()
    partner_td = _entity_set("contract_gate_partner_td_entities")
    technical = prompt_block("contract_gate_technical_class", "").strip().upper()
    for c in declared_codes:
        entity = str(c.get("entity") or "").upper()
        td_bd = str(c.get("td_bd") or "").upper()
        if entity in authority or (
            technical and entity in partner_td and td_bd == technical
        ):
            out.append(c)
    return out


def check_error_code_emission(diff: str, declared_codes: list[dict],
                              corpus_text: str = "") -> list[Finding]:
    """Flag a platform-side error code the plan declares but the code never emits
    as a string literal ON ANY PATH (cbabbf9c emitted msg.substring(0,2)).

    The literal is searched in the change's added lines AND ``corpus_text`` (the
    changed repos' existing source). This is essential for the idiomatic pattern
    where codes are CENTRAL NAMED CONSTANTS — ``public static final String
    TXN_NOT_FOUND = "U16";`` in an ErrorCodes class, referenced elsewhere as
    ``PlatformErrorCodes.TXN_NOT_FOUND``. Diff-only scope falsely flags a change that
    correctly emits via the constant name, because the raw ``"U16"`` literal lives
    only in the (unchanged) constants file. Only a code whose literal exists NOWHERE
    — no constant definition, no direct emission — is a real 'never emitted' bug."""
    blob = "\n".join(_added_lines(diff))
    if corpus_text:
        blob += "\n" + corpus_text
    findings = []
    for c in _must_emit_codes(declared_codes):
        code = str(c.get("code") or "").strip()
        if not code:
            continue
        # Emitted as a quoted literal somewhere (`"YA"`, `setErrorCd("YA")`, ...).
        if re.search(r'"' + re.escape(code) + r'"', blob):
            continue
        findings.append(Finding(
            check="error_code_emission",
            severity="blocker",
            key=code,
            detail=(f'declared error code "{code}" ({c.get("entity")}/{c.get("td_bd")}: '
                    f'{str(c.get("description") or "")[:80]}) is never emitted as a '
                    f"string literal on any code path"),
        ))
    return findings


# ── C3 static safety checks ───────────────────────────────────────────────────
# These need PER-FILE line attribution (ordering and plan membership are per-file
# properties), which the flat _added_lines view throws away.


def _file_sections(diff: str) -> list[dict]:
    """Segment a ``_render_diff`` blob into per-file ``{path, new, added, removed}``
    sections. NEW FILE blocks carry raw content as ``added``; tracked git-diff hunks
    yield '+'/'-' lines under the path from ``+++ b/<path>`` (``--- a/<path>`` for a
    tracked deletion, whose '+++' side is /dev/null)."""
    sections: list[dict] = []
    cur: dict | None = None
    minus_path = ""
    for ln in diff.splitlines():
        if ln.startswith("# ") and " NEW FILE " in ln:
            cur = {"path": ln.split(" NEW FILE ", 1)[1].strip(), "new": True,
                   "added": [], "removed": []}
            sections.append(cur)
            continue
        if ln.startswith("# repo "):                 # section marker ends any new-file block
            cur = None
            continue
        if ln.startswith("#  "):                     # manifest line — metadata, not content
            continue
        if cur is not None and cur["new"]:
            cur["added"].append(ln)
            continue
        if ln.startswith("diff --git "):
            cur = None
            minus_path = ""
            continue
        if ln.startswith("--- "):
            p = ln[4:].strip()
            minus_path = p[2:] if p.startswith("a/") else p
            continue
        if ln.startswith("+++ "):
            p = ln[4:].strip()
            p = p[2:] if p.startswith("b/") else p
            if p == "/dev/null":
                p = minus_path
            cur = {"path": p, "new": False, "added": [], "removed": []}
            sections.append(cur)
            continue
        if cur is not None and not cur["new"]:
            if ln.startswith("+"):
                cur["added"].append(ln[1:])
            elif ln.startswith("-"):
                cur["removed"].append(ln[1:])
    return sections


_PUBLISH_RE = re.compile(r"\b(?:applicationEventPublisher|eventPublisher)\.publishEvent\s*\(")
# Persistence verbs that must COMPLETE before an event is published — the event's
# consumers read the state these calls write.
_PERSIST_CALL_RE = re.compile(
    r"\b(?:appendLog|createTransaction|putAll|setSessionField)\s*\(|\.put\s*\(")
_PERSIST_WINDOW = 15


def check_publish_before_persist(diff: str) -> list[Finding]:
    """Flag a publishEvent added BEFORE the persistence call that follows it in the
    same method region (a persist within the next 15 added lines, none in the
    previous 15) — that ordering inversion means event consumers read state that
    isn't written yet. Warning-only: the line-window heuristic can't see across
    files or through extracted helpers, so it stays advisory."""
    findings: list[Finding] = []
    try:
        for sec in _file_sections(diff):
            if not sec["path"].endswith(".java"):
                continue
            lines = sec["added"]
            for i, ln in enumerate(lines):
                if not _PUBLISH_RE.search(ln):
                    continue
                before = any(_PERSIST_CALL_RE.search(x)
                             for x in lines[max(0, i - _PERSIST_WINDOW):i])
                after = any(_PERSIST_CALL_RE.search(x)
                            for x in lines[i + 1:i + 1 + _PERSIST_WINDOW])
                if after and not before:
                    findings.append(Finding(
                        check="publish_before_persist", severity="warning",
                        key=sec["path"].rsplit("/", 1)[-1],
                        detail=(f"publishEvent fires before the persistence call that follows "
                                f"it in {sec['path']} — event consumers read state that is not "
                                f"written yet: `{ln.strip()[:120]}`"),
                        file=sec["path"],
                        suggested_fix=("Publish from the completion of the persistence chain "
                                       "(.doOnSuccess/.then after the write) so state is durable "
                                       "before consumers run.")))
    except Exception:  # noqa: BLE001 — fail-open: a checker error must never poison the gate
        return []
    return findings


_COMMENT_PREFIXES = ("//", "*", "/*", "*/")


def _norm_tail(path: str) -> str:
    """Last-two path segments, lowercased — plan paths and diff paths rarely share a
    repo-root prefix, but dir/basename is stable across both renderings."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:]).lower()


def check_shared_file_behavior_edits(diff: str, planned_paths: set[str],
                                     directives_text: str) -> list[Finding]:
    """Flag a MODIFIED file the ratified plan never named whose hunks remove or
    rewrite existing non-comment lines (a '-' line not re-added identically) —
    this is how a shared handler loses a REQ_TXN_CONFIRMATION publish nobody
    asked to touch. Files named in the directives are sanctioned. Blocker."""
    findings: list[Finding] = []
    try:
        if not planned_paths:
            # Legacy/no-analysis runs have no plan to test membership against —
            # flagging EVERY modified file would drown the gate in noise.
            return []
        planned = {_norm_tail(p) for p in planned_paths if p}
        dtext = (directives_text or "").lower()
        for sec in _file_sections(diff):
            if sec["new"]:
                continue
            if _norm_tail(sec["path"]) in planned:
                continue
            base = sec["path"].rsplit("/", 1)[-1]
            if base and base.lower() in dtext:
                continue
            added_stripped = {x.strip() for x in sec["added"]}
            lost = [x for x in sec["removed"]
                    if x.strip() and not x.strip().startswith(_COMMENT_PREFIXES)
                    and x.strip() not in added_stripped]
            if lost:
                findings.append(Finding(
                    check="shared_file_behavior_edit", severity="blocker",
                    key=base,
                    detail=(f"behaviour-line edit in a file the plan never named: {sec['path']} "
                            f"removes/rewrites {len(lost)} existing line(s), "
                            f"e.g. `{lost[0].strip()[:120]}`"),
                    file=sec["path"],
                    suggested_fix=("Revert the edit to this shared file, or get the plan "
                                   "amended to name it.")))
    except Exception:  # noqa: BLE001 — fail-open
        return []
    return findings


def _consequential_leg_re() -> "re.Pattern[str] | None":
    """Regex matching the call sites that ARE this ecosystem's consequential
    legs — the ones whose presence the ratified plan must own.

    Pack data (`prompt_blocks.contract_gate_consequential_leg_patterns`)
    because the symbols are ONE codebase's: the six Java names this used to
    hardcode came from a single payments switch. None when the pack declares
    none, and the check then SKIPS — the same posture `error_code_pattern_of`
    takes. Falling back to another domain's symbol alphabet either matches
    nothing (an always-on gate that silently does nothing) or matches an
    unrelated token, and this gate emits an operator-visible finding.

    Only symbols worth FLAGGING belong here. The list this replaced also named
    three debit symbols that an in-loop filter then discarded on every run.
    """
    from app.core.domain.registry import prompt_block

    raw = (prompt_block("contract_gate_consequential_leg_patterns", "") or "").strip()
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error:
        # A pack shipping an invalid regex has no opinion we can act on; skip
        # rather than fail every gate run.
        return None


# CamelCase / SNAKE_CASE word components of a symbol, used to ask "do the
# directives talk about this leg at all". Short components (req, txn, get) match
# too much to be evidence of anything.
_SYMBOL_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")
_MIN_EVIDENCE_WORD = 5


def _leg_is_declared(token: str, directives_text: str) -> bool:
    """True if the ratified directives plausibly talk about `token`'s leg.

    Deliberately loose, as it always was: an exact symbol match would flag
    every leg, since directives are written in business language, not Java.
    Matching on the symbol's own long word components generalises what used to
    be a hardcoded "does the word 'credit' appear" test.
    """
    if not directives_text:
        return False
    haystack = directives_text.lower()
    if token.lower() in haystack:
        return True
    return any(w.lower() in haystack
               for w in _SYMBOL_WORD_RE.findall(token)
               if len(w) >= _MIN_EVIDENCE_WORD)


def check_declared_legs(diff: str, directives_text: str) -> list[Finding]:
    """Flag an added consequential call site whose leg the ratified directives
    never declare — a leg the plan doesn't own is exactly the 'consolidated vs
    per-participant' drift class. Advisory (warning): the match against
    directives is deliberately loose.

    Skips entirely when the active pack declares no leg symbols — see
    `_consequential_leg_re`.
    """
    findings: list[Finding] = []
    try:
        pattern = _consequential_leg_re()
        if pattern is None:
            return []
        seen: set[tuple[str, str]] = set()
        for sec in _file_sections(diff):
            for ln in sec["added"]:
                for tok in pattern.findall(ln):
                    if _leg_is_declared(tok, directives_text) or (tok, sec["path"]) in seen:
                        continue
                    seen.add((tok, sec["path"]))
                    findings.append(Finding(
                        check="declared_leg", severity="warning",
                        key=tok,
                        detail=(f"call site not traceable to a declared leg "
                                f"directive: {tok} in {sec['path']}"),
                        file=sec["path"],
                        suggested_fix=("Confirm the ratified plan declares this leg; if it "
                                       "does not, raise a decision question instead of inventing "
                                       "a leg.")))
    except Exception:  # noqa: BLE001 — fail-open
        return []
    return findings


_VALUE_KEY_RE = re.compile(r'@Value\(\s*"\$\{([^}:"]+)')
# A dotted lowercase config-key token, >= 3 segments (split.session.ttl.seconds) —
# two-segment tokens are mostly method calls / filenames, not keys.
_PLAN_KEY_RE = re.compile(r"\b([a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*){2,})\b")
# Package names and file paths are dotted-lowercase too — never config keys.
_NON_KEY_PREFIXES = ("com.", "org.", "net.", "java.", "javax.", "io.", "in.")
_NON_KEY_SUFFIXES = (".java", ".xml", ".xsd", ".xjb", ".yml", ".yaml",
                     ".properties", ".json", ".md", ".sql")


def check_config_keys_declared(diff: str, plan_text: str) -> list[Finding]:
    """Flag a config key the plan promises that nothing in the change binds — a
    promised-but-unbound key means the behaviour silently runs on a hardcoded
    default (cbabbf9c's 600s TTL). A key present verbatim anywhere in the added
    lines (application.yml, getProperty) counts as bound, not just @Value."""
    findings: list[Finding] = []
    try:
        plan_keys: set[str] = set()
        for tok in _PLAN_KEY_RE.findall(plan_text or ""):
            if tok.startswith(_NON_KEY_PREFIXES) or tok.endswith(_NON_KEY_SUFFIXES):
                continue
            plan_keys.add(tok)
        if not plan_keys:
            return []
        added = _added_lines(diff)
        blob = "\n".join(added)
        bound: set[str] = set()
        for ln in added:
            bound.update(k.strip() for k in _VALUE_KEY_RE.findall(ln))
        for key in sorted(plan_keys - bound):
            if key in blob:
                continue
            findings.append(Finding(
                check="config_key_declared", severity="warning",
                key=key,
                detail=f'plan promises config key "{key}" but no @Value binds it in the change-set',
                suggested_fix=(f'Add the @Value("${{{key}}}") binding (or amend the plan to '
                               f"drop the key).")))
    except Exception:  # noqa: BLE001 — fail-open
        return []
    return findings


def run_contract_gate(diff: str, declared_codes: list[dict] | None = None, *,
                      extra_write_text: str = "",
                      corpus_text: str = "",
                      planned_paths: set[str] | None = None,
                      directives_text: str = "",
                      plan_text: str = "") -> GateResult:
    """Run all deterministic contract checks over a change-set diff.

    ``extra_write_text`` is the full text of modified files (see
    ``check_field_consistency``) — widens the write-scope so an existing field the
    change reads is not falsely flagged. Empty is safe (diff-only scope).

    ``corpus_text`` is the changed repos' existing source (bounded), used by
    ``check_error_code_emission`` so a declared code emitted via a NAMED CONSTANT
    defined in an unchanged file is not falsely flagged. Empty is safe (diff-only).

    ``planned_paths`` / ``directives_text`` / ``plan_text`` feed the C3 static
    safety checks; all default to empty, which safely disables the checks that
    depend on them (each check is also internally fail-open)."""
    result = GateResult()
    result.findings.extend(check_field_consistency(diff, extra_write_text=extra_write_text))
    if declared_codes:
        result.findings.extend(check_error_code_emission(diff, declared_codes, corpus_text=corpus_text))
    result.findings.extend(check_publish_before_persist(diff))
    result.findings.extend(check_shared_file_behavior_edits(
        diff, planned_paths or set(), directives_text))
    result.findings.extend(check_declared_legs(diff, directives_text))
    result.findings.extend(check_config_keys_declared(diff, plan_text))
    return result
