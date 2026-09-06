# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Decision Ledger service (accuracy upgrade S1).

The ledger is append-only with per-question supersession: a new answer for an
existing question_key is appended with supersedes_id pointing at the entry it
replaces. The ACTIVE set (tip of each supersession chain) is what downstream
agents are bound by — superseded decisions never appear in the DECISIONS block.

build_decisions_block renders only system-controlled fields (kind, question,
directive/chosen) — never raw evidence prose — so PM free text does not cross
the prompt trust boundary as instruction-grade text.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models.change_analysis import DecisionLedgerEntry
from app.models.base import utcnow

logger = logging.getLogger(__name__)

# ── Question identity ────────────────────────────────────────────────────────
# A supersession chain is only as good as its key. The original code_decision key
# was f"code_decision:{question[:60]}" — the QUESTION PROSE, which an LLM rewrites
# every time it re-asks. One real run asked the same question seven times, produced
# seven distinct keys, superseded nothing, and injected all seven mutually
# contradictory answers into the prompt as simultaneously BINDING. The agent then
# flip-flopped between them for seventeen hours.
#
# Identity now comes from three signals, cheapest and most deterministic first:
#   1. ANCHOR key — a deterministic hash of the directive/plan item the question
#      blocks, not of the phrasing. Same blocked item ⇒ same key, always.
#   2. ARTIFACT overlap — the set of code artifacts a question names (file names,
#      CONSTANT_NAMES, camelCase identifiers, quoted literals). Two questions about
#      the same collision cite the same artifacts even when every sentence differs.
#      Measured on the real seven-escalation run: every pair shared BG, GP,
#      txnPurpose and NET-Common.xsd, at containment ≥ 0.62 for 18 of 21 pairs.
#   3. SEMANTIC similarity of the question text via embeddings.
#
# Signals 1 and 2 are deterministic and always available. Signal 3 is the most
# flexible but the least dependable: `embed_query` is FAIL-SOFT and returns a zero
# vector when the embedding gateway is down, so a design that leaned on it alone
# would quietly regress to prose-keying during an outage — the exact failure mode
# this module exists to prevent. Hence artifacts, not embeddings, are the backstop.

# Cosine band over question embeddings. Deliberately conservative: a false supersede
# silently BURIES a human's binding answer, which is worse than carrying a redundant
# entry, so the bar for automatic merging is high.
SIMILARITY_SUPERSEDE = 0.92   # ≥ this ⇒ same question; reuse the existing chain
SIMILARITY_RELATED = 0.80     # ≥ this ⇒ probably related; keep separate but SURFACE it

# Containment bands over the salient-artifact sets (|A∩B| / min(|A|,|B|) — containment
# rather than Jaccard, because a follow-up question that adds newly-discovered call
# sites legitimately has a much larger artifact set than the original ask).
ARTIFACT_SUPERSEDE = 0.75     # ≥ this, with enough artifacts to be meaningful
ARTIFACT_RELATED = 0.50
# Below this many shared artifacts, containment is noise ("NET-Common.xsd" alone
# links half the corpus). Two questions must agree on several specific things.
ARTIFACT_MIN_SHARED = 3

# After this many recorded answers on one key, a fresh ask on the same key is no
# longer a question the human can usefully answer — it's a platform defect (the
# agent cannot execute the answer it keeps being given). See `repeat_state`.
REPEAT_DEFECT_THRESHOLD = 2

_KEY_MAX = 128
_WS = re.compile(r"\s+")
_NON_KEY = re.compile(r"[^a-z0-9]+")

