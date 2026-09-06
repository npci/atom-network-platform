# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Goal-verifier reviewer — pure core (schema, parse, gap-fingerprint, quorum).

The convergence machinery grok-build has and our legacy review loop lacks
(`goal_classifier.rs` parity). Kept PURE — no I/O, no app imports beyond config/
logging — so every rule here is unit-testable in isolation (the panel runner,
evidence packet, and orchestrator wiring live in ``agentic_goal_verifier.py``).

Why this exists: the legacy loop re-derived an ever-growing checklist each round
and tracked stalls by a coarse ``basename|category`` key that churned across files
and never fired — a hard change rode the round cap ($43, run cc9e81b0). Grok's
answer is (1) a verdict schema whose ``evidence`` is MANDATORY (no rubber-stamp),
(2) a ``path:line`` gap-fingerprint that is stable across scratch-path/panel churn
so "changed nothing this round" is detectable, and (3) a strict-majority quorum
with a decisive-refute short-circuit. This module is those three, verbatim in
spirit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# ── Verdict schema (grok goal_verifier_prompt.md:163-172 parity) ───────────────

_CONFIDENCE = ("high", "medium", "low")


class Blocking(str, Enum):
    """Why a refute is not model-fixable. ``none`` loops back to the code agent;
    ``contradiction``/``unverifiable`` need a human decision, not a retry."""
    NONE = "none"
    CONTRADICTION = "contradiction"     # objective/plan internally precludes itself
    UNVERIFIABLE = "unverifiable"       # no honest evidence path in THIS environment

    @classmethod
    def parse(cls, raw) -> "Blocking":
        s = str(raw or "").strip().lower()
        return {"contradiction": cls.CONTRADICTION,
                "unverifiable": cls.UNVERIFIABLE}.get(s, cls.NONE)

    @property
    def is_blocking(self) -> bool:
        return self is not Blocking.NONE


@dataclass
class VerifierFinding:
    """One gap the implementer acts on. ``kind``: bug (defect in shipped behaviour)
    | gap (unmet plan criterion / missing evidence) | todo (TODO/stub/skip left in)."""
    kind: str = "gap"
    location: str = ""      # "path:line" when code-related, else where
    detail: str = ""        # one concrete line

    @property
    def empty(self) -> bool:
        return not (self.kind.strip() or self.location.strip() or self.detail.strip())


_ALLOWED_KINDS = frozenset({"bug", "gap", "todo"})


@dataclass
class SkepticVerdict:
    """One skeptic's structured vote. ``refuted`` + non-empty ``evidence`` are
    mandatory for a REAL vote — a parse that lacks either is rejected (see
    :func:`parse_verdict_json`) and the caller substitutes :func:`skeptic_failure`."""
    refuted: bool
    evidence: str
    findings: list[VerifierFinding] = field(default_factory=list)
    confidence: str = "medium"
    blocking: Blocking = Blocking.NONE
    details_md: str = ""
    skeptic_idx: int = 0
    synthetic: bool = False           # True = infra/parse failure → fail-closed refute
    fallback_note: str = ""           # stable fingerprint source for a synthetic refute


def _coerce_findings(raw) -> list[VerifierFinding]:
    out: list[VerifierFinding] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        location = str(item.get("location") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if not (location or detail):
            continue                       # content-less finding (kind alone) is noise — drop
        kind = str(item.get("kind") or "gap").strip().lower()
        if kind not in _ALLOWED_KINDS:
            kind = "gap"
        out.append(VerifierFinding(kind=kind, location=location, detail=detail))
    return out


def parse_verdict_json(obj) -> SkepticVerdict | None:
    """Validate a parsed JSON verdict object into a :class:`SkepticVerdict`, or
    ``None`` when it fails the contract. FAIL-CLOSED: ``refuted`` must be a real
    bool AND ``evidence`` must be non-empty — this is the rule that closes the
    rubber-stamp failure mode (grok goal_classifier.rs:885-912). A ``None`` here is
    the caller's cue to record a synthetic refute, not to trust the output."""
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("refuted"), bool):
        return None
    evidence = str(obj.get("evidence") or "").strip()
    if not evidence:
        return None
    conf = str(obj.get("confidence") or "medium").strip().lower()
    if conf not in _CONFIDENCE:
        conf = "medium"
    return SkepticVerdict(
        refuted=obj["refuted"],
        evidence=evidence,
        findings=_coerce_findings(obj.get("findings")),
        confidence=conf,
        blocking=Blocking.parse(obj.get("blocking")),
        details_md=str(obj.get("details_md") or "").strip(),
    )


