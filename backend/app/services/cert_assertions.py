# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Field-level assertion engine (CERT-2) — pure functions, three outcomes.

One function per assertion kind, `(expected, observed) -> AssertionOutcome`.
Stdlib only — `re` plus the codec Protocol for payload reads. No DB, no LLM,
no model imports: `evaluate_specs` is duck-typed over spec rows (attribute
access only), so its tests need plain namespaces and strings.

THREE OUTCOMES, NOT TWO. A constraint that cannot be checked SKIPs WITH A
REASON — it never silently passes. The first pass proved why with a mutation
run: redefining SKIP as a pass kept 70 of 72 tests green. The outcomes are
pinned as distinct values by tests; keep them that way.

What SKIPs, and why (each is a true statement, not a shrug):

* datatype `Alphanumeric` — the ingest catch-all label; it names no character
  set, so there is nothing to check.
* datatype `Code` — the enum assertion governs those values.
* datatype `Fixed value` — the sample defines it; no independent rule.
* an optional field (`mandatory="N"`), and any value-assertion on an absent
  field — presence is occurrence/mandatory's question, not datatype's.
* an uncompilable `pattern_rule` — a REGISTRY defect; failing the partner for
  our bad regex blames the wrong party.
* `mandatory="C"` — conditionally mandatory is SURFACED, not evaluated: the
  outcome quotes `condition_text` (prose for humans) rather than guessing at
  its truth.

THE `length_rule` TRAP: one registry column carries two different constraint
kinds. `Min/Max Length` bounds the STRING; `minInclusive/maxInclusive` bounds
the NUMBER. They are parsed apart — treating `maxInclusive 100000` as a length
would fail every amount over five digits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.core.wire.codec import CodecError, WireCodec

__all__ = [
    "PASS", "FAIL", "SKIP", "AssertionOutcome", "SpecOutcome",
    "assert_occurrence", "assert_datatype", "assert_length", "assert_mandatory",
    "assert_enum", "assert_pattern", "assert_response_code",
    "evaluate_specs", "assertion_failures",
]

# Distinct values, pinned by tests. SKIP must never compare equal to PASS.
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass(frozen=True)
class AssertionOutcome:
    status: str
    reason: str | None = None    # SKIP always carries one; FAIL says what broke


def _skip(reason: str) -> AssertionOutcome:
    return AssertionOutcome(SKIP, reason)


def _fail(reason: str) -> AssertionOutcome:
    return AssertionOutcome(FAIL, reason)


_OK = AssertionOutcome(PASS)


# ── occurrence ───────────────────────────────────────────────────────────────

_OCCURRENCE = re.compile(r"^\s*(\d+)\s*\.\.\s*(\d+|n|\*)\s*$", re.IGNORECASE)


def assert_occurrence(expected: Mapping[str, Any], count: int) -> AssertionOutcome:
    rule = str(expected.get("occurrence") or "")
    m = _OCCURRENCE.match(rule)
    if not m:
        return _skip(f"occurrence rule {rule!r} is not parseable (registry defect)")
    lo = int(m.group(1))
    hi_raw = m.group(2).lower()
    hi = None if hi_raw in ("n", "*") else int(hi_raw)
    if count < lo:
        return _fail(f"occurs {count}x, minimum is {lo}")
    if hi is not None and count > hi:
        return _fail(f"occurs {count}x, maximum is {hi}")
    return _OK


# ── datatype ─────────────────────────────────────────────────────────────────

# Catch-all registry labels that name no checkable rule. Lowercased keys.
_UNCHECKABLE_DATATYPES = {
    "alphanumeric": "datatype 'Alphanumeric' names no character set",
    "code": "datatype 'Code' is governed by the enum assertion",
    "fixed value": "datatype 'Fixed value' is defined by the sample, not a rule",
}
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def assert_datatype(expected: Mapping[str, Any],
                    values: Sequence[str]) -> AssertionOutcome:
    label = str(expected.get("datatype") or "").strip()
    reason = _UNCHECKABLE_DATATYPES.get(label.lower())
    if reason:
        return _skip(reason)
    if not values:
        return _skip("field absent — presence is occurrence/mandatory's question")
    if label.lower() in ("numeric", "number", "decimal"):
        for v in values:
            if not _NUMERIC.match(v):
                return _fail(f"value {v!r} is not {label}")
        return _OK
    return _skip(f"datatype {label!r} has no checkable rule")


# ── length (the two-kinds-in-one-column trap) ────────────────────────────────

