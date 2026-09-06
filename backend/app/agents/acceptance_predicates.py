# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic acceptance predicates — the tier-1 completeness check (R4).

A planned change is decomposed into atomic, MACHINE-CHECKABLE acceptance predicates. Each is
verified by CODE (string/regex over the actual diff) — **zero false-positive, zero false-negative**
for the structural class. This is the deterministic counterpart to the non-deterministic LLM
completeness judge (`plan_fidelity`): where the LLM *guesses* whether a behaviour is present (and
hallucinated "purposeRemark not forwarded" when `setPurposeRemark` was right there), a predicate
*verifies* it with certainty.

Two payoffs, one mechanism (this is why accuracy and iteration-count are the same problem):
  1. ACCURACY — a satisfied predicate cannot be a phantom; an unmet one is a real gap. It can BLOCK
     reliably (unlike the LLM, which we had to declaw to advisory).
  2. FEWEST ITERATIONS — an unmet predicate is a PRECISE instruction ("`ApiMessageAssembler.java`
     must add a call to `setPurposeRemark` — it doesn't"), and the SAME predicates are handed to the
     code agent as its definition-of-done so it self-checks BEFORE declaring done — most gaps never
     reach a review round.

Predicate kinds (all evaluated against a unified diff / `agentic_review._render_diff` output):
  - ``file_touched``   {path}                  — the change creates/modifies `path` (matched by basename)
  - ``added_in_file``  {file, contains|regex}  — a literal/regex matches an ADDED ('+') line in `file`
  - ``added_anywhere`` {contains|regex}        — a literal/regex matches an ADDED line in any NON-TEST file
                                                  (``src/test/**`` is excluded — a production-behaviour token
                                                  that exists ONLY in test code means the behaviour is absent)
  - ``no_stub``        {}                       — NO TODO/FIXME/stub/UnsupportedOperationException added

Every predicate carries a human-readable ``desc`` so a failure is an actionable instruction.

**Fail-safe direction:** a predicate the checker cannot evaluate (malformed, unknown kind, empty
diff) is reported ``unknown`` — NEVER silently ``satisfied``. The caller decides how to treat
``unknown`` (we treat it as non-blocking, since a checker gap must not invent a blocker — accuracy
cuts both ways). Pure functions; no I/O; never raises.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import re
from dataclasses import dataclass

# A stub/placeholder left in produced code = not done. High-precision, low-false-positive set
# (mirrors agentic_orchestrator._STUB_RE so the two completeness signals agree).
_STUB_RE = re.compile(r"\bTODO\b|\bFIXME\b|not[\s_]?implemented|UnsupportedOperationException", re.I)

# Markers _render_diff emits for untracked NEW files (git diff omits them): "# repo <id> NEW FILE <path>".
_NEWFILE_RE = re.compile(r"^#\s*repo\s+\S+\s+NEW FILE\s+(?P<path>.+?)\s*$")
_DELFILE_RE = re.compile(r"^#\s*repo\s+\S+\s+DELETED\s+(?P<path>.+?)\s*$")


def _basename(p: str | None) -> str:
    return (p or "").replace("\\", "/").rstrip("/").split("/")[-1].lower()


# Maven/Gradle test-source root. A production-behaviour predicate must not be satisfied by a token
# that lives only under here — the production code would still be missing it (see `added_anywhere`).
_TEST_PATH_RE = re.compile(r"(^|/)src/test/", re.I)


def _is_test_path(p: str | None) -> bool:
    return bool(_TEST_PATH_RE.search((p or "").replace("\\", "/")))


@dataclass
class PredicateResult:
    predicate: dict
    status: str            # "satisfied" | "unmet" | "unknown"
    evidence: str | None    # the matching added line (satisfied) or the reason (unmet/unknown)

    @property
    def desc(self) -> str:
        return str(self.predicate.get("desc") or self.predicate.get("kind") or "predicate")


def parse_diff(diff_text: str) -> dict[str, dict]:
    """Parse a unified diff (or ``agentic_review._render_diff`` output) into
    ``{file_path: {"added": [lines], "removed": [lines]}}``.

    Handles BOTH standard `+`/`-` hunks AND `_render_diff`'s "# repo .. NEW FILE <path>" blocks
    (whose following lines are the full new content, un-prefixed, until the next marker)."""
    files: dict[str, dict] = {}
    cur: str | None = None
    in_new_file = False  # inside a "# repo .. NEW FILE" raw-content block

    def _file(path: str) -> dict:
        return files.setdefault(path, {"added": [], "removed": []})

    for raw in (diff_text or "").splitlines():
        m_new = _NEWFILE_RE.match(raw)
        if m_new:
            cur = m_new.group("path"); _file(cur); in_new_file = True
            continue
        if _DELFILE_RE.match(raw):
            cur = None; in_new_file = False
            continue
        if raw.startswith("# repo "):              # a "(changes vs base)" section header
            in_new_file = False
            continue
        if raw.startswith("diff --git"):
            in_new_file = False
            # "diff --git a/<x> b/<y>"
            seg = raw.split(" b/", 1)
            cur = seg[1].strip() if len(seg) == 2 else None
            if cur:
                _file(cur)
            continue
        if raw.startswith("+++ "):
            in_new_file = False
            p = raw[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            cur = None if (not p or p == "/dev/null") else p
            if cur:
                _file(cur)
            continue
        if raw.startswith("--- ") or raw.startswith("@@"):
            continue
        if cur is None:
            continue
        if in_new_file:
            files[cur]["added"].append(raw)
        elif raw.startswith("+") and not raw.startswith("+++"):
            files[cur]["added"].append(raw[1:])
        elif raw.startswith("-") and not raw.startswith("---"):
            files[cur]["removed"].append(raw[1:])
    return files


def _match_files(target: str, parsed: dict[str, dict]) -> list[str]:
    """Files in the diff matching `target` by basename or path-suffix (robust to repo-prefix /
    absolute-vs-relative differences between plan paths and diff paths)."""
    t = (target or "").replace("\\", "/").strip().lower()
    if not t:
        return []
    tb = _basename(t)
    out = []
    for path in parsed:
        pl = path.lower()
        # Exact basename, or a path-suffix anchored at a '/' boundary. NOT a bare
        # pl.endswith(t): that mis-matches across the basename ("assembler.java"
        # would satisfy against "ReqTransferAssembler.java"), false-satisfying the predicate.
        if _basename(pl) == tb or pl.endswith("/" + t):
            out.append(path)
    return out


def _compile(pred: dict):
    """Return a compiled matcher fn(line)->bool from `contains` (literal) or `regex`, or None."""
    if pred.get("contains"):
        needle = str(pred["contains"])
        return lambda line: needle in line
    if pred.get("regex"):
        try:
            rx = re.compile(str(pred["regex"]))
        except re.error:
            return None
        return lambda line: rx.search(line) is not None
    return None


def check_predicate(pred: dict, parsed: dict[str, dict]) -> PredicateResult:
    """Evaluate one predicate against the parsed diff. Never raises."""
    try:
        kind = str(pred.get("kind") or "").strip()

        if kind == "file_touched":
            hits = _match_files(pred.get("path") or pred.get("file") or "", parsed)
            if hits:
                return PredicateResult(pred, "satisfied", hits[0])
            return PredicateResult(pred, "unmet", f"no diffed file matches {pred.get('path') or pred.get('file')!r}")

        if kind in ("added_in_file", "added_anywhere"):
            match = _compile(pred)
            if match is None:
                return PredicateResult(pred, "unknown", "predicate has no 'contains' or valid 'regex'")
            if kind == "added_in_file":
                target = pred.get("file") or pred.get("path") or ""
                files = _match_files(target, parsed)
                if not files:
                    return PredicateResult(pred, "unmet", f"file {target!r} not touched at all")
                scope = [(f, parsed[f]["added"]) for f in files]
            else:
                # NON-TEST files only: a production token found solely in src/test/** does not
                # prove the production behaviour exists (that was a real false-satisfy — a producer
                # that lived only in test code passed this gate).
                scope = [(f, d["added"]) for f, d in parsed.items() if not _is_test_path(f)]
            for f, added in scope:
                for ln in added:
                    if match(ln):
                        return PredicateResult(pred, "satisfied", f"{_basename(f)}: {ln.strip()[:120]}")
            tgt = (" in " + str(pred.get('file'))) if kind == "added_in_file" else ""
            return PredicateResult(pred, "unmet", f"no added line matches {pred.get('contains') or pred.get('regex')!r}{tgt}")

        if kind == "no_stub":
            for f, d in parsed.items():
                for ln in d["added"]:
                    if _STUB_RE.search(ln):
                        return PredicateResult(pred, "unmet", f"stub/placeholder added in {_basename(f)}: {ln.strip()[:100]}")
            return PredicateResult(pred, "satisfied", None)

        return PredicateResult(pred, "unknown", f"unknown predicate kind {kind!r}")
    except Exception as e:  # noqa: BLE001 — a checker bug must never fabricate a blocker
        return PredicateResult(pred, "unknown", f"checker error: {type(e).__name__}")


def check_predicates(predicates: list[dict], diff_text: str) -> list[PredicateResult]:
    """Evaluate every predicate against the diff. Pure + deterministic. Never raises."""
    parsed = parse_diff(diff_text)
    return [check_predicate(p, parsed) for p in (predicates or []) if isinstance(p, dict)]


def unmet(results: list[PredicateResult]) -> list[PredicateResult]:
    return [r for r in results if r.status == "unmet"]


_BARE_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_bare_token(pred: dict) -> bool:
    """True when the predicate's matcher is ONE bare identifier (no punctuation, no
    structure — e.g. ``contains: "purposeRemark"``). Such predicates are naming-
    sensitive: a correct implementation under a different (equally valid) name
    false-unmets them. Callers that IMPERATIVELY act on unmet results (the in-loop
    convergence nudge) should treat these as advisory — the nudge otherwise tells
    the agent to inject a literal token into already-correct code."""
    if pred.get("regex"):
        return False
    return bool(_BARE_TOKEN_RE.match(str(pred.get("contains") or "").strip()))


def feedback_block(results: list[PredicateResult]) -> str:
    """Actionable, deterministic instruction for the code agent — only the UNMET predicates, each a
    precise 'deliver exactly this' line. Empty string when nothing is unmet."""
    miss = unmet(results)
    if not miss:
        return ""
    lines = ["DEFINITION OF DONE — the following acceptance checks are deterministically UNMET; "
             "deliver EXACTLY these (verified by code, not opinion):"]
    for r in miss:
        lines.append(f"  - {r.desc}  [unmet: {r.evidence}]")
    return "\n".join(lines)


def summarize(results: list[PredicateResult]) -> dict:
    """Counts + the unmet descriptions, for the shadow event / verdict."""
    by = {"satisfied": 0, "unmet": 0, "unknown": 0}
    for r in results:
        by[r.status] = by.get(r.status, 0) + 1
    return {**by, "total": len(results),
            "unmet_items": [{"desc": r.desc, "why": r.evidence} for r in unmet(results)][:15]}


# ── LLM predicate extraction (NOT pure — the only I/O in this module) ──────────────────────────────
# The deterministic CHECKER above is the trustworthy half. This half asks an LLM to TRANSLATE the
# ratified plan into the predicate schema — a fallible step — but its output is then VERIFIED by the
# pure checker against the real diff, so a bad predicate can only mislabel a check, never fabricate a
# fact. Fail-open: any error → [] (no predicates → the deterministic gate simply does not fire).

_EXTRACT_SYSTEM = load_prompt("agents/acceptance_predicates/extract_system.md")


async def extract_predicates(plan_text: str, *, max_predicates: int = 15) -> list[dict]:
    """LLM: ratified plan → structured predicates (each later VERIFIED by the pure checker). Fail-open → []."""
    if not (plan_text or "").strip():
        return []
    from app.core.llm import call_llm
    from app.core.json_recovery import parse_llm_json
    from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE
    user = (f"RATIFIED PLAN:\n{wrap_untrusted(plan_text[:8000], 'PLAN')}\n\n"
            "Emit the acceptance predicates a correct diff must satisfy.")
    try:
        raw = await call_llm(system=_EXTRACT_SYSTEM + ANTI_INJECTION_CLAUSE,
                             messages=[{"role": "user", "content": user}],
                             max_tokens=1600, agent_name="acceptance_predicates")
    except Exception:  # noqa: BLE001 — fail-open: no predicates rather than a broken gate
        return []
    data = await parse_llm_json(raw, fallback=None)
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for p in (data.get("predicates") or []):
        if isinstance(p, dict) and str(p.get("kind") or "").strip():
            out.append({k: p[k] for k in ("kind", "file", "path", "contains", "regex", "desc") if p.get(k)})
    return out[:max_predicates]
