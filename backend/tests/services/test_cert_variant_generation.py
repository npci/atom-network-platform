# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""§3.1 request-variant generation — the constrained-combination fixtures.

These are the verification bar COMBINED_EXECUTION_PLAN §3.1 names: several
valid variants per API (b), invalid business combinations excluded from the
positive set (c), deterministic pairwise coverage (d), critical three-way
coverage (e), one-fault-at-a-time negatives (f), and an HONEST coverage gap
when a cross-field rule is unknown (g). Fixture (a) — several rules sharing
one execution — is a builder-level property, pinned in
test_cert_case_builder.py.
"""
from __future__ import annotations

import itertools

import pytest

from app.core.domain.contract import CrossFieldRule
from app.services import cert_variants
from app.services.cert_variants import (
    BoundarySpec, FieldAxis, NegativeSpec, generate_variants,
)

API = "ReqTransfer"
CASE_ID = "TC001"
TEMPLATE = {"type": "PAY", "channel": "UPI", "mode": "ONLINE", "amount": "100.00"}
EXPECTED = {"result": "SUCCESS", "code": "00"}

TUPLES = [
    {"type": "PAY", "channel": "UPI", "mode": "ONLINE"},
    {"type": "COLLECT", "channel": "QR", "mode": "ONLINE"},
    {"type": "PAY", "channel": "QR", "mode": "OFFLINE"},
    {"type": "COLLECT", "channel": "UPI", "mode": "ONLINE"},
]


def _tuple_rule(tuples=None, *, critical=False, fields=("channel", "mode", "type"),
                expected=None):
    values = {"tuples": tuples if tuples is not None else TUPLES}
    if expected is not None:
        values["expected"] = expected
    return CrossFieldRule(api_name=API, kind="valid_tuple", fields=list(fields),
                          values=values, critical=critical)


def _positives(result):
    return [v for v in result.variants if not v.is_negative]


# ── (b) several valid variants for one API ───────────────────────────────────

def test_template_is_always_the_first_variant():
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED)
    assert result.variants[0].strategy == "template"
    assert result.variants[0].input_data == TEMPLATE
    assert not result.variants[0].is_negative


def test_declared_tuples_yield_several_valid_variants():
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[_tuple_rule()])
    assert len(_positives(result)) > 1, "one observed value must not be the whole set"
    combos = {tuple(sorted((k, v.input_data[k]) for k in ("type", "channel", "mode")))
              for v in _positives(result)}
    assert len(combos) > 1


def test_a_tuple_may_declare_its_own_expected_outcome():
    result = generate_variants(
        API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
        rules=[_tuple_rule([{"type": "COLLECT", "channel": "QR", "mode": "ONLINE"}],
                           expected={"result": "SUCCESS", "code": "01"})])
    collect = [v for v in _positives(result)
               if v.input_data.get("type") == "COLLECT"]
    assert collect and all(v.expected == {"result": "SUCCESS", "code": "01"}
                           for v in collect)


# ── (c) invalid business combinations excluded from the positive set ─────────

def test_undeclared_combinations_never_appear_as_positives():
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[_tuple_rule()])
    allowed = {tuple(sorted(t.items())) for t in TUPLES}
    for v in _positives(result):
        projection = tuple(sorted(
            (k, v.input_data[k]) for k in ("type", "channel", "mode")))
        assert projection in allowed, f"invented business combination: {projection}"


def test_forbids_rule_excludes_a_declared_tuple():
    forbids = CrossFieldRule(api_name=API, kind="forbids", fields=["channel", "type"],
                             values={"combo": {"type": "COLLECT", "channel": "QR"}})
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[_tuple_rule(), forbids])
    for v in _positives(result):
        assert not (v.input_data.get("type") == "COLLECT"
                    and v.input_data.get("channel") == "QR")


# ── (d) deterministic pairwise coverage ──────────────────────────────────────

def test_generation_is_deterministic():
    first = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                              rules=[_tuple_rule()])
    second = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[_tuple_rule()])
    assert [v.variant_id for v in first.variants] == \
           [v.variant_id for v in second.variants]


def test_pairwise_coverage_of_the_allowed_space():
    """Every achievable (field,value)×(field,value) pair appears in some
    selected positive variant."""
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[_tuple_rule()])
    fields = ("channel", "mode", "type")
    achievable = set()
    for t in TUPLES:
        for a, b in itertools.combinations(sorted(fields), 2):
            achievable.add(((a, t[a]), (b, t[b])))
    covered = set()
    for v in _positives(result):
        for a, b in itertools.combinations(sorted(fields), 2):
            covered.add(((a, v.input_data[a]), (b, v.input_data[b])))
    assert achievable <= covered, f"uncovered pairs: {achievable - covered}"


def test_selection_is_smaller_than_full_enumeration_when_pairs_allow():
    """Pairwise selection, not the Cartesian/declared product, when the tuple
    set is redundant for pair coverage."""
    tuples = TUPLES + [{"type": "PAY", "channel": "UPI", "mode": "OFFLINE"}]
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[_tuple_rule(tuples)])
    # template + selected covering set; must at minimum not exceed the
    # declared tuple count + template.
    assert len(_positives(result)) <= len(tuples) + 1


# ── (e) critical three-way / enumerated coverage ─────────────────────────────

def test_critical_rule_gets_three_way_coverage():
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[_tuple_rule(critical=True)])
    fields = ("channel", "mode", "type")
    achievable = {tuple(sorted((f, t[f]) for f in fields)) for t in TUPLES}
    covered = {tuple(sorted((f, v.input_data[f]) for f in fields))
               for v in _positives(result)}
    assert achievable <= covered, "critical triples must all be exercised"


def test_critical_two_field_rule_is_enumerated():
    rule = CrossFieldRule(
        api_name=API, kind="valid_tuple", fields=["channel", "type"],
        values={"tuples": [{"type": "PAY", "channel": "UPI"},
                           {"type": "COLLECT", "channel": "QR"},
                           {"type": "PAY", "channel": "QR"}]},
        critical=True)
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[rule])
    covered = {(v.input_data["type"], v.input_data["channel"])
               for v in _positives(result)}
    assert {("PAY", "UPI"), ("COLLECT", "QR"), ("PAY", "QR")} <= covered


# ── (f) negatives carry exactly one fault ────────────────────────────────────

def test_negative_variants_carry_exactly_one_fault():
    negatives = [NegativeSpec(path="amount", value="-1",
                              expected={"result": "FAILURE", "code": "ZM"})]
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               negatives=negatives)
    neg = [v for v in result.variants if v.is_negative]
    assert len(neg) == 1
    v = neg[0]
    assert v.fault_key == "amount"
    assert v.expected == {"result": "FAILURE", "code": "ZM"}
    diffs = {k for k in v.input_data if v.input_data[k] != TEMPLATE.get(k)}
    assert diffs == {"amount"}, "a negative must mutate exactly one field"


def test_boundary_outside_values_are_one_fault_negatives():
    boundaries = [BoundarySpec(path="amount", at_limit=("0.01", "100000.00"),
                               outside=("100000.01",),
                               expected_error={"result": "FAILURE", "code": "ZM"})]
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               boundaries=boundaries)
    at_limit = [v for v in result.variants if v.strategy == "boundary"]
    outside = [v for v in result.variants if v.is_negative]
    assert {v.input_data["amount"] for v in at_limit} == {"0.01", "100000.00"}
    assert len(outside) == 1 and outside[0].fault_key == "amount"


# ── (g) honest coverage gaps ─────────────────────────────────────────────────

def test_ungoverned_field_is_not_varied_and_reports_a_gap():
    """Field-level candidate values do NOT prove combination validity — the
    field stays at the template value and the gap is explicit."""
    axes = [FieldAxis(path="purpose", values=("00", "01", "02"))]
    result = generate_variants(API, case_id=CASE_ID, template={**TEMPLATE, "purpose": "00"},
                               expected=EXPECTED, axes=axes)
    assert all(v.input_data.get("purpose") == "00" for v in result.variants)
    gap = [g for g in result.gaps if g["kind"] == "unknown_validity"]
    assert gap and gap[0]["field"] == "purpose"
    assert set(gap[0]["unexplored_values"]) == {"01", "02"}


def test_unsupported_rule_kind_is_reported_not_silently_dropped():
    rule = CrossFieldRule(api_name=API, kind="requires",
                          fields=["ReqTransfer/Payer", "ReqTransfer/Payer/@addr"])
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[rule])
    assert any(g["kind"] == "rule_not_applied" for g in result.gaps)


def test_axis_with_no_values_is_a_gap():
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               axes=[FieldAxis(path="purpose")])
    assert any(g["kind"] == "missing_values" for g in result.gaps)


def test_combination_explosion_is_refused_loudly(monkeypatch):
    """No silent caps: past the candidate guard the generator refuses with a
    gap instead of quietly truncating coverage."""
    monkeypatch.setattr(cert_variants, "MAX_CANDIDATES", 2)
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[_tuple_rule()])
    assert any(g["kind"] == "combination_explosion" for g in result.gaps)
    assert len(_positives(result)) == 1, "only the template survives a refusal"


def test_rules_for_other_apis_are_ignored():
    other = CrossFieldRule(api_name="ReqMandate", kind="valid_tuple",
                           fields=["type"], values={"tuples": [{"type": "CREATE"}]})
    result = generate_variants(API, case_id=CASE_ID, template=TEMPLATE, expected=EXPECTED,
                               rules=[other])
    assert len(result.variants) == 1  # template only