_MIN_LEN = re.compile(r"min\.?\s*length\s*[:=]?\s*(\d+)", re.IGNORECASE)
_MAX_LEN = re.compile(r"max\.?\s*length\s*[:=]?\s*(\d+)", re.IGNORECASE)
_EXACT_LEN = re.compile(r"^\s*length\s*[:=]?\s*(\d+)\s*$", re.IGNORECASE)
_MIN_INC = re.compile(r"min\s*inclusive\s*[:=]?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_MAX_INC = re.compile(r"max\s*inclusive\s*[:=]?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def assert_length(expected: Mapping[str, Any],
                  values: Sequence[str]) -> AssertionOutcome:
    rule = str(expected.get("length_rule") or "")
    min_len = _MIN_LEN.search(rule)
    max_len = _MAX_LEN.search(rule)
    exact = _EXACT_LEN.match(rule) if not (min_len or max_len) else None
    min_inc = _MIN_INC.search(rule)
    max_inc = _MAX_INC.search(rule)
    if not any((min_len, max_len, exact, min_inc, max_inc)):
        return _skip(f"length rule {rule!r} is not parseable (registry defect)")
    if not values:
        return _skip("field absent — presence is occurrence/mandatory's question")

    for v in values:
        if min_len and len(v) < int(min_len.group(1)):
            return _fail(f"length {len(v)} under Min Length {min_len.group(1)}")
        if max_len and len(v) > int(max_len.group(1)):
            return _fail(f"length {len(v)} over Max Length {max_len.group(1)}")
        if exact and len(v) != int(exact.group(1)):
            return _fail(f"length {len(v)} != required Length {exact.group(1)}")
        if min_inc or max_inc:
            if not _NUMERIC.match(v):
                return _fail(f"value {v!r} is not numeric but the rule bounds a number")
            number = float(v)
            if min_inc and number < float(min_inc.group(1)):
                return _fail(f"value {v} under minInclusive {min_inc.group(1)}")
            if max_inc and number > float(max_inc.group(1)):
                return _fail(f"value {v} over maxInclusive {max_inc.group(1)}")
    return _OK


# ── mandatory ────────────────────────────────────────────────────────────────

def assert_mandatory(expected: Mapping[str, Any], count: int) -> AssertionOutcome:
    flag = str(expected.get("mandatory") or "").strip().upper()
    if flag == "Y":
        return _OK if count >= 1 else _fail("mandatory field is absent")
    if flag == "N":
        return _skip("optional field — nothing to assert")
    if flag == "C":
        condition = str(expected.get("condition_text") or "").strip() \
            or "(no condition text recorded)"
        return _skip(f"conditionally mandatory — surfaced, not evaluated: {condition}")
    return _skip(f"mandatory flag {flag!r} is not a known value (registry defect)")


# ── enum ─────────────────────────────────────────────────────────────────────

def assert_enum(expected: Mapping[str, Any],
                values: Sequence[str]) -> AssertionOutcome:
    allowed = expected.get("enum_values") or []
    if not allowed:
        return _skip("enum constraint carries no values (registry defect)")
    if not values:
        return _skip("field absent — presence is occurrence/mandatory's question")
    allowed_set = {str(a) for a in allowed}
    for v in values:
        if v not in allowed_set:   # exact, case-sensitive — codes are codes
            return _fail(f"value {v!r} not in enum {sorted(allowed_set)}")
    return _OK


# ── pattern ──────────────────────────────────────────────────────────────────

def assert_pattern(expected: Mapping[str, Any],
                   values: Sequence[str]) -> AssertionOutcome:
    raw = str(expected.get("pattern_rule") or "")
    try:
        compiled = re.compile(raw)
    except re.error as exc:
        return _skip(f"pattern {raw!r} does not compile ({exc}) — registry defect")
    if not values:
        return _skip("field absent — presence is occurrence/mandatory's question")
    for v in values:
        if not compiled.fullmatch(v):   # XSD pattern semantics: whole value
            return _fail(f"value {v!r} does not match pattern {raw!r}")
    return _OK


# ── response code (never reads the payload) ──────────────────────────────────

def assert_response_code(expected: Mapping[str, Any],
                         actual_code: str | None) -> AssertionOutcome:
    exp_code = expected.get("code")
    exp_result = (expected.get("result") or "").strip().upper() or None
    if actual_code is None:
        return _skip("no response code observed")
    if exp_code is not None:
        return _OK if str(actual_code) == str(exp_code) \
            else _fail(f"response code {actual_code!r}, expected {exp_code!r}")
    if exp_result is not None:
        # Without a declared code, "00" is the success spelling on this wire.
        succeeded = str(actual_code) == "00"
        wanted_success = exp_result == "SUCCESS"
        return _OK if succeeded == wanted_success else _fail(
            f"outcome {'success' if succeeded else 'failure'} "
            f"({actual_code!r}), expected {exp_result}")
    return _skip("no expected outcome recorded for this variant")


# ── evaluation over spec rows ────────────────────────────────────────────────

@dataclass(frozen=True)
class SpecOutcome:
    spec: Any                     # the row (duck-typed), untouched
    status: str
    reason: str | None
    kind: str
    field_path: str | None


def _parent_path(path: str) -> str:
    """The element path one level up — the field's containing element.

    `A/B/@c` → `A/B`; `A/B/C` → `A/B`. An attribute and the element it hangs
    on share a parent element, so a single rsplit is right for both."""
    return path.rsplit("/", 1)[0]


def _ancestor_absent(path: str, doc: Any, codec: "WireCodec") -> bool:
    """True when this field sits under an intermediate element that is ABSENT
    from the captured document. Only intermediate ancestors count: a direct
    child of the message root has no optional ancestor to defer to (the root
    is present by construction — the doc was matched on it)."""
    parent = _parent_path(path)
    if "/" not in parent:          # parent IS the root → always present here
        return False
    return codec.count(doc, parent) == 0


_BY_COUNT = {"occurrence": assert_occurrence, "mandatory": assert_mandatory}
_BY_VALUES = {"datatype": assert_datatype, "length": assert_length,
              "enum": assert_enum, "pattern": assert_pattern}


def evaluate_specs(
    specs: Sequence[Any],
    *,
    request_body: str | bytes | None,
    response_body: str | bytes | None,
    actual_code: str | None,
    codec: WireCodec,
) -> list[SpecOutcome]:
    """Grade every spec row against the captured exchange.

    Duck-typed: a spec needs only `.assertion_kind`, `.expected` and
    `.field_path`. Which captured body a field spec reads is decided by the
    path's ROOT segment matching the parsed document's root — a `ReqTransfer/...`
    path grades the captured request, a `RespTransfer/...` path the response.

    THE SAFETY RULE (C-3): no captured payload → field assertions SKIP, never
    FAIL. With the body missing, "expected ≥1 occurrence, found 0" would fail
    the PARTNER for the AUTHORITY's missing data — the most damaging wrong
    answer available. An unparseable capture is the same story (our capture
    defect), via `CodecError`. `response_code` still runs: it is not derived
    from the payload.
    """
    docs: list[Any] = []
    unparseable: list[str] = []
    for body in (request_body, response_body):
        if body is None or (isinstance(body, (str, bytes)) and not body):
            continue
        try:
            docs.append(codec.parse(body))
        except CodecError as exc:
            unparseable.append(str(exc))

    outcomes: list[SpecOutcome] = []
    for spec in specs:
        kind = spec.assertion_kind
        expected = spec.expected or {}
        path = getattr(spec, "field_path", None)

        if kind == "response_code":
            outcome = assert_response_code(expected, actual_code)
        elif not path:
            outcome = _skip(f"{kind} assertion carries no field path")
        else:
            root = path.split("/", 1)[0]
            # Which captured body carries this message? The one whose root
            # matches the path's first segment — asked through the codec
            # itself (`count` on the bare root), so no format specifics leak
            # in here.
            doc = next((d for d in docs if codec.count(d, root) >= 1), None)
            if doc is None:
                reason = ("captured payload unparseable — our capture defect"
                          if unparseable else
                          f"no captured payload for {root} — not the partner's failure")
                outcome = _skip(reason)
            elif kind in _BY_COUNT and _ancestor_absent(path, doc, codec):
                # A required field nested under an OPTIONAL ancestor is only
                # required WHEN that ancestor is present — XSD minOccurs is
                # relative to the parent. `RespAvailabilityCheck/Book/@isbn`
                # is mandatory within Book, but a FAILURE response legitimately
                # carries no Book at all; asserting isbn there would fail the
                # partner for correctly omitting the whole optional element.
                # The ancestor's OWN occurrence assertion governs whether its
                # absence is allowed; this child defers to it.
                outcome = _skip(
                    f"ancestor {_parent_path(path)!r} absent — presence is that "
                    "element's occurrence to judge, not this field's")
            elif kind in _BY_COUNT:
                outcome = _BY_COUNT[kind](expected, codec.count(doc, path))
            elif kind in _BY_VALUES:
                outcome = _BY_VALUES[kind](expected, list(codec.values(doc, path)))
            else:
                outcome = _skip(f"unknown assertion kind {kind!r}")

        outcomes.append(SpecOutcome(spec=spec, status=outcome.status,
                                    reason=outcome.reason, kind=kind,
                                    field_path=path))
    return outcomes


def assertion_failures(outcomes: Sequence[SpecOutcome]) -> list[dict]:
    """The wire-bound digest: ONLY failures travel (C-3). A full list would be
    mostly skips; the failing field + rule is what makes a defect notice
    fixable."""
    return [
        {"field": o.field_path, "kind": o.kind,
         "expected": dict(o.spec.expected or {}), "reason": o.reason}
        for o in outcomes if o.status == FAIL
    ]
