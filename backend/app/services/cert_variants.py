# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Request-variant generation (COMBINED_EXECUTION_PLAN §3.1) — pure.

One API is exercised with a SMALL, DETERMINISTIC set of valid input
combinations, not one observed sample and not a Cartesian explosion. This
module is the selection logic only: plain data in (`template`, axes, declared
`CrossFieldRule`s, boundary/negative specs), `VariantSpec`s out. No DB, no
simulator, no pack — the case builder supplies the inputs and persists the
output.

THE HONESTY RULES, which this module enforces rather than documents:

* Business validity is NEVER inferred from field-level constraints. A field
  with several candidate values that no declared rule governs is NOT varied —
  it stays at the template value and the unexplored values are reported as an
  `unknown_validity` gap for operator review. Only `valid_tuple` rules create
  multi-field variation, because only they DECLARE which combinations are
  valid.
* Negative variants carry exactly ONE fault (`fault_key`), so a rejection is
  attributable. Multi-error probing is a separately labelled concern, not a
  default.
* Selection is deterministic: same inputs → same `variant_id`s and the same
  selected set. IDs are content hashes; ordering is by sorted field path,
  never dict/set iteration order.

Coverage: constraint-aware pairwise by default over the rule-governed value
space; a rule marked `critical` upgrades its fields to three-way (or full
enumeration when it has fewer than three fields). Unsupported or unusable rule
kinds are surfaced as gaps, never silently dropped.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field as dc_field
from typing import Any, Mapping, Sequence

from app.core.domain.contract import CrossFieldRule

__all__ = [
    "FieldAxis", "BoundarySpec", "NegativeSpec", "VariantSpec",
    "GenerationResult", "generate_variants",
]

# Guard against a silent Cartesian explosion: beyond this many raw candidate
# combinations, the generator refuses (with a gap) rather than under-testing
# quietly. §7: "Do not hide combinatorial explosion by under-testing."
MAX_CANDIDATES = 5000


@dataclass(frozen=True)
class FieldAxis:
    """Candidate VALID values for one field, from an approved source (test-case
    catalogue, partner test data, approved scenarios) — never invented."""

    path: str
    values: tuple[str, ...] = ()
    critical: bool = False


@dataclass(frozen=True)
class BoundarySpec:
    """Declared boundary values for one field: `at_limit` are valid extremes,
    `outside` are just-past-the-limit values expected to be rejected."""

    path: str
    at_limit: tuple[str, ...] = ()
    outside: tuple[str, ...] = ()
    expected_error: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class NegativeSpec:
    """One declared invalid value for one field — one fault, attributable."""

    path: str
    value: str
    expected: Mapping[str, Any] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class VariantSpec:
    variant_id: str
    api_name: str
    input_data: Mapping[str, str]
    expected: Mapping[str, Any]
    strategy: str          # template|pairwise|three_way|enumerated|boundary|negative
    covered_rules: tuple[str, ...] = ()
    is_negative: bool = False
    fault_key: str | None = None


@dataclass
class GenerationResult:
    variants: list[VariantSpec]
    gaps: list[dict]       # honest, machine-readable, for operator review


