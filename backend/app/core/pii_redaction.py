# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Best-effort PII redaction for content flowing into LLM prompts.

Closes THREAT_MODEL.md T6 ("No PII redaction before sending content to
external LLM providers") and security_architecture_skills.md §10.2
("PII MUST be... minimized in downstream flows"). See
docs/PII_DATA_CLASSIFICATION.md §3 for the full closure design (Tier 1
vs Tier 2).

This is a heuristic, defense-in-depth filter — NOT a substitute for
classifying which `A2ATaskType` payloads are PII-CERTAIN vs
PII-POSSIBLE (Tier 2, tracked as a follow-up in
docs/PII_DATA_CLASSIFICATION.md). Ship this first because it requires no
schema change and reduces risk immediately for every field sourced from
`a2a_messages.payload`, `tech_specs.content`, `brds.content`, or
`negotiation_messages` before it enters an LLM prompt.

Deliberately conservative (biased toward over-redaction): a false
positive here means a harmless numeric reference gets replaced with a
placeholder in the LLM's view of the document, which costs a small
amount of prompt fidelity. A false negative means a real mobile number
or account reference reaches an external LLM provider unredacted, which
is the actual risk this module exists to reduce. The trade-off is
deliberately made in favor of over-redaction.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Conservative patterns — tuned to avoid false-negatives (better to
# over-redact a false positive like a random 10-digit reference number
# than under-redact a real mobile number).
_MOBILE_RE = re.compile(r"\b(?:\+?91[-\s]?)?[6-9]\d{9}\b")
_VPA_RE = re.compile(r"\b[\w.\-]{2,256}@[a-zA-Z][\w.\-]{1,64}\b")  # name@bank-handle
_ACCOUNT_RE = re.compile(r"\b\d{9,18}\b")  # bank account number range (conservative, high false-positive rate by design)
_MPIN_HINT_RE = re.compile(r"\b(?:mpin|m-pin)\D{0,10}\d{4,6}\b", re.IGNORECASE)

_REPLACEMENT = "[REDACTED-PII-{kind}]"

# ── Context-keyed numeric patterns (PROFILE_DOC) ─────────────────────────────
#
# The bare `_ACCOUNT_RE` above matches ANY 9–18 digit run. That is correct for
# partner-authored free text (a naked digit string there is far more likely to
# be an account/transaction reference than a constant), but it is actively
# WRONG for specification documents: a TSD/BRD legitimately contains timeouts
# (`30000`), epoch-millis timestamps (`1735689600000`), byte budgets, port
# numbers, and the Authority response/error codes. Redacting those corrupts the very
# contract the code-generation agents are required to implement against —
# turning a PII control into a correctness bug.
#
# Measured against realistic spec content, the bare pattern matched
# `1234567891` (a response code), `1735689600000` (an epoch timestamp) and
# `999999999` (a byte budget) — all false positives, none of them PII.
#
# So for documents we require the digit run to be ADJACENT TO A PII-INDICATING
# LABEL ("account no", "a/c", "mobile", "customer id", ...). This trades some
# recall for not breaking codegen — and the recall loss is bounded, because the
# high-signal patterns (mobile numbers, VPA handles, MPIN references) still run
# unconditionally under every profile.
_LABELLED_ACCOUNT_RE = re.compile(
    r"(?i)\b(?:a/?c(?:count)?|acct|account)\s*(?:no\.?|number|#)?\s*[:=\-]?\s*\d{9,18}\b"
)
_LABELLED_CUSTOMER_ID_RE = re.compile(
    r"(?i)\b(?:cust(?:omer)?|crn|ucic)\s*(?:id|no\.?|number|#)?\s*[:=\-]?\s*[A-Z0-9]{6,20}\b"
)
_LABELLED_MOBILE_RE = re.compile(
    r"(?i)\b(?:mobile|msisdn|phone|contact)\s*(?:no\.?|number|#)?\s*[:=\-]?\s*(?:\+?91[-\s]?)?\d{10}\b"
)

# `PROFILE_FREETEXT` — for partner/human-authored prose (negotiation messages,
# A2A free-text payload fields). Biased toward over-redaction, as the module
# docstring describes: a false positive costs a little prompt fidelity, a false
# negative leaks real PII to an external provider.
PROFILE_FREETEXT = "freetext"

# `PROFILE_DOC` — for specification documents that drive code generation
# (BRD/TSD/assessment/plan sections). High-signal patterns only, plus
# label-anchored numeric patterns. Will NOT touch bare numeric literals, so
# timeouts/codes/timestamps in a spec survive intact.
PROFILE_DOC = "doc"

# Order matters within a profile: MPIN and mobile patterns are checked before
# the broad account-number pattern so a matched mobile number is not ALSO
# partially re-matched (and double-redacted, harmlessly but confusingly) by the
# account-number regex. Each pattern only consumes text not already replaced by
# an earlier pass, since `re.subn` operates on the progressively-redacted
# string.
_PROFILE_PATTERNS: dict[str, tuple[tuple[re.Pattern, str], ...]] = {
    PROFILE_FREETEXT: (
        (_MPIN_HINT_RE, "MPIN"),
        (_MOBILE_RE, "MOBILE"),
        (_VPA_RE, "VPA"),
        (_ACCOUNT_RE, "ACCOUNT"),
    ),
    PROFILE_DOC: (
        (_MPIN_HINT_RE, "MPIN"),
        (_LABELLED_MOBILE_RE, "MOBILE"),
        (_MOBILE_RE, "MOBILE"),
        (_LABELLED_ACCOUNT_RE, "ACCOUNT"),
        (_LABELLED_CUSTOMER_ID_RE, "CUSTOMER-ID"),
        # NOTE: `_VPA_RE` and the bare `_ACCOUNT_RE` are deliberately EXCLUDED
        # here. `_VPA_RE` matches any email-shaped token, which in a spec is
        # usually a contact address or an XML namespace fragment, not a
        # consumer's the network handle; the bare account pattern's false positives are
        # documented above. Both remain active under PROFILE_FREETEXT, where
        # the content genuinely is partner-authored prose.
    ),
}


def redact_for_llm_prompt(text: str, *, field_name: str = "",
                          correlation_id: str | None = None,
                          profile: str = PROFILE_FREETEXT) -> tuple[str, int]:
    """Returns (redacted_text, redaction_count).

    `profile` selects the pattern set — `PROFILE_FREETEXT` (default,
    aggressive, for partner/human-authored prose) or `PROFILE_DOC`
    (conservative, label-anchored, for specification documents that drive
    code generation). The default is unchanged from this function's
    original behaviour, so existing callers are unaffected.

    Callers should log a `SECURITY_EVENT event=pii_redacted_before_llm_call`
    telemetry line (severity=low, decision=redacted) when redaction_count > 0,
    per security_architecture_skills.md §13.2 — this function itself does
    NOT emit that telemetry (it has no correlation-id/run context by
    default), so the caller wires it into whatever telemetry chokepoint
    already exists for the call site (e.g. `core/observability.py`).

    Never logs the redacted VALUE — only whether/how many redactions
    occurred — avoiding the exact "large/sensitive payload logging"
    anti-pattern both skill files flag.
    """
    if not text:
        return text, 0
    patterns = _PROFILE_PATTERNS.get(profile)
    if patterns is None:
        raise ValueError(
            f"unknown redaction profile: {profile!r} "
            f"(expected one of {sorted(_PROFILE_PATTERNS)})"
        )
    count = 0
    result = text
    for pattern, kind in patterns:
        result, n = pattern.subn(_REPLACEMENT.format(kind=kind), result)
        count += n
    if count and field_name:
        logger.info(
            "PII_REDACTION field=%s profile=%s correlation_id=%s redaction_count=%d",
            field_name, profile, correlation_id or "-", count,
        )
    return result, count


def redact_fields_for_llm_prompt(fields: dict[str, str], *,
                                 correlation_id: str | None = None,
                                 profile: str = PROFILE_FREETEXT) -> dict[str, str]:
    """Convenience wrapper for redacting multiple named fields at once
    (e.g. a dict of {heading: body} sections pulled from a TSD/BRD before
    assembly into a prompt block). Returns a NEW dict — never mutates the
    input, so callers holding onto the original (unredacted) content for
    display purposes are unaffected."""
    out: dict[str, str] = {}
    for name, value in fields.items():
        redacted, _ = redact_for_llm_prompt(
            value, field_name=name, correlation_id=correlation_id, profile=profile)
        out[name] = redacted
    return out


def redact_a2a_payload_for_llm(payload, task_type, *,
                               correlation_id: str | None = None) -> tuple[object, int]:
    """Tier 2 enforcement — redact an A2A message payload before it reaches an
    external LLM, based on the message type's DESIGN-TIME PII classification.

    See `docs/PII_DATA_CLASSIFICATION.md` §3 Tier 2 and
    `a2a_common/protocol.py`'s `PII_CLASSIFICATION_RATIONALE`.

    Two properties distinguish this from Tier 1's heuristic filtering:

    1. **Mandatory, not optional.** For a task type classified
       `carries_pii=True`, redaction is applied REGARDLESS of
       `settings.pii_redaction_freetext_enabled`. That flag exists so an
       operator can tune the heuristic filter on content that only MIGHT carry
       PII; it is not a licence to ship a message type known by design to carry
       it. A message type whose whole purpose is to convey account/transaction
       detail (e.g. `cert_case_result`) must not become unfiltered because a
       general knob was turned off.
    2. **Fails closed on the unknown.** `protocol.carries_pii()` returns True
       for an unrecognised task type, so a protocol addition that nobody has
       classified is filtered until a human classifies it — the opposite of
       silently inheriting "no PII".

    Walks nested dicts/lists so a payload's free-text field is filtered wherever
    it sits in the structure (A2A payloads are commonly `{"payload": {...}}`
    with the real content one or two levels down). Non-string leaves — ints,
    bools, timestamps — are returned untouched: they are contract values, and
    the reasoning in `PROFILE_DOC`'s comment about not corrupting machine-
    readable content applies here too.

    Returns `(redacted_payload, redaction_count)`. The input is never mutated.
    """
    from app.a2a_common.protocol import carries_pii

    if payload is None:
        return payload, 0
    if not carries_pii(task_type):
        return payload, 0

    total = 0

    def _walk(node):
        nonlocal total
        if isinstance(node, str):
            redacted, n = redact_for_llm_prompt(node, profile=PROFILE_FREETEXT)
            total += n
            return redacted
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            walked = [_walk(v) for v in node]
            return type(node)(walked) if isinstance(node, tuple) else walked
        return node   # int/float/bool/None — contract values, left intact

    result = _walk(payload)
    if total:
        tt = getattr(task_type, "value", task_type)
        logger.info(
            "SECURITY_EVENT event=pii_redacted_before_llm_call severity=low "
            "task_type=%s classification=carries_pii correlation_id=%s "
            "redaction_count=%d decision=redacted",
            tt, correlation_id or "-", total,
        )
    return result, total


def redact_doc_sections(sections: dict[str, str], *,
                        doc_label: str = "",
                        correlation_id: str | None = None) -> tuple[dict[str, str], int]:
    """Redact a `{heading: body}` document map under `PROFILE_DOC`, returning
    `(redacted_sections, total_redaction_count)`.

    This is the shape `context_assembler.doc_sections()` produces for BRD /
    TSD / assessment / plan content, and the shape `agentic_subagents.py`
    renders into prompts. Returns a NEW dict; the caller's original map is
    never mutated (the UI/API read paths keep serving unredacted content —
    only what crosses the LLM boundary is filtered).
    """
    if not sections:
        return sections, 0
    out: dict[str, str] = {}
    total = 0
    for heading, body in sections.items():
        redacted, n = redact_for_llm_prompt(
            body,
            field_name=f"{doc_label}:{heading}" if doc_label else heading,
            correlation_id=correlation_id,
            profile=PROFILE_DOC,
        )
        out[heading] = redacted
        total += n
    return out, total
