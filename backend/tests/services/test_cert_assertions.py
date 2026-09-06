# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-2: the assertion engine — three outcomes, table-driven per kind.

The mutation-run lesson is baked in: `SKIP` redefined as a pass once kept 70 of
72 tests green, so two tests pin the outcomes as DISTINCT VALUES and every
skip-test asserts the status is SKIP and not PASS. The `length_rule` trap
(string bounds vs numeric bounds in one column) is pinned in both directions.

The engine is pure: these tests import no models and build no database —
spec rows are `SimpleNamespace`, payloads are strings, the codec is the real
`XmlCodec`.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.wire.xml_codec import XmlCodec
from app.services.cert_assertions import (
    FAIL, PASS, SKIP,
    assert_datatype, assert_enum, assert_length, assert_mandatory,
    assert_occurrence, assert_pattern, assert_response_code,
    assertion_failures, evaluate_specs,
)

CODEC = XmlCodec()


# ── the three outcomes are distinct values (mutation pins) ───────────────────

def test_pass_fail_skip_are_three_distinct_values():
    assert len({PASS, FAIL, SKIP}) == 3


def test_skip_is_not_a_pass_in_disguise():
    """The surviving mutant: SKIP = <PASS's value>. Both pins must die it."""
    assert SKIP != PASS
    outcome = assert_datatype({"datatype": "Alphanumeric"}, ["x"])
    assert outcome.status == SKIP
    assert outcome.status != PASS
    assert outcome.reason, "a SKIP without a reason is indistinguishable from a shrug"


# ── occurrence ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rule,count,status", [
    ("1..1", 1, PASS),
    ("1..1", 0, FAIL),
    ("1..1", 2, FAIL),
    ("0..1", 0, PASS),
    ("0..1", 1, PASS),
    ("0..1", 2, FAIL),
    ("0..n", 0, PASS),       # the pinned boundary: 0..n accepts ZERO
    ("0..n", 17, PASS),
    ("1..n", 0, FAIL),
    ("1..n", 5, PASS),
    ("2..4", 4, PASS),       # at the upper limit, inclusive
    ("2..4", 5, FAIL),
    ("2..4", 1, FAIL),
])
def test_occurrence_bounds(rule, count, status):
    assert assert_occurrence({"occurrence": rule}, count).status == status


def test_unparseable_occurrence_skips_as_registry_defect():
    outcome = assert_occurrence({"occurrence": "whenever"}, 1)
    assert outcome.status == SKIP
    assert "registry defect" in outcome.reason


# ── datatype ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label", ["Alphanumeric", "alphanumeric", "Code", "Fixed value"])
def test_uncheckable_datatype_labels_skip_with_reasons(label):
    outcome = assert_datatype({"datatype": label}, ["anything"])
    assert outcome.status == SKIP and outcome.reason


@pytest.mark.parametrize("value", ["0", "42", "100.00", "-3.5"])
def test_numeric_values_pass_numeric(value):
    assert assert_datatype({"datatype": "Numeric"}, [value]).status == PASS


@pytest.mark.parametrize("value", ["abc", "12a", "", "1.2.3"])
def test_non_numeric_values_fail_numeric(value):
    assert assert_datatype({"datatype": "Numeric"}, [value]).status == FAIL


def test_unknown_datatype_label_skips():
    assert assert_datatype({"datatype": "Quantum"}, ["x"]).status == SKIP


def test_datatype_on_absent_field_skips():
    outcome = assert_datatype({"datatype": "Numeric"}, [])
    assert outcome.status == SKIP
    assert "absent" in outcome.reason


# ── length: the two-constraint-kinds-in-one-column trap ──────────────────────

@pytest.mark.parametrize("rule,value,status", [
    ("Min Length 1, Max Length 35", "a" * 35, PASS),    # exactly at the limit
    ("Min Length 1, Max Length 35", "a" * 36, FAIL),
    ("Min Length 2, Max Length 35", "a", FAIL),
    ("Max Length 10", "1234567890", PASS),
    ("Length 4", "abcd", PASS),
    ("Length 4", "abc", FAIL),
])
def test_string_length_bounds(rule, value, status):
    assert assert_length({"length_rule": rule}, [value]).status == status


