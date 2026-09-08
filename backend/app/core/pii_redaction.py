# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Best-effort PII redaction for content flowing into LLM prompts.

Closes T6 ("No PII redaction before sending content to external LLM
providers") and implements the rule that "PII MUST be... minimized in
downstream flows". The closure has two tiers: this module is Tier 1.

This is a heuristic, defense-in-depth filter — NOT a substitute for
classifying which `A2ATaskType` payloads are PII-CERTAIN vs
PII-POSSIBLE (Tier 2). Ship this first because it requires no
schema change and reduces risk immediately for every field sourced from
`a2a_messages.payload`, `tech_specs.content`, `brds.content`, or
`negotiation_messages` before it enters an LLM prompt.

Deliberately conservative (biased toward over-redaction): a false
positive here means a harmless numeric reference gets replaced with a
placeholder in the LLM's view of the document, which costs a small
amount of prompt fidelity. A false negative means a real subscriber
number or account reference reaches an external LLM provider unredacted,
which is the actual risk this module exists to reduce. The trade-off is
deliberately made in favor of over-redaction.

BUILT-INS ARE COUNTRY-NEUTRAL; THE PACK EXTENDS THEM. The pattern set has
two layers: the built-ins below, which hold only what is true of every
deployment (E.164 numbers, addressing handles, account-shaped digit runs,
label-anchored references), and the active domain pack's `pii_patterns:`
block, which names the classes only that domain can — a national mobile
format, a scheme's customer reference, its own word for a secret.

The layering is one-way. A pack may ADD classes; it can never remove a
built-in, and a pack that is absent, unloadable, or declares an
uncompilable pattern loses only its own additions — the built-ins still
run. That is what "fail closed" means for a filter: the failure mode is
less prompt fidelity, never silently unredacted output.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── Built-in patterns: COUNTRY-NEUTRAL, always active ────────────────────────
#
# These used to encode one country's taxonomy — a +91 mobile format, `mpin`,
# `crn`/`ucic` customer references. That is not a cosmetic problem: a deployment
# outside that country got NO phone-number redaction at all from the "generic"
# filter, because a German or Brazilian number matches none of it. A privacy
# control that silently does nothing is worse than one that is absent, since
# nobody goes looking for it.
#
# So the built-ins now cover only what is true everywhere, and a domain adds its
# own classes through the pack's `pii_patterns:` block (see `_pack_patterns`).
# The built-ins ALWAYS run — a pack can extend this set, never shrink it.
#
# Conservative by design: better to over-redact a random reference number than
# to let a real subscriber number through to an external provider.

# E.164 international numbers: a leading `+`, 1–3 digit country code, then 6–14
# more digits with optional separators. The `+` is the one country-neutral
# marker a phone number carries, which is why the built-in requires it — a bare
# national-format run is indistinguishable from any other digit string without
# knowing the country, and that knowledge is exactly what the pack supplies.
_E164_RE = re.compile(r"(?<![\w+])\+\d{1,3}[-\s.]?(?:\d[-\s.]?){5,13}\d\b")
_HANDLE_RE = re.compile(r"\b[\w.\-]{2,256}@[a-zA-Z][\w.\-]{1,64}\b")  # name@addressing-handle
_ACCOUNT_RE = re.compile(r"\b\d{9,18}\b")  # account number range (conservative, high false-positive rate by design)
# Secret-entry references, in the spellings every domain shares. A domain whose
# secret has its own name (the network pack: `mpin`) declares that in
# `pii_patterns:` — `\bpin\b` deliberately does not match inside `MPIN`.
_SECRET_ENTRY_RE = re.compile(
    r"\b(?:pin|passcode|otp|one[-\s]?time[-\s]?p(?:assword|in))\D{0,10}\d{4,8}\b",
    re.IGNORECASE,
)

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
# high-signal patterns (phone numbers, addressing handles, secret-entry
# references, plus whatever the pack adds) still run unconditionally under
# every profile.
_LABELLED_ACCOUNT_RE = re.compile(
    r"(?i)\b(?:a/?c(?:count)?|acct|account)\s*(?:no\.?|number|#)?\s*[:=\-]?\s*\d{9,18}\b"
)
# `crn` / `ucic` were here as built-ins; they are one banking ecosystem's names
# for a customer reference, so they moved to the network pack's `pii_patterns:`.
# What stays is the spelling every domain uses.
_LABELLED_CUSTOMER_ID_RE = re.compile(
    r"(?i)\b(?:cust(?:omer)?)\s*(?:id|no\.?|number|#)?\s*[:=\-]?\s*[A-Z0-9]{6,20}\b"
)
# Label-anchored phone number, country-neutral: an optional `+` and country
# code, then 7–15 digits (the E.164 range). The label is what makes this safe
# to apply without knowing the national format.
_LABELLED_MOBILE_RE = re.compile(
    r"(?i)\b(?:mobile|msisdn|phone|contact)\s*(?:no\.?|number|#)?\s*[:=\-]?\s*"
    r"(?:\+\d{1,3}[-\s]?)?\d{7,15}\b"
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

# Order matters within a profile: secret-entry and phone-number patterns are
# checked before the broad account-number pattern so a matched phone number is
# not ALSO partially re-matched (and double-redacted, harmlessly but
# confusingly) by the account-number regex. Each pattern only consumes text not
# already replaced by an earlier pass, since `re.subn` operates on the
# progressively-redacted string. The pack's own patterns run ahead of all of
# these — see `_patterns_for`.
_BUILTIN_PROFILE_PATTERNS: dict[str, tuple[tuple[re.Pattern, str], ...]] = {
    PROFILE_FREETEXT: (
        (_SECRET_ENTRY_RE, "SECRET"),
        (_E164_RE, "MOBILE"),
        (_HANDLE_RE, "HANDLE"),
        (_ACCOUNT_RE, "ACCOUNT"),
    ),
    PROFILE_DOC: (
        (_SECRET_ENTRY_RE, "SECRET"),
        (_LABELLED_MOBILE_RE, "MOBILE"),
        (_E164_RE, "MOBILE"),
        (_LABELLED_ACCOUNT_RE, "ACCOUNT"),
        (_LABELLED_CUSTOMER_ID_RE, "CUSTOMER-ID"),
        # NOTE: `_HANDLE_RE` and the bare `_ACCOUNT_RE` are deliberately EXCLUDED
        # here. `_HANDLE_RE` matches any email-shaped token, which in a spec is
        # usually a contact address or an XML namespace fragment, not a
        # consumer's addressing handle; the bare account pattern's false positives are
        # documented above. Both remain active under PROFILE_FREETEXT, where
        # the content genuinely is partner-authored prose.
    ),
}

_PROFILES = tuple(_BUILTIN_PROFILE_PATTERNS)


def _pack_patterns() -> tuple[tuple[re.Pattern, str], ...]:
    """The active domain pack's own PII classes, or `()`.

    FAILS CLOSED, in the only sense that means anything for a redaction filter:
    a pack that is missing, unloadable, or full of nonsense costs you the
    domain-specific rules and NOTHING else — every built-in still runs. The one
    outcome ruled out is "a bad pack silently turned redaction off", so every
    failure path here returns the empty tuple and logs, and none of them raise
    into the caller's prompt-assembly path.

    Not cached: `DOMAIN_PACK` is read at call time and the registry does its own
    caching, so a mid-process pack swap (tests, and a future multi-tenant read)
    is picked up. The cost is a dict lookup against an already-compiled map.
    """
    try:
        from app.core.domain.contract import pii_patterns_of
        from app.core.domain.registry import get_active_pack

        return tuple((pattern, str(kind))
                     for kind, pattern in pii_patterns_of(get_active_pack()).items())
    except Exception:                                  # noqa: BLE001 — see above
        logger.warning(
            "PII_REDACTION could not read `pii_patterns` from the active domain "
            "pack; falling back to the built-in patterns only", exc_info=True)
        return ()


def _patterns_for(profile: str) -> tuple[tuple[re.Pattern, str], ...] | None:
    """Pack patterns first, then the built-ins. None for an unknown profile.

    Pack-first is deliberate and matches the old hardcoded ordering: a
    domain-specific rule (a national mobile format) has to get its match in
    before the broad `_ACCOUNT_RE` digit-run sweep claims the same characters
    under a vaguer label.
    """
    builtins = _BUILTIN_PROFILE_PATTERNS.get(profile)
    if builtins is None:
        return None
    return _pack_patterns() + builtins


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
    telemetry line (severity=low, decision=redacted) when redaction_count > 0.
    This function itself does
    NOT emit that telemetry (it has no correlation-id/run context by
    default), so the caller wires it into whatever telemetry chokepoint
    already exists for the call site (e.g. `core/observability.py`).

    Never logs the redacted VALUE — only whether/how many redactions
    occurred — avoiding the exact "large/sensitive payload logging"
    anti-pattern the reviews flag.
    """
    if not text:
        return text, 0
    patterns = _patterns_for(profile)
    if patterns is None:
        raise ValueError(
            f"unknown redaction profile: {profile!r} "
            f"(expected one of {sorted(_PROFILES)})"
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

    The per-task-type classification lives in `a2a_common/protocol.py`'s
    `PII_CLASSIFICATION_RATIONALE`.

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