# Things that identify a piece of the codebase: source file names, SCREAMING_CASE
# constants, camelCase/PascalCase identifiers, quoted literals, and BARE short wire
# codes (BG, GP, QR).
#
# The bare-code alternative matters more than it looks. A wire code is very often the
# ONLY thing that distinguishes two otherwise identical questions ("txnPurpose in
# NET-Common.xsd should be BG" vs "…should be QR"), and agents write these codes
# unquoted at least as often as quoted. Without it, those two questions look 100%
# identical on artifacts and would wrongly collapse onto one chain — the same class of
# bug as the original 7-key fragmentation, in the opposite direction.
#
# Ordering is significant: `re.findall` with alternation takes the FIRST alternative
# that matches at a position, so filenames precede bare identifiers (otherwise "the network"
# would be clipped out of "NET-Common.xsd") and SCREAMING_CASE precedes the bare code
# (so "UTP_PURPOSE_CODES_LIST" is not shredded into "UTP").
#
# The bare-code branch deliberately requires a LEADING LETTER (`[A-Z][A-Z0-9]{1,5}`)
# rather than the looser `\b[A-Z0-9]{2,6}\b`. Admitting all-digit tokens would sweep in
# line numbers, years and counts ("NET-Common.xsd line 1060 … 3 times in 2024"), and
# those are the tokens most likely to be shared by UNRELATED questions about the same
# file — they would manufacture identity rather than evidence it. The cost is that a
# purely numeric wire code ("00") is not captured on its own; measured against real
# escalations that changes no merge outcome, because such a question always also names
# the field and the file, which is already ≥ ARTIFACT_MIN_SHARED. A numeric code IS
# still captured when quoted ("00"), which is how schema text writes it.
_ARTIFACT = re.compile(
    r'[A-Za-z0-9_-]+\.(?:xsd|xjb|java|xml|json|yaml|yml|sql|properties)'
    r'|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+'
    r'|[A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+'
    r'|"[A-Za-z0-9_.:-]{1,24}"'
    r'|(?<![A-Za-z0-9_"\'-])[A-Z][A-Z0-9]{1,5}(?![A-Za-z0-9_-])'
)
# Words that name an artifact-shaped token but carry no identity (they appear in
# nearly every schema/code question, so they inflate overlap without evidence).
# Includes the prose ALL-CAPS the bare-code rule now reaches (BINDING, MUST, XSD …) —
# these are emphasis and jargon, not identity.
_ARTIFACT_STOP = {"simpletype", "complextype", "enumeration", "xs:enumeration",
                  "code_decision", "codedecision", "todo", "...",
                  # ALL-CAPS prose / emphasis the agent and prompts use constantly
                  "binding", "must", "not", "never", "always", "and", "or", "the", "a",
                  "new", "old", "todo:", "note", "warn", "error", "ok", "yes", "no",
                  "if", "then", "else", "all", "any", "one", "two", "use", "add",
                  "xsd", "xjb", "xml", "json", "java", "sql", "api", "url", "uri",
                  "id", "ids", "utc", "eol", "crlf", "lf", "utf", "http", "https",
                  "get", "post", "put", "patch", "phase", "code", "codes", "value",
                  "values", "file", "files", "line", "lines", "refused", "staged",
                  "locked", "open", "still", "fix", "fixed", "why", "what", "how"}


def salient_artifacts(*texts: str) -> set[str]:
    """The set of code artifacts these texts name, lowercased.

    This is the deterministic half of "are these the same question?". An LLM rewords
    its prose freely but keeps citing the same files, constants and literals, because
    those are what it is actually stuck on.
    """
    out: set[str] = set()
    for t in texts:
        for m in _ARTIFACT.findall(t or ""):
            tok = m.strip('"').lower()
            if tok and tok not in _ARTIFACT_STOP:
                out.add(tok)
    return out


def artifact_containment(a: set[str], b: set[str]) -> tuple[float, list[str]]:
    """``(containment, sorted shared artifacts)`` for two artifact sets.

    Containment (over the SMALLER set) rather than Jaccard: a re-ask that surfaces
    newly-discovered call sites is still the same question, and Jaccard would punish
    it for knowing more. Returns 0.0 when the shared evidence is too thin to mean
    anything — see :data:`ARTIFACT_MIN_SHARED`.
    """
    shared = sorted(a & b)
    if len(shared) < ARTIFACT_MIN_SHARED:
        return 0.0, shared
    return len(shared) / max(min(len(a), len(b)), 1), shared


def normalize_anchor(text: str) -> str:
    """Lowercase / punctuation-free / whitespace-collapsed form of an anchor string.

    Quoting style, trailing periods and capitalisation must not create a new question
    identity: `'txnPurpose="BG"'` and `txnPurpose = 'BG'` are the same blocked item.
    """
    return _NON_KEY.sub("-", _WS.sub(" ", (text or "").strip().lower())).strip("-")


