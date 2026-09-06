# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Regressions from the aggressive validation pass (defects D1 and D2).

Both were reproduced by a tester against code whose full suite was green — the
sequential happy-path tests could not see either. Each test here FAILS on the
pre-fix implementation:

  D1 (BLOCKER) two catalogue cases with identical request inputs hashed to the
     same `variant_id`, and the round's uniqueness key omitted `case_id`, so
     storing a normal catalogue aborted the whole round with an IntegrityError.

  D2 (MAJOR) the covering set depended on the ORDER rules/tuples were declared
     in: 20 shuffled runs produced 14 different outputs. Equivalent registry
     snapshots could dispatch different requests.
"""
from __future__ import annotations

import itertools
import json
import random

import pytest

from app.core.domain.contract import CrossFieldRule
from app.services.cert_variants import generate_variants, variant_id_for

API = "ReqTransfer"
TEMPLATE = {"type": "PAY", "channel": "UPI", "mode": "ONLINE"}
EXPECTED = {"result": "SUCCESS", "code": "00"}


# ── D1: variant identity is scoped to its case ───────────────────────────────

def test_same_inputs_different_cases_get_different_variant_ids():
    """THE D1 REPRO. Two cases, identical request data, different case ids and
    expected responses — they are different executions and must not collide."""
    a = generate_variants(API, case_id="TC-1", template=TEMPLATE,
                          expected={"result": "SUCCESS", "code": "00"})
    b = generate_variants(API, case_id="TC-2", template=TEMPLATE,
                          expected={"result": "FAILURE", "code": "ZM"})
    ids_a = {v.variant_id for v in a.variants}
    ids_b = {v.variant_id for v in b.variants}
    assert ids_a and ids_b
    assert not (ids_a & ids_b), \
        "two cases collided on variant_id — storing the round will IntegrityError"


def test_same_case_same_inputs_still_stable():
    """Scoping by case must not cost determinism WITHIN a case."""
    first = generate_variants(API, case_id="TC-1", template=TEMPLATE, expected=EXPECTED)
    second = generate_variants(API, case_id="TC-1", template=TEMPLATE, expected=EXPECTED)
    assert [v.variant_id for v in first.variants] == \
           [v.variant_id for v in second.variants]


def test_expected_outcome_is_part_of_variant_identity():
    """Same case, same inputs, different asserted outcome = different variant.
    Without this the emit-dedup silently collapsed them to whichever ran first."""
    one = variant_id_for(case_id="TC-1", api_name=API, input_data=TEMPLATE,
                         strategy="template", expected={"code": "00"})
    two = variant_id_for(case_id="TC-1", api_name=API, input_data=TEMPLATE,
                         strategy="template", expected={"code": "ZM"})
    assert one != two


def test_variant_id_ignores_input_key_order():
    """Identity is content, not dict construction order."""
    forward = variant_id_for(case_id="TC-1", api_name=API,
                             input_data={"a": "1", "b": "2"}, strategy="template")
    reverse = variant_id_for(case_id="TC-1", api_name=API,
                             input_data={"b": "2", "a": "1"}, strategy="template")
    assert forward == reverse


# ── D2: selection depends on content, never on declaration order ─────────────

def _shuffled_rule(rng, tuples, *, critical=False):
    shuffled = list(tuples)
    rng.shuffle(shuffled)
    return CrossFieldRule(api_name=API, kind="valid_tuple",
                          fields=["channel", "mode", "type"],
                          values={"tuples": shuffled}, critical=critical)


def _fingerprint(result):
    """What the round would actually dispatch: the selected inputs, in order."""
    return json.dumps([[v.variant_id, sorted(v.input_data.items())]
                       for v in result.variants], sort_keys=True)


FULL_BINARY_SPACE = [
    dict(zip(("type", "channel", "mode"), combo))
    for combo in itertools.product(("PAY", "COLLECT"), ("UPI", "QR"),
                                   ("ONLINE", "OFFLINE"))
]


def test_shuffled_tuple_order_selects_an_identical_covering_set():
    """THE D2 REPRO, at the tester's scale: 50 shuffles of the complete
    three-field binary tuple space must all select the SAME set."""
    rng = random.Random(20260830)
    prints = {
        _fingerprint(generate_variants(
            API, case_id="TC-1", template=TEMPLATE, expected=EXPECTED,
            rules=[_shuffled_rule(rng, FULL_BINARY_SPACE)]))
        for _ in range(50)
    }
    assert len(prints) == 1, \
        f"same tuple data selected {len(prints)} different covering sets"


def test_shuffled_rule_order_selects_an_identical_covering_set():
    """Two rules, order between them shuffled — same conclusion."""
    rng = random.Random(7)
    rule_a = CrossFieldRule(api_name=API, kind="valid_tuple",
                            fields=["type", "channel"],
                            values={"tuples": [{"type": "PAY", "channel": "UPI"},
                                               {"type": "COLLECT", "channel": "QR"}]})
    rule_b = CrossFieldRule(api_name=API, kind="valid_tuple",
                            fields=["mode"],
                            values={"tuples": [{"mode": "ONLINE"},
                                               {"mode": "OFFLINE"}]})
    prints = set()
    for _ in range(20):
        rules = [rule_a, rule_b]
        rng.shuffle(rules)
        prints.add(_fingerprint(generate_variants(
            API, case_id="TC-1", template=TEMPLATE, expected=EXPECTED, rules=rules)))
    assert len(prints) == 1, f"rule order changed the outcome ({len(prints)} variants)"


def test_shuffled_order_still_covers_every_achievable_pair():
    """Determinism must not be bought by selecting less. Whatever set is
    chosen, it still has to cover the pairs."""
    rng = random.Random(99)
    result = generate_variants(
        API, case_id="TC-1", template=TEMPLATE, expected=EXPECTED,
        rules=[_shuffled_rule(rng, FULL_BINARY_SPACE)])
    fields = ("channel", "mode", "type")
    achievable, covered = set(), set()
    for t in FULL_BINARY_SPACE:
        for a, b in itertools.combinations(sorted(fields), 2):
            achievable.add(((a, t[a]), (b, t[b])))
    for v in (x for x in result.variants if not x.is_negative):
        for a, b in itertools.combinations(sorted(fields), 2):
            covered.add(((a, v.input_data[a]), (b, v.input_data[b])))
    assert achievable <= covered, f"uncovered pairs: {achievable - covered}"


def test_critical_three_way_is_also_order_independent():
    rng = random.Random(11)
    prints = {
        _fingerprint(generate_variants(
            API, case_id="TC-1", template=TEMPLATE, expected=EXPECTED,
            rules=[_shuffled_rule(rng, FULL_BINARY_SPACE, critical=True)]))
        for _ in range(20)
    }
    assert len(prints) == 1