def skeptic_failure(skeptic_idx: int, reason: str) -> SkepticVerdict:
    """Synthetic ``refuted: true`` vote for an infra/parse/transport failure —
    "default to refuted if uncertain" enforced in code, not just the prompt
    (grok goal_classifier.rs:1585-1596). ``fallback_note`` keeps a repeated infra
    failure's fingerprint STABLE so it can still trip the stall guard."""
    note = f"verifier-infra:{reason}".strip()
    return SkepticVerdict(refuted=True, evidence=note, confidence="low",
                          blocking=Blocking.NONE, skeptic_idx=skeptic_idx,
                          synthetic=True, fallback_note=note)


# ── Gap fingerprint (grok goal_classifier.rs:1203-1278 parity) ─────────────────

# Per-attempt scratch prefixes embed run/repo ids that would make an identical gap
# fingerprint differently every round — collapse them to a literal so the stall
# guard can actually fire. Extend with our workspace root at call sites if needed.
_SCRATCH_RE = re.compile(r"(/tmp/|/var/folders/|/private/tmp/|/private/var/folders/)\S*")
_PATH_LINE_RE = re.compile(r"([\w./\-]+):(\d+)(?::\d+)?")


def normalize_scratch_paths(s: str) -> str:
    return _SCRATCH_RE.sub("<scratch>", s or "")


def extract_path_line_tokens(s: str) -> list[str]:
    """Lowercased ``path:line`` citations from one evidence string. A token counts
    only when the prefix looks path-ish (has ``/`` or ``.``) and the suffix is all
    digits; ``path:line:col`` tolerated (col dropped)."""
    toks: list[str] = []
    for m in _PATH_LINE_RE.finditer(normalize_scratch_paths(s)):
        path, line = m.group(1), m.group(2)
        if "/" in path or "." in path:
            toks.append(f"{path.lower()}:{line}")
    return toks


def gap_fingerprint(evidences: list[str]) -> str:
    """Stable fingerprint of a round's refute grounds, computed over RAW evidence
    (not the decorated ``[skeptic N, conf]`` bullets) so it is invariant to panel
    reorder and confidence churn but CHANGES when a cited line changes. Prefers
    ``path:line`` tokens; falls back to sorted trimmed lowercased non-empty lines.
    Empty input → ``""`` (treated as "no stable fingerprint" — never a repeat)."""
    toks: list[str] = []
    for ev in evidences or []:
        toks.extend(extract_path_line_tokens(ev))
    if not toks:
        for ev in evidences or []:
            for ln in normalize_scratch_paths(ev).splitlines():
                ln = ln.strip().lower()
                if ln:
                    toks.append(ln)
    if not toks:
        return ""
    return "\n".join(sorted(set(toks)))


def fingerprint_source(v: SkepticVerdict) -> str:
    """The string a refuter contributes to the round fingerprint: its raw model
    evidence, or its ``fallback_note`` for a synthetic infra-refute."""
    return v.fallback_note if v.synthetic else v.evidence


# ── Panel aggregation / quorum (grok goal_classifier.rs:1009-1033, 2214 parity) ─

class Outcome(str, Enum):
    ACHIEVED = "achieved"               # quorum passed, no decisive refute
    NOT_ACHIEVED = "not_achieved"       # ≥1 model-fixable refute → loop to code
    BLOCKED = "blocked"                 # every refuter non-fixable → needs a human
    FAIL_OPEN_ACHIEVED = "fail_open"    # no real verdict extractable → never block on infra


@dataclass
class PanelResult:
    outcome: Outcome
    refuted: bool
    gaps: list[VerifierFinding]              # union of refuting skeptics' findings
    gaps_summary: str                        # rendered, high→low confidence
    fingerprint: str
    blocking: Blocking                       # worst blocking-kind among refuters
    votes: list[SkepticVerdict]
    reason: str = ""


_CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def _render_gaps(refuters: list[SkepticVerdict]) -> str:
    lines: list[str] = []
    for v in sorted(refuters, key=lambda x: _CONF_RANK.get(x.confidence, 1)):
        head = f"- [skeptic {v.skeptic_idx}, {v.confidence}] {v.evidence}".rstrip()
        lines.append(head)
        for f in v.findings[:12]:
            loc = f" ({f.location})" if f.location else ""
            lines.append(f"    - {f.kind}{loc} — {f.detail}")
    return "\n".join(lines)