def stable_question_key(prefix: str, anchor: str) -> str:
    """Deterministic `question_key` for a question anchored on `anchor`.

    Readable slug + short digest of the FULL normalized anchor. The slug keeps the
    key legible in the DB and in logs; the digest keeps two anchors that share a
    128-char prefix from colliding into one chain.
    """
    norm = normalize_anchor(anchor) or "unanchored"
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]
    room = _KEY_MAX - len(prefix) - len(digest) - 2   # ':' separators
    return f"{prefix}:{norm[:max(room, 0)].strip('-')}:{digest}"


def _is_degenerate(vec) -> bool:
    """True when a vector carries no signal and must not be compared.

    `embed_query` is FAIL-SOFT: on a gateway error it returns a 768-dim zero vector
    rather than raising. Cosine against a zero vector is 0.0 for everything, so an
    outage would silently downgrade every comparison to 'not similar' — which is the
    safe direction, but we detect it explicitly so the caller can fall back to the
    deterministic key path instead of trusting a meaningless score.
    """
    if not vec:
        return True
    return math.fsum(abs(float(x)) for x in vec) == 0.0


def _cosine(a, b) -> float:
    """Cosine similarity; 0.0 on any structural mismatch (length, zero norm, non-numeric)."""
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = na = nb = 0.0
        for x, y in zip(a, b):
            xf, yf = float(x), float(y)
            dot += xf * yf
            na += xf * xf
            nb += yf * yf
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / (math.sqrt(na) * math.sqrt(nb))
    except Exception:  # noqa: BLE001 — similarity is advisory; never fail a decision on it
        return 0.0


def _entry_text(e: DecisionLedgerEntry) -> tuple[str, str]:
    """``(question, anchor)`` for an entry. The anchor is recovered from
    ``decided_against['blocked_item']`` where the code-decision path stores it."""
    da = e.decided_against if isinstance(e.decided_against, dict) else {}
    return (e.question or "").strip(), str(da.get("blocked_item") or "")


def _embed_scores(question: str, cands: list[DecisionLedgerEntry]) -> dict[str, float]:
    """``{entry_id: cosine}`` for the candidates, or ``{}`` when embeddings are unusable.

    Never raises and never returns a fabricated score: a degenerate (all-zero) vector
    means the gateway failed, and scoring 0.0 against it would masquerade as a
    confident "these are different". We return nothing instead, and the caller falls
    back to the deterministic artifact signal.
    """
    try:
        from app.rag.embeddings import embed_query
        qv = embed_query(question)
        if _is_degenerate(qv):
            logger.warning("decision-ledger: query embedding is degenerate (embedder likely "
                           "down) — using deterministic artifact overlap only")
            return {}
        out: dict[str, float] = {}
        for e in cands:
            ev = embed_query((e.question or "").strip())
            if not _is_degenerate(ev):
                out[e.id] = _cosine(qv, ev)
        return out
    except Exception as exc:  # noqa: BLE001 — advisory only
        logger.warning("decision-ledger embedding similarity failed (skipped): %s", exc)
        return {}


def find_similar_active(
    db: Session,
    change_request_id: str,
    question: str,
    *,
    anchor: str = "",
    kind: Optional[str] = None,
    exclude_key: Optional[str] = None,
) -> list[dict]:
    """Active entries that look like the same question, strongest first.

    Each result is ``{entry, score, verdict, semantic, artifact, shared}`` where
    ``verdict`` is ``'supersede'`` / ``'related'``. An entry qualifies if EITHER
    signal clears its bar — embeddings catch a full reword that cites nothing
    concrete, artifact overlap catches the far more common case of the same
    collision described differently — and ``score`` is the stronger of the two so
    a caller can rank without knowing which signal fired.
    """
    q = (question or "").strip()
    if not q and not anchor:
        return []
    cands = [
        e for e in active_entries(db, change_request_id)
        if (kind is None or e.kind == kind)
        and (exclude_key is None or e.question_key != exclude_key)
        and ((e.question or "").strip() or _entry_text(e)[1])
    ]
    if not cands:
        return []

    mine = salient_artifacts(q, anchor)
    sem = _embed_scores(q, cands) if q else {}

    out: list[dict] = []
    for e in cands:
        eq, ea = _entry_text(e)
        art, shared = artifact_containment(mine, salient_artifacts(eq, ea))
        s = sem.get(e.id, 0.0)
        if s >= SIMILARITY_SUPERSEDE or art >= ARTIFACT_SUPERSEDE:
            verdict = "supersede"
        elif s >= SIMILARITY_RELATED or art >= ARTIFACT_RELATED:
            verdict = "related"
        else:
            continue
        out.append({"entry": e, "score": round(max(s, art), 4), "verdict": verdict,
                    "semantic": round(s, 4), "artifact": round(art, 4), "shared": shared})
    # Supersede-grade matches first, then by strength.
    out.sort(key=lambda d: (d["verdict"] == "supersede", d["score"]), reverse=True)
    return out