def _canon(value) -> str:
    """Canonical JSON for hashing AND for ordering. Sorted keys and fixed
    separators, so the string depends on CONTENT only — never on the order the
    caller happened to supply."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def variant_id_for(*, case_id: str, api_name: str, input_data: Mapping[str, str],
                   strategy: str, expected: Mapping[str, object] | None = None) -> str:
    """Deterministic identity for one variant, SCOPED TO ITS CASE.

    `case_id` and `expected` are part of the identity, not decoration. Two
    catalogue cases can legitimately carry identical request inputs for the
    same API while differing in case id and expected response — they are
    different executions and must get different ids. Hashing inputs alone
    collided them, and since the round's uniqueness key is
    (cflow_id, run_number, case_id, variant_id), a collision aborted the whole
    round's storage with an IntegrityError. Including `expected` additionally
    separates two variants of one case that share inputs but assert different
    outcomes (e.g. rival tuple rules carrying their own `expected`), which the
    emit-dedup would otherwise silently collapse to whichever came first.
    """
    canonical = _canon({
        "case": case_id,
        "api": (api_name or "").lower(),
        "input": dict(sorted(input_data.items())),
        "strategy": strategy,
        "expected": dict(expected or {}),
    })
    return "v" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _rule_key(rule: CrossFieldRule) -> str:
    return f"{rule.kind}:{'+'.join(sorted(rule.fields))}"


def _tuples_of(rule: CrossFieldRule) -> list[dict[str, str]]:
    """The enumerated valid combinations a `valid_tuple` rule declares:
    `values["tuples"]` is a list of {field path: value} mappings.

    Returned in CANONICAL order, not declaration order: the selected covering
    set must depend on the rule's CONTENT, so that two equivalent registry
    snapshots that happen to list the same tuples in a different sequence
    dispatch the same requests.
    """
    raw = rule.values.get("tuples") or []
    tuples = [dict(t) for t in raw if isinstance(t, Mapping)]
    return sorted(tuples, key=_canon)


def _forbidden(combo: Mapping[str, str], rules: Sequence[CrossFieldRule]) -> bool:
    for rule in rules:
        if rule.kind != "forbids":
            continue
        bad = rule.values.get("combo") or {}
        if bad and all(combo.get(p) == v for p, v in bad.items()):
            return True
    return False


def _allowed(combo: Mapping[str, str], tuple_rules: Sequence[CrossFieldRule],
             all_rules: Sequence[CrossFieldRule]) -> bool:
    """A combo is allowed when every applicable valid_tuple rule lists its
    projection and no forbids rule matches it."""
    for rule in tuple_rules:
        if not all(f in combo for f in rule.fields):
            continue
        projection = {f: combo[f] for f in rule.fields}
        if projection not in _tuples_of(rule):
            return False
    return not _forbidden(combo, all_rules)


def generate_variants(
    api_name: str,
    *,
    case_id: str,
    template: Mapping[str, str],
    expected: Mapping[str, Any],
    axes: Sequence[FieldAxis] = (),
    rules: Sequence[CrossFieldRule] = (),
    boundaries: Sequence[BoundarySpec] = (),
    negatives: Sequence[NegativeSpec] = (),
) -> GenerationResult:
    """The §3.1 covering set for one API, for ONE case.

    `case_id` scopes variant identity: two catalogue cases may carry identical
    request inputs for the same API and are still different executions.

    `template` is a KNOWN-VALID input combination (catalogue / approved data)
    and is always the first variant. `expected` is the outcome every positive
    variant asserts (per-variant overrides ride on rule tuples via
    `values["expected"]` — a declared tuple may declare its own outcome).

    DETERMINISM IS BY CONTENT, NOT BY INPUT ORDER. Rule tuples, the candidate
    combination space and greedy tie-breaks are all ordered by canonical JSON,
    so two equivalent registry snapshots that list the same rules/tuples in a
    different sequence select the SAME covering set. Anything else would let
    equivalent snapshots dispatch different requests.
    """
    gaps: list[dict] = []
    variants: list[VariantSpec] = []
    seen: set[str] = set()

    def _emit(input_data: Mapping[str, str], strategy: str, *,
              exp: Mapping[str, Any] | None = None,
              covered: tuple[str, ...] = (),
              is_negative: bool = False, fault_key: str | None = None) -> None:
        resolved = dict(exp if exp is not None else expected)
        vid = variant_id_for(case_id=case_id, api_name=api_name,
                             input_data=input_data, strategy=strategy,
                             expected=resolved)
        if vid in seen:
            return
        seen.add(vid)
        variants.append(VariantSpec(
            variant_id=vid, api_name=api_name, input_data=dict(input_data),
            expected=resolved,
            strategy=strategy, covered_rules=covered,
            is_negative=is_negative, fault_key=fault_key,
        ))

    api_rules = [r for r in rules if r.api_name.lower() == api_name.lower()]
    tuple_rules = [r for r in api_rules if r.kind == "valid_tuple"]
    known_kinds = {"valid_tuple", "forbids"}
    for rule in api_rules:
        if rule.kind not in known_kinds:
            # requires/conditional/exactly_one/… are presence rules over
            # message STRUCTURE; variant generation cannot apply them to a
            # value combination, and pretending otherwise would claim
            # coverage this module does not provide.
            gaps.append({"kind": "rule_not_applied", "api": api_name,
                         "rule": _rule_key(rule),
                         "reason": f"rule kind {rule.kind!r} is not applicable "
                                   "to value-combination selection"})

    # 1) The known-valid template, always.
    _emit(template, "template")

    # 2) Which fields may vary at all: only those governed by a valid_tuple
    #    rule. Everything else with candidate values is a reported gap.
    governed: set[str] = set()
    for rule in tuple_rules:
        governed.update(rule.fields)
    for axis in sorted(axes, key=lambda a: a.path):
        if not axis.values:
            gaps.append({"kind": "missing_values", "api": api_name,
                         "field": axis.path,
                         "reason": "axis declared with no usable values"})
        elif axis.path not in governed and len(axis.values) > 1:
            gaps.append({"kind": "unknown_validity", "api": api_name,
                         "field": axis.path,
                         "unexplored_values": sorted(set(axis.values) - {template.get(axis.path)}),
                         "reason": "no declared cross-field rule governs this "
                                   "field's combinations — not varied"})

    # 3) The allowed combination space: the union of declared tuples, laid
    #    over the template, then CANONICALLY ORDERED and de-duplicated. The
    #    canonical sort is what makes selection independent of the order the
    #    rules and their tuples were declared in.
    combos: list[tuple[dict[str, str], tuple[str, ...], Mapping[str, Any] | None]] = []
    _seen_combo: set[str] = set()
    for rule in sorted(tuple_rules, key=_rule_key):
        for tup in _tuples_of(rule):
            candidate = {**template, **tup}
            if not _allowed(candidate, tuple_rules, api_rules):
                continue   # excluded by another tuple rule or a forbids rule
            exp_override = rule.values.get("expected")
            dedup_key = _canon([candidate, exp_override])
            if dedup_key in _seen_combo:
                continue   # same candidate reached via two rules
            _seen_combo.add(dedup_key)
            combos.append((candidate, (_rule_key(rule),), exp_override))
    combos.sort(key=lambda c: (_canon(c[0]), _canon(c[2]), c[1]))
    if len(combos) > MAX_CANDIDATES:
        gaps.append({"kind": "combination_explosion", "api": api_name,
                     "candidates": len(combos), "cap": MAX_CANDIDATES,
                     "reason": "declared tuple space exceeds the candidate cap; "
                               "selection refused rather than silently truncated"})
        combos = []

    # 4) Coverage targets over the allowed space: every achievable PAIR of
    #    governed (field, value) assignments; TRIPLES for critical rules'
    #    fields (three-way), full enumeration when a critical rule has < 3
    #    fields.
    def _projections(combo: Mapping[str, str], fields: Sequence[str], k: int):
        usable = [f for f in sorted(fields) if f in combo]
        for group in itertools.combinations(usable, k):
            yield tuple((f, combo[f]) for f in group)

    governed_sorted = sorted(governed)
    targets: set[tuple] = set()
    enumerated_rules: list[CrossFieldRule] = []
    for combo, _, _ in combos:
        targets.update(_projections(combo, governed_sorted, min(2, len(governed_sorted))))
    for rule in sorted((r for r in tuple_rules if r.critical), key=_rule_key):
        if len(rule.fields) >= 3:
            for combo, _, _ in combos:
                targets.update(_projections(combo, rule.fields, 3))
        else:
            enumerated_rules.append(rule)

    # 5) Deterministic greedy selection: repeatedly take the combo covering
    #    the most uncovered targets. TIES BREAK ON THE CANONICAL KEY, never on
    #    position: `combos` is already canonically sorted, and comparing
    #    (-hits, canonical) makes the winner a pure function of content. Using
    #    first-encountered instead made an equivalent-but-reordered rule set
    #    select a different covering set on every shuffle.
    uncovered = set(targets)
    while uncovered:
        best_key, best_idx, best_hits = None, -1, frozenset()
        for idx, (combo, _, exp_override) in enumerate(combos):
            hits = frozenset(
                t for t in uncovered
                if all(combo.get(f) == v for f, v in t)
            )
            if not hits:
                continue
            key = (-len(hits), _canon(combo), _canon(exp_override))
            if best_key is None or key < best_key:
                best_key, best_idx, best_hits = key, idx, hits
        if best_idx < 0:
            break   # remaining targets unreachable — they came from combos, so this is defensive
        combo, covered, exp_override = combos[best_idx]
        strategy = "three_way" if any(len(t) == 3 for t in best_hits) else "pairwise"
        _emit(combo, strategy, exp=exp_override, covered=covered)
        uncovered -= best_hits

    # Critical rules too small for triples: every declared tuple, verbatim.
    for rule in enumerated_rules:
        for tup in _tuples_of(rule):
            candidate = {**template, **tup}
            if _allowed(candidate, tuple_rules, api_rules):
                exp_override = rule.values.get("expected")
                _emit(candidate, "enumerated", exp=exp_override,
                      covered=(_rule_key(rule),))

    # 6) Boundaries: valid extremes as positives, just-outside as one-fault
    #    negatives.
    for spec in sorted(boundaries, key=lambda b: b.path):
        for value in spec.at_limit:
            _emit({**template, spec.path: value}, "boundary")
        for value in spec.outside:
            _emit({**template, spec.path: value}, "negative",
                  exp=spec.expected_error or {},
                  is_negative=True, fault_key=spec.path)

    # 7) Declared negatives: one intentional fault per variant.
    for spec in sorted(negatives, key=lambda n: (n.path, n.value)):
        _emit({**template, spec.path: spec.value}, "negative",
              exp=spec.expected, is_negative=True, fault_key=spec.path)

    return GenerationResult(variants=variants, gaps=gaps)