def aggregate_verdicts(votes: list[SkepticVerdict], *, cold_start_idx: int = 1) -> PanelResult:
    """Aggregate skeptic votes into a terminal outcome (grok "Variant-C" quorum).

    Approval needs a STRICT MAJORITY of the COLD panel (skeptic_idx >= ``cold_start_idx``):
    ``needed = cold_count // 2 + 1``. Skeptic 0 is a gatekeeper — its not-refuted vote
    does NOT count toward approval, but its refute still binds (a decisive high-confidence
    skeptic-0 refute alone fails the round). ``achieved = quorum AND not decisive_refute``.

    A rejection routes to BLOCKED only when EVERY refuter is non-``none`` blocking (needs a
    human); a single model-fixable refute keeps it NOT_ACHIEVED so the loop can progress.
    All-synthetic (no real verdict extracted) → FAIL_OPEN_ACHIEVED: an infra fault never
    blocks the user."""
    real = [v for v in votes if not v.synthetic]
    if not real:
        # EVERY skeptic hit an infra/parse failure — the VERIFIER is broken, not the work.
        # Fail CLOSED to the human gate: do NOT fail-open to "achieved" (that would present
        # unverified work as review-clean exactly when the verifier is down), and do NOT loop
        # the code agent (the code isn't the problem). grok treats a per-skeptic failure as a
        # refute; all-refute with no fixable path routes to needs-user.
        fp = gap_fingerprint([fingerprint_source(v) for v in votes])
        gap = VerifierFinding(kind="gap", location="",
                              detail=("Verifier could not obtain a verdict (all skeptics failed — "
                                      "likely an LLM/transport/credit outage). NOT verified."))
        return PanelResult(Outcome.BLOCKED, refuted=True, gaps=[gap],
                           gaps_summary="- verifier infrastructure failure (no verdict obtained)",
                           fingerprint=fp, blocking=Blocking.UNVERIFIABLE, votes=votes,
                           reason="all skeptics failed — verifier infra (fail-closed to human)")

    refuters = [v for v in votes if v.refuted]
    fp = gap_fingerprint([fingerprint_source(v) for v in refuters]) if refuters else ""

    # Skeptic-0 decisive refute: a high-confidence, model-fixable (non-blocking is fine)
    # gatekeeper refute fails the round on its own.
    decisive = any(v.refuted and v.skeptic_idx < cold_start_idx and v.confidence == "high"
                   for v in votes)

    cold = [v for v in votes if v.skeptic_idx >= cold_start_idx and not v.synthetic]
    # When there is no cold panel (panel size 1), skeptic 0 IS the judge.
    if not cold:
        approve_pool = [v for v in real]
        needed = 1
    else:
        approve_pool = cold
        needed = len(cold) // 2 + 1
    approvals = sum(1 for v in approve_pool if not v.refuted)
    quorum = approvals >= needed

    # ACHIEVED = the cold panel reached a STRICT MAJORITY and skeptic-0 did not decisively
    # refute. A MINORITY refuter does NOT block approval — that is the whole point of the
    # majority vote (resilience to one flaky/biased skeptic). (Earlier we also required
    # `not refuters`, i.e. UNANIMITY, which made the majority computation dead code and
    # re-opened the loop on a single hallucinated gap — grok's Variant-C exists to prevent
    # exactly that.) The minority refuter's gaps still surface on the non-achieved path.
    achieved = quorum and not decisive
    if achieved:
        return PanelResult(Outcome.ACHIEVED, refuted=False, gaps=[], gaps_summary="",
                           fingerprint="", blocking=Blocking.NONE, votes=votes)

    gaps = [f for v in refuters for f in v.findings]
    worst = Blocking.NONE
    if refuters and all(v.blocking.is_blocking for v in refuters):
        # every refuter needs a human — surface the most specific kind
        worst = (Blocking.CONTRADICTION
                 if any(v.blocking is Blocking.CONTRADICTION for v in refuters)
                 else Blocking.UNVERIFIABLE)
        return PanelResult(Outcome.BLOCKED, refuted=True, gaps=gaps,
                           gaps_summary=_render_gaps(refuters), fingerprint=fp,
                           blocking=worst, votes=votes,
                           reason="all refuters non-model-fixable")
    return PanelResult(Outcome.NOT_ACHIEVED, refuted=True, gaps=gaps,
                       gaps_summary=_render_gaps(refuters), fingerprint=fp,
                       blocking=worst, votes=votes)


# ── Stall tracking (grok goal_tracker.rs:1213-1229 parity) ─────────────────────

STALL_THRESHOLD = 2                    # two identical fingerprints in a row → stop
STRATEGIST_STALL_THRESHOLD = 5        # relaxed while a strategist restructure is in flight


def record_stall(prev_fp: str | None, prev_count: int, fp: str, *,
                 strategist_active: bool = False) -> tuple[int, bool]:
    """Update the stall streak. Returns ``(new_count, stalled)``. An empty ``fp``
    (no stable fingerprint this round) never counts as a repeat and resets nothing —
    it is a no-op so a degenerate round can't spuriously trip OR reset the guard."""
    if not fp:
        return prev_count, False
    count = prev_count + 1 if prev_fp == fp else 1
    threshold = STRATEGIST_STALL_THRESHOLD if strategist_active else STALL_THRESHOLD
    return count, count >= threshold