@pytest.mark.parametrize("rule,value,status", [
    ("minInclusive 1, maxInclusive 100000", "100000", PASS),   # inclusive
    ("minInclusive 1, maxInclusive 100000", "100001", FAIL),
    ("minInclusive 1, maxInclusive 100000", "0", FAIL),
    ("minInclusive 10", "5", FAIL),
    ("minInclusive 10", "500", PASS),
])
def test_numeric_inclusive_bounds(rule, value, status):
    assert assert_length({"length_rule": rule}, [value]).status == status


def test_min_length_and_min_inclusive_are_different_constraints():
    """THE trap. "5" satisfies Min Length 1 (one character) but violates
    minInclusive 10 (the number five); "500" is three characters yet passes
    minInclusive 10. Conflate them and one of these flips."""
    assert assert_length({"length_rule": "Min Length 1"}, ["5"]).status == PASS
    assert assert_length({"length_rule": "minInclusive 10"}, ["5"]).status == FAIL
    assert assert_length({"length_rule": "minInclusive 10"}, ["500"]).status == PASS
    assert assert_length({"length_rule": "Min Length 4"}, ["500"]).status == FAIL


def test_max_inclusive_is_not_a_length():
    """maxInclusive 100000 must not fail every value longer than 5 digits —
    wait, 100000 IS 6 digits; a length reading would cap at 6 chars and fail
    "99999.99" (8 chars), which is numerically fine."""
    assert assert_length({"length_rule": "maxInclusive 100000"},
                         ["99999.99"]).status == PASS


def test_numeric_bound_on_non_numeric_value_fails():
    outcome = assert_length({"length_rule": "minInclusive 1"}, ["abc"])
    assert outcome.status == FAIL


def test_unparseable_length_rule_skips_as_registry_defect():
    outcome = assert_length({"length_rule": "As per annexure IV"}, ["x"])
    assert outcome.status == SKIP
    assert "registry defect" in outcome.reason


def test_length_on_absent_field_skips():
    assert assert_length({"length_rule": "Max Length 5"}, []).status == SKIP


# ── mandatory ────────────────────────────────────────────────────────────────

def test_mandatory_y_present_passes_absent_fails():
    assert assert_mandatory({"mandatory": "Y"}, 1).status == PASS
    assert assert_mandatory({"mandatory": "Y"}, 0).status == FAIL


def test_optional_field_skips():
    outcome = assert_mandatory({"mandatory": "N"}, 0)
    assert outcome.status == SKIP


def test_mandatory_c_is_surfaced_not_evaluated():
    """Conditionally mandatory quotes the condition rather than guessing at
    its truth — even when the field is absent."""
    outcome = assert_mandatory(
        {"mandatory": "C", "condition_text": "Required when type is PAY"}, 0)
    assert outcome.status == SKIP
    assert "Required when type is PAY" in outcome.reason


def test_mandatory_c_without_condition_text_still_says_so():
    outcome = assert_mandatory({"mandatory": "C"}, 1)
    assert outcome.status == SKIP
    assert "no condition text" in outcome.reason


def test_unknown_mandatory_flag_skips():
    assert assert_mandatory({"mandatory": "M"}, 1).status == SKIP


# ── enum ─────────────────────────────────────────────────────────────────────

def test_enum_member_passes_nonmember_fails():
    expected = {"enum_values": ["PAY", "COLLECT"]}
    assert assert_enum(expected, ["PAY"]).status == PASS
    assert assert_enum(expected, ["REFUND"]).status == FAIL


def test_enum_is_case_sensitive():
    """Codes are codes: `pay` is not `PAY` on a wire that says PAY."""
    assert assert_enum({"enum_values": ["PAY"]}, ["pay"]).status == FAIL


def test_enum_checks_every_occurrence():
    assert assert_enum({"enum_values": ["A", "B"]}, ["A", "C"]).status == FAIL


def test_enum_on_absent_field_skips():
    assert assert_enum({"enum_values": ["A"]}, []).status == SKIP


def test_enum_with_no_values_skips_as_registry_defect():
    assert assert_enum({"enum_values": []}, ["A"]).status == SKIP


# ── pattern ──────────────────────────────────────────────────────────────────

def test_pattern_full_match_semantics():
    """XSD patterns anchor to the whole value — a partial match is a FAIL."""
    expected = {"pattern_rule": r"[0-9]{4}"}
    assert assert_pattern(expected, ["1234"]).status == PASS
    assert assert_pattern(expected, ["12345"]).status == FAIL
    assert assert_pattern(expected, ["x1234"]).status == FAIL