def resolve_question_key(
    db: Session,
    change_request_id: str,
    *,
    prefix: str,
    anchor: str,
    question: Optional[str] = None,
    kind: Optional[str] = None,
    use_similarity: bool = True,
) -> tuple[str, dict]:
    """The `question_key` this question belongs to, plus how it was decided.

    Resolution order — deterministic first, similarity only as a fallback:

    * **exact** — the anchor-derived key already has a chain. Reuse it; the new answer
      supersedes the old one. No embedding call is made.
    * **similar** — no chain for this anchor, but an active entry clears a supersede
      bar on artifact overlap or embedding cosine. Same question wearing different
      words: adopt that entry's key so the chain stays single-headed.
    * **new** — everything else. Weaker matches are still reported in
      ``meta['related']`` so a human sees "you already answered something close to
      this" instead of the platform silently guessing.

    Returns ``(key, meta)`` with
    ``meta = {match, score, signal, related, anchor_key}``.
    """
    anchor_key = stable_question_key(prefix, anchor)
    meta: dict = {"match": "new", "score": 0.0, "signal": None,
                  "related": [], "anchor_key": anchor_key}

    exists = (
        db.query(DecisionLedgerEntry.id)
        .filter(DecisionLedgerEntry.change_request_id == change_request_id,
                DecisionLedgerEntry.question_key == anchor_key)
        .first()
    )
    if exists:
        meta["match"] = "exact"
        meta["score"] = 1.0
        meta["signal"] = "anchor"
        return anchor_key, meta

    if not use_similarity or not ((question or "").strip() or (anchor or "").strip()):
        return anchor_key, meta

    matches = find_similar_active(db, change_request_id, question or "", anchor=anchor,
                                  kind=kind, exclude_key=anchor_key)
    for m in matches:
        e = m["entry"]
        meta["related"].append({
            "question_key": e.question_key, "score": m["score"], "verdict": m["verdict"],
            "semantic": m["semantic"], "artifact": m["artifact"], "shared": m["shared"][:12],
            "question": (e.question or "")[:200], "chosen": (e.chosen or "")[:200],
        })
    if matches:
        best, bm = matches[0], meta["related"][0]
        meta["score"] = bm["score"]
        meta["signal"] = ("semantic" if best["semantic"] >= best["artifact"] else "artifact")
        if best["verdict"] == "supersede":
            meta["match"] = "similar"
            return str(best["entry"].question_key), meta
    return anchor_key, meta


def chain_entries(db: Session, change_request_id: str, question_key: str) -> list[DecisionLedgerEntry]:
    """Every answer ever recorded under `question_key`, oldest first."""
    return (
        db.query(DecisionLedgerEntry)
        .filter(DecisionLedgerEntry.change_request_id == change_request_id,
                DecisionLedgerEntry.question_key == question_key)
        .order_by(DecisionLedgerEntry.created_at.asc())
        .all()
    )


def repeat_state(db: Session, change_request_id: str, question_key: str) -> dict:
    """How many times this question has already been answered, and whether asking a
    human again is still useful.

    ``{'count': int, 'is_defect': bool, 'prior': [{chosen, directive, decided_at}]}``

    ``is_defect`` means the human has answered this same question
    :data:`REPEAT_DEFECT_THRESHOLD`+ times and the agent is asking anyway. At that
    point the bottleneck is not a missing decision — the agent is structurally unable
    to act on the one it keeps receiving — and re-prompting the human just extends the
    loop. Callers should surface the prior answers and name the loop instead.
    """
    prior = chain_entries(db, change_request_id, question_key)
    return {
        "count": len(prior),
        "is_defect": len(prior) >= REPEAT_DEFECT_THRESHOLD,
        "prior": [
            {"chosen": (e.chosen or "")[:300],
             "directive": (e.directive or "")[:300],
             "decided_at": e.decided_at.isoformat() if e.decided_at else None}
            for e in prior
        ],
    }


