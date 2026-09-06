# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Load + format the latest clarification row as a prompt block.

Called by BRD / Tech Spec / Product Kit WS handlers right before streaming.
Returns a string that agents can splice into the final user message as the
most authoritative context block.
"""
import logging

from sqlalchemy.orm import Session

from app.models.clarification import Clarification

logger = logging.getLogger(__name__)


def load_answers_block(change_id: str, db: Session) -> str:
    """Return the latest Clarification answers formatted for prompt injection.

    If there is no clarification row (or no answers), returns an empty string.
    """
    row = (
        db.query(Clarification)
        .filter(Clarification.change_request_id == change_id)
        .order_by(Clarification.version.desc())
        .first()
    )
    if row is None:
        return ""
    if row.status not in ("answered", "skipped"):
        # Don't leak partial answers — they might be inconsistent
        return ""
    if not row.answers:
        return ""

    # Build a q/a listing using the question text so the LLM sees context
    id_to_q = {q["id"]: q for q in (row.questions or [])}
    lines: list[str] = []
    for qid, answer in (row.answers or {}).items():
        if not answer or not str(answer).strip():
            continue
        q = id_to_q.get(qid, {})
        qtext = q.get("text") or q.get("gap_key", "(question)")
        lines.append(f"Q: {qtext}")
        lines.append(f"A: {str(answer).strip()}")
        lines.append("")

    # Also include assumed gaps so the LLM knows what defaults we took
    if row.assumed_gaps:
        lines.append("## Assumed (not asked; using defaults):")
        for g in row.assumed_gaps[:8]:
            lines.append(f"- {g.get('key')}: {g.get('default')} ({g.get('reason','')})")

    if not lines:
        return ""

    return "\n".join(lines).strip()


def has_answers(change_id: str, db: Session) -> bool:
    row = (
        db.query(Clarification)
        .filter(Clarification.change_request_id == change_id)
        .order_by(Clarification.version.desc())
        .first()
    )
    return bool(row and row.status == "answered" and (row.answers or {}))


# ── v1 structured signal extraction ──────────────────────────────────────────

# Gap-key derivations — ONE helper pair shared by the question producer
# (question_generator.build_scope_signal_questions / _expand_signal_gap) and
# the ledger reader below. The two sides must agree byte-for-byte or answered
# questions silently stop being readable, so neither may restate the shape.

def op_scope_gap_key(op_key: str) -> str:
    return f"op_{op_key}_in_scope"


def party_scope_gap_key(party_key: str) -> str:
    return f"party_{party_key.lower()}_in_scope"


def _party_gap_to_key() -> dict[str, str]:
    """gap key -> canonical party key, from the active pack's cert vocabulary
    (legacy v2 per-party yes/no entries; v3 uses one multi_select whose option
    ids ARE the canonical keys). A pack that scopes no parties reads none."""
    from app.core.domain.contract import cert_vocabulary_of
    from app.core.domain.registry import get_active_pack

    return {party_scope_gap_key(k): k
            for k, _lbl in cert_vocabulary_of(get_active_pack()).parties()}


def _operation_gap_to_key() -> dict[str, str]:
    """gap key -> canonical operation key, from the active pack's
    change_operations. A pack that declares no operations reads none."""
    from app.core.domain.contract import change_operations_of
    from app.core.domain.registry import get_active_pack

    return {op_scope_gap_key(o.key): o.key
            for o in change_operations_of(get_active_pack())}

# Default ScopeSignals when a change predates v1 signals — safe fall-through
# values that let the engine work as before (planner scope enforcement is
# skipped when parties/operations are None).
_DEFAULT_SCOPE_SIGNALS: dict = {
    "parties_in_scope":       None,
    "feature_operations":     None,
    "risk_profile":           "standard",
    "compliance_sensitivity": "standard",
    "scope_error_codes":      {},
}

# PM scope signals are captured through the AGENTIC clarification stage
# (AnalysisPanel → decide-clarifications → decision ledger), not the legacy REST
# Clarification table. Signal questions are injected with a stable
# question id of `scope_signal::<gap_key>`, which becomes the ledger
# `question_key`. The producer is `question_generator.build_scope_signal_questions`.
SCOPE_SIGNAL_QK_PREFIX = "scope_signal::"


def _recover_parties_multi_select(entry) -> list[str]:
    """Recover the canonical PARTY key list from a v3 multi_select
    `parties_in_scope` ledger entry.

    `chosen` is a JSON list of option labels (see `agentic.py::_resolve`
    for a `multi_select` question). Reverse-lookup each label to its
    option `id` (which the question builder set to the canonical PARTY
    key like `PAYER_PSP`). Malformed JSON → empty list (safe default).
    """
    import json as _json
    chosen = (getattr(entry, "chosen", None) or "").strip()
    if not chosen:
        return []
    try:
        labels = _json.loads(chosen)
    except Exception:  # noqa: BLE001 — corrupt row → treat as no parties
        logger.warning(
            "parties_in_scope: chosen is not JSON — recovered as empty. raw=%r",
            chosen[:120],
        )
        return []
    if not isinstance(labels, list):
        return []
    label_to_id: dict[str, str] = {}
    for o in (getattr(entry, "options", None) or []):
        if not isinstance(o, dict):
            continue
        lbl = str(o.get("label") or "").strip()
        oid = str(o.get("id") or "").strip()
        if lbl and oid:
            label_to_id[lbl] = oid
    out: list[str] = []
    seen: set[str] = set()
    for lbl in labels:
        oid = label_to_id.get(str(lbl).strip())
        if oid and oid not in seen:
            out.append(oid)
            seen.add(oid)
    return out


def _recover_signal_value(entry) -> str | None:
    """Recover the canonical value (the chosen option's `id`) from a ledger entry.

    `decide_clarifications` stores `chosen` as the option LABEL and `options` as
    the full option list. Signal-question options set `id` to the canonical value
    ("yes"/"no", "low"/"high", "RBI-mandated", …), so we map the chosen label
    back to its option id. Case is preserved — compliance values are mixed-case.
    """
    chosen = (getattr(entry, "chosen", None) or "").strip()
    if not chosen:
        return None
    for o in (getattr(entry, "options", None) or []):
        if isinstance(o, dict) and str(o.get("label") or "").strip() == chosen:
            return (str(o.get("id") or "").strip() or None)
    # No option matched (e.g. a free-text custom answer) — fall back to the raw
    # chosen string so callers can still parse it.
    return chosen or None


def get_scope_signals(change_id: str, db: Session) -> dict:
    """Bundle the PM scope signals into a typed dict, read from the decision ledger.

    Signals are captured in the agentic clarification stage: injected signal
    questions land in the decision ledger keyed by `scope_signal::<gap_key>`.
    Returns _DEFAULT_SCOPE_SIGNALS when no signal entries exist (a legacy change,
    or a run whose clarification gate never asked them) — backward compatible.

    Shape:
        {
            "parties_in_scope":       list[str] | None,  # canonical PARTY keys, None when unasked
            "feature_operations":     list[str] | None,  # canonical op keys, None when unasked
            "risk_profile":           str,               # low | standard | high
            "compliance_sensitivity": str,               # standard | RBI-mandated | PMLA-touched
            "scope_error_codes":      dict[str, list[str]],  # {"debit_failure": ["ZM"], ...}
        }
    """
    from app.services.decision_ledger import active_entries

    # gap_key → recovered value (single-select / yes-no), from the active tip
    # of each signal decision.
    signals_by_gap: dict[str, str] = {}
    # gap_key → raw entry (needed for the v3 multi_select parties_in_scope
    # branch, where `chosen` is a JSON list of labels and we need `options`
    # for reverse-lookup).
    entries_by_gap: dict[str, object] = {}
    try:
        for e in active_entries(db, change_id):
            qk = getattr(e, "question_key", None) or ""
            if not qk.startswith(SCOPE_SIGNAL_QK_PREFIX):
                continue
            gap = qk[len(SCOPE_SIGNAL_QK_PREFIX):]
            entries_by_gap[gap] = e
            val = _recover_signal_value(e)
            if val is not None:
                signals_by_gap[gap] = val
    except Exception:  # noqa: BLE001 — ledger read must never fail the engine
        logger.exception("get_scope_signals: ledger read failed change=%s", change_id)
        return dict(_DEFAULT_SCOPE_SIGNALS)

    if not signals_by_gap and not entries_by_gap:
        return dict(_DEFAULT_SCOPE_SIGNALS)

    # Parties — three recovery paths in order of preference:
    #   v3 (multi_select, current):  gap_key = "parties_in_scope", `chosen` is a
    #     JSON list of labels. Reverse-lookup each label to its option `id`
    #     (canonical PARTY key) via the entry's `options`.
    #   v2 (4× yes_no, legacy):      gap_keys `party_*_in_scope`, `chosen` per
    #     entry is a "Yes …" / "No …" label. Include a party iff its entry's
    #     recovered id is "yes".
    #   None:                        no party questions were ever asked
    #     (very old change) — return None so downstream scope enforcement
    #     short-circuits.
    parties_in_scope: list[str] | None = None
    if "parties_in_scope" in entries_by_gap:
        parties_in_scope = _recover_parties_multi_select(
            entries_by_gap["parties_in_scope"]
        )
    else:
        party_gap_to_key = _party_gap_to_key()
        party_gaps = {g: v for g, v in signals_by_gap.items() if g in party_gap_to_key}
        if party_gaps:
            parties_in_scope = [
                party_gap_to_key[g] for g, v in party_gaps.items() if v == "yes"
            ]

    operation_gap_to_key = _operation_gap_to_key()
    op_gaps = {g: v for g, v in signals_by_gap.items() if g in operation_gap_to_key}
    feature_operations = (
        [operation_gap_to_key[g] for g, v in op_gaps.items() if v == "yes"]
        if op_gaps else None
    )

    risk = signals_by_gap.get("risk_profile") or "standard"
    compliance = signals_by_gap.get("compliance_sensitivity") or "standard"

    # scope_error_codes — free-text JSON captured out-of-band (dedicated widget
    # is a later PR). Parse defensively; absent/malformed → {} ("any category-
    # matching code allowed").
    scope_error_codes: dict[str, list[str]] = {}
    raw_codes = signals_by_gap.get("scope_error_codes") or ""
    if raw_codes:
        try:
            import json as _json
            parsed = _json.loads(raw_codes)
            if isinstance(parsed, dict):
                for key, val in parsed.items():
                    if isinstance(val, list):
                        scope_error_codes[str(key)] = [str(c) for c in val]
        except Exception as e:  # noqa: BLE001 — malformed JSON → fail-soft
            logger.warning(
                "get_scope_signals: scope_error_codes malformed for change=%s (%s)",
                change_id, e,
            )

    return {
        "parties_in_scope":       parties_in_scope,
        "feature_operations":     feature_operations,
        "risk_profile":           risk,
        "compliance_sensitivity": compliance,
        "scope_error_codes":      scope_error_codes,
    }