def test_uncompilable_pattern_skips_as_registry_defect():
    """A bad regex is OUR defect; failing the partner for it blames the wrong
    party."""
    outcome = assert_pattern({"pattern_rule": "[unclosed"}, ["x"])
    assert outcome.status == SKIP
    assert "registry defect" in outcome.reason


def test_pattern_on_absent_field_skips():
    assert assert_pattern({"pattern_rule": ".*"}, []).status == SKIP


# ── response code ────────────────────────────────────────────────────────────

def test_response_code_exact_match():
    assert assert_response_code({"code": "00"}, "00").status == PASS
    assert assert_response_code({"code": "00"}, "ZM").status == FAIL


def test_response_code_result_only():
    assert assert_response_code({"result": "SUCCESS"}, "00").status == PASS
    assert assert_response_code({"result": "SUCCESS"}, "ZM").status == FAIL
    assert assert_response_code({"result": "FAILURE"}, "ZM").status == PASS


def test_response_code_without_observation_skips():
    assert assert_response_code({"code": "00"}, None).status == SKIP


def test_response_code_without_expectation_skips():
    assert assert_response_code({}, "00").status == SKIP


# ── evaluate_specs: duck-typed glue over captured payloads ───────────────────

REQ = ('<network:ReqTransfer xmlns:network="http://example.org/upi">'
       '<Head ver="2.0"/><Amt value="100.00" curr="INR"/><Amt value="1" curr="INR"/>'
       "</network:ReqTransfer>")
RESP = '<RespTransfer><Resp result="SUCCESS"/></RespTransfer>'


def _spec(kind, expected, path=None):
    return SimpleNamespace(assertion_kind=kind, expected=expected, field_path=path)


def test_evaluate_specs_is_duck_typed_over_plain_namespaces():
    outcomes = evaluate_specs(
        [_spec("occurrence", {"occurrence": "1..1"}, "ReqTransfer/Head"),
         _spec("response_code", {"code": "00"})],
        request_body=REQ, response_body=RESP, actual_code="00", codec=CODEC)
    assert [o.status for o in outcomes] == [PASS, PASS]


def test_field_specs_grade_the_body_whose_root_matches():
    outcomes = evaluate_specs(
        [_spec("enum", {"enum_values": ["SUCCESS", "FAILURE"]},
               "RespTransfer/Resp/@result"),
         _spec("occurrence", {"occurrence": "2..2"}, "ReqTransfer/Amt")],
        request_body=REQ, response_body=RESP, actual_code="00", codec=CODEC)
    assert [o.status for o in outcomes] == [PASS, PASS]


def test_missing_payload_skips_field_assertions_never_fails():
    """C-3's safety rule: with no captured body, "expected ≥1, found 0" would
    fail the partner for OUR missing data."""
    outcomes = evaluate_specs(
        [_spec("occurrence", {"occurrence": "1..1"}, "ReqTransfer/Head"),
         _spec("mandatory", {"mandatory": "Y"}, "ReqTransfer/Amt/@value")],
        request_body=None, response_body=None, actual_code="00", codec=CODEC)
    assert all(o.status == SKIP for o in outcomes)
    assert all("not the partner's failure" in o.reason for o in outcomes)


def test_required_field_under_absent_optional_ancestor_skips():
    """A mandatory field nested in an OPTIONAL element that is absent must not
    fail — XSD minOccurs is relative to the parent. NLLN CR-1: a FAILURE
    RespAvailabilityCheck legitimately carries no Book, so Book/@isbn
    (mandatory within Book) is not owed; the response is conformant."""
    resp = ('<RespAvailabilityCheck xmlns="http://nlln.in/schema/v1">'
            '<Head ver="1.0"/><Resp result="FAILURE" errCode="E009"/>'
            '</RespAvailabilityCheck>')
    outcomes = evaluate_specs(
        [_spec("occurrence", {"occurrence": "1..1"},
               "RespAvailabilityCheck/Book/@isbn"),
         _spec("mandatory", {"mandatory": "Y"},
               "RespAvailabilityCheck/Book/@isbn")],
        request_body=None, response_body=resp, actual_code=None,
        codec=CODEC)
    assert [o.status for o in outcomes] == [SKIP, SKIP]
    assert all("ancestor" in (o.reason or "") for o in outcomes)