def append_entry(
    db: Session,
    change_request_id: str,
    *,
    question_key: str,
    kind: str,
    question: Optional[str] = None,
    options: Optional[list] = None,
    chosen: Optional[str] = None,
    evidence: Optional[list] = None,
    directive: Optional[str] = None,
    decided_by: Optional[str] = None,
    decided_against: Optional[dict] = None,
    supersede: bool = True,
) -> DecisionLedgerEntry:
    """Append a ledger entry. If `supersede`, the current active entry for the
    same question_key is marked as the predecessor (supersedes_id)."""
    supersedes_id = None
    if supersede:
        existing = (
            db.query(DecisionLedgerEntry)
            .filter(
                DecisionLedgerEntry.change_request_id == change_request_id,
                DecisionLedgerEntry.question_key == question_key,
            )
            .order_by(DecisionLedgerEntry.created_at.asc())
            .all()
        )
        # Supersede the current TIP (an entry nobody else already supersedes), not
        # merely the newest by created_at — picking a tip is what keeps each
        # question's chain single-headed even under equal timestamps / branches.
        superseded = {e.supersedes_id for e in existing if e.supersedes_id}
        tips = [e for e in existing if e.id not in superseded]
        if tips:
            supersedes_id = tips[-1].id  # newest tip (existing is created_at asc)

    entry = DecisionLedgerEntry(
        change_request_id=change_request_id,
        question_key=question_key,
        kind=kind,
        question=question,
        options=options,
        chosen=chosen,
        evidence=evidence,
        directive=directive,
        decided_by=decided_by,
        decided_at=utcnow(),
        decided_against=decided_against,
        supersedes_id=supersedes_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def active_entries(db: Session, change_request_id: str) -> list[DecisionLedgerEntry]:
    """Tip of every supersession chain — entries not superseded by any later one."""
    all_entries = (
        db.query(DecisionLedgerEntry)
        .filter(DecisionLedgerEntry.change_request_id == change_request_id)
        .order_by(DecisionLedgerEntry.created_at.asc())
        .all()
    )
    superseded_ids = {e.supersedes_id for e in all_entries if e.supersedes_id}
    return [e for e in all_entries if e.id not in superseded_ids]


def build_decisions_block(change_request_id: str, db: Session) -> str:
    """Render the active decisions as a binding prompt block. '' when empty.

    Clarification entries carry a system-generated provenance tag: an answer the PM
    typed and an agent-suggested option the PM clicked are epistemically different
    (the latter can be a ratified guess), and downstream agents must see which one
    they are bound by — plus the platform's occupancy verdict when the option
    proposed a concrete value. The tag is system-rendered (never PM prose), so the
    prompt trust boundary is unchanged."""
    entries = active_entries(db, change_request_id)
    if not entries:
        return ""
    lines = [
        "DECISIONS (BINDING — human-ratified; do NOT contradict, re-derive, or "
        "reopen these; superseded decisions are already excluded):",
    ]
    for e in entries:
        label = (e.question or e.question_key or "decision").strip()
        body = (e.directive or e.chosen or "").strip()
        if not body:
            continue
        lines.append(f"- [{e.kind}] {label} → {body}{_provenance_tag(e)}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _provenance_tag(e: DecisionLedgerEntry) -> str:
    """Short parenthetical on how a clarification answer originated. '' when unknown
    (pre-provenance entries) or not a clarification."""
    da = e.decided_against if isinstance(e.decided_against, dict) else {}
    if e.kind != "clarification" or da.get("origin") not in ("llm_option", "human_typed"):
        return ""
    if da.get("origin") == "human_typed":
        return " (PM-provided answer)"
    occ = da.get("occupancy") or {}
    if occ.get("hits"):
        return (f" (PM selected an agent-suggested option; its value '{occ.get('value')}' "
                f"already appears at {occ['hits']} code location(s) — re-verify against the "
                "code before treating it as available)")
    if occ and not occ.get("complete", True):
        return (f" (PM selected an agent-suggested option; the platform could not fully "
                f"scan value '{occ.get('value')}' — availability unverified)")
    return " (PM selected an agent-suggested option)"