def test_required_field_under_PRESENT_ancestor_still_asserts():
    """The skip is scoped to an ABSENT ancestor — a required field genuinely
    missing from a PRESENT element still fails, or the fix would mask real
    defects. Book is present but carries no isbn."""
    resp = ('<RespAvailabilityCheck xmlns="http://nlln.in/schema/v1">'
            '<Head ver="1.0"/><Resp result="SUCCESS"/>'
            '<Book title="Untagged"/></RespAvailabilityCheck>')
    outcomes = evaluate_specs(
        [_spec("occurrence", {"occurrence": "1..1"},
               "RespAvailabilityCheck/Book/@isbn")],
        request_body=None, response_body=resp, actual_code=None, codec=CODEC)
    assert outcomes[0].status == FAIL


def test_response_code_still_runs_without_any_payload():
    outcomes = evaluate_specs(
        [_spec("response_code", {"code": "00"})],
        request_body=None, response_body=None, actual_code="ZM", codec=CODEC)
    assert outcomes[0].status == FAIL


def test_unparseable_capture_skips_as_our_defect():
    outcomes = evaluate_specs(
        [_spec("occurrence", {"occurrence": "1..1"}, "ReqTransfer/Head")],
        request_body="<broken", response_body=None, actual_code="00", codec=CODEC)
    assert outcomes[0].status == SKIP
    assert "capture defect" in outcomes[0].reason


def test_assertion_failures_carries_only_failures_with_field_and_rule():
    outcomes = evaluate_specs(
        [_spec("occurrence", {"occurrence": "1..1"}, "ReqTransfer/Head"),      # PASS
         _spec("occurrence", {"occurrence": "1..1"}, "ReqTransfer/Missing"),   # FAIL
         _spec("datatype", {"datatype": "Alphanumeric"}, "ReqTransfer/Head")], # SKIP
        request_body=REQ, response_body=None, actual_code="00", codec=CODEC)
    failures = assertion_failures(outcomes)
    assert len(failures) == 1
    assert failures[0]["field"] == "ReqTransfer/Missing"
    assert failures[0]["expected"] == {"occurrence": "1..1"}


# ── round-trip: a registry-built sample passes its own constraints ───────────

_MINI_XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="http://example.org/cert"
           xmlns="http://example.org/cert" elementFormDefault="qualified">
  <xs:element name="ReqRoundTrip">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="Amt" minOccurs="1" maxOccurs="1">
          <xs:complexType>
            <xs:attribute name="value" use="required">
              <xs:simpleType>
                <xs:restriction base="xs:string">
                  <xs:minLength value="1"/><xs:maxLength value="10"/>
                </xs:restriction>
              </xs:simpleType>
            </xs:attribute>
            <xs:attribute name="curr" use="required">
              <xs:simpleType>
                <xs:restriction base="xs:string">
                  <xs:enumeration value="INR"/><xs:enumeration value="USD"/>
                </xs:restriction>
              </xs:simpleType>
            </xs:attribute>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
      <xs:attribute name="ver" use="required">
        <xs:simpleType>
          <xs:restriction base="xs:string">
            <xs:enumeration value="2.0"/>
          </xs:restriction>
        </xs:simpleType>
      </xs:attribute>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def _specs_from_message(msg) -> list[SimpleNamespace]:
    from app.services.api_registry_ingest import _flatten

    specs = []
    for field, xpath, depth, _parent in _flatten(msg.root):
        if depth == 0:
            continue
        if field.occurrence:
            specs.append(_spec("occurrence", {"occurrence": field.occurrence}, xpath))
        if field.mandatory:
            specs.append(_spec("mandatory", {"mandatory": field.mandatory}, xpath))
        if field.datatype:
            specs.append(_spec("datatype", {"datatype": field.datatype}, xpath))
        if field.length_rule:
            specs.append(_spec("length", {"length_rule": field.length_rule}, xpath))
        if field.enum_values:
            specs.append(_spec("enum", {"enum_values": field.enum_values}, xpath))
    return specs


def _failures(msg, body, *, kinds=None):
    specs = _specs_from_message(msg)
    if kinds:
        specs = [s for s in specs if s.assertion_kind in kinds]
    outcomes = evaluate_specs(specs, request_body=body, response_body=None,
                              actual_code=None, codec=CODEC)
    return [(o.field_path, o.kind, o.reason)
            for o in outcomes if o.status == FAIL]


def test_valid_instance_passes_its_own_registry_constraints(tmp_path):
    """The C-2 round-trip bar through the REAL ingest parser: a valid instance
    of a schema must pass every constraint the registry parsed from that
    schema — including the `@xmlns` pseudo-field the parser records, which the
    codec must recover from the element's namespace."""
    from app.services.api_registry_ingest import parse_xsd_dir

    (tmp_path / "roundtrip.xsd").write_text(_MINI_XSD)
    msg = next(m for m in parse_xsd_dir(tmp_path) if m.api_name == "ReqRoundTrip")
    instance = ('<network:ReqRoundTrip xmlns:network="http://example.org/cert" ver="2.0">'
                '<Amt value="100.00" curr="INR"/></network:ReqRoundTrip>')
    assert _failures(msg, instance) == []


_OPTIONAL_ATTR_XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="http://example.org/cert"
           xmlns="http://example.org/cert" elementFormDefault="qualified">
  <xs:element name="ReqOpt">
    <xs:complexType>
      <xs:attribute name="isbn" type="xs:string" use="required"/>
      <xs:attribute name="language" type="xs:string" use="optional"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def test_optional_attribute_gets_zero_min_occurrence(tmp_path):
    """The ingest fix: an `use="optional"` attribute parses to occurrence
    0..1, not 1..1 — so a message that omits it is conformant. Before this,
    every optional attribute (e.g. NLLN CR-1's additive Book/@language) was
    certified as mandatory, failing every response that legitimately left it
    out."""
    from app.services.api_registry_ingest import parse_xsd_dir

    (tmp_path / "opt.xsd").write_text(_OPTIONAL_ATTR_XSD)
    msg = next(m for m in parse_xsd_dir(tmp_path) if m.api_name == "ReqOpt")
    by_tag = {c.xml_tag: c for c in msg.root.children}
    assert by_tag["isbn"].occurrence == "1..1" and by_tag["isbn"].mandatory == "Y"
    assert by_tag["language"].occurrence == "0..1" and by_tag["language"].mandatory == "N"

    # And an instance omitting the optional attribute passes.
    instance = ('<network:ReqOpt xmlns:network="http://example.org/cert" '
                'isbn="9789389811063"/>')
    assert _failures(msg, instance, kinds={"occurrence", "mandatory"}) == []


def test_registry_skeleton_sample_passes_its_structural_constraints(tmp_path):
    """`build_sample_xml` emits a SKELETON (enum placeholders like `INR|USD`,
    empty value slots) for TSD rendering — deliberately not a valid instance.
    The structural half (occurrence/mandatory) must still hold: every declared
    node is present where the engine looks for it."""
    from app.services.api_registry_ingest import build_sample_xml, parse_xsd_dir

    (tmp_path / "roundtrip.xsd").write_text(_MINI_XSD)
    msg = next(m for m in parse_xsd_dir(tmp_path) if m.api_name == "ReqRoundTrip")
    assert _failures(msg, build_sample_xml(msg),
                     kinds={"occurrence", "mandatory"}) == []


_KB_ENV = os.environ.get("KB_XSD_DIR") or ""
_KB_DIRS = ([Path(_KB_ENV)] if _KB_ENV else []) + [
    Path("knowledge_base/existing_xsds"), Path("/knowledge_base/existing_xsds"),
    Path("../knowledge_base/existing_xsds")]
_KB = next((d for d in _KB_DIRS if d.is_dir()), None)


@pytest.mark.skipif(_KB is None, reason="XSD seed corpus not present in this image")
def test_real_corpus_samples_pass_their_structural_constraints():
    """Same structural bar against the restored 55-schema corpus: the real
    `ReqTransfer`/`ReqBalEnq` skeletons must place every mandatory node where the
    engine's path walk finds it — this is what catches a codec/registry path
    grammar mismatch."""
    from app.services.api_registry_ingest import build_sample_xml, parse_xsd_dir

    messages = parse_xsd_dir(_KB)
    subjects = [m for m in messages if m.api_name in ("ReqTransfer", "ReqBalEnq")]
    assert subjects, "corpus parsed but carried none of the expected messages"
    for msg in subjects:
        failures = _failures(msg, build_sample_xml(msg),
                             kinds={"occurrence", "mandatory"})
        assert failures == [], f"{msg.api_name}: {failures[:5]}"
