# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-1: case-set generation from the registry delta — the recorded pins.

The two deliberate deviations the first pass earned are the load-bearing tests
here: one row is one ASSERTION (not one executed case), and the field set is
the WHOLE message (not only the changed fields). Plus §3.1 fixture (a):
several rules share one execution.
"""
from __future__ import annotations

import pytest

from app.core.domain.contract import CrossFieldRule
from app.services import cert_case_builder
from app.models.api_registry import ApiField, ApiMessage
from app.models.phase_c import CertCaseSpec, CertRequestVariant

CHANGE, CFLOW = "change-77", "CFLOW-builder"


class FakePack:
    """Minimal pack: enough for wire_format_of / combination_rules_of."""

    key, version = "fake", "0"

    def __init__(self, *, wire_format="xml", rules=()):
        self._wire_format = wire_format
        self._rules = tuple(rules)

    def change_types(self): return []
    def artifacts(self): return []
    def prompt_blocks(self): return {}
    def wire_format(self): return self._wire_format
    def combination_rules(self): return self._rules


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register models so metadata is complete
    from app.core.database import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        ApiMessage.__table__, ApiField.__table__,
        CertRequestVariant.__table__, CertCaseSpec.__table__,
    ])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _message(db, api_name="ReqTransfer", *, change_id=CHANGE, fields=()):
    msg = ApiMessage(api_name=api_name, direction="request",
                     introduced_by_change_id=change_id)
    db.add(msg)
    db.flush()
    for i, spec in enumerate(fields):
        db.add(ApiField(message_id=msg.id, position=i,
                        xml_tag=spec.get("tag", f"F{i}"),
                        xpath=spec.get("xpath", f"{api_name}/F{i}"),
                        **{k: v for k, v in spec.items()
                           if k not in ("tag", "xpath")}))
    db.commit()
    return msg


def _case(case_id="TC001", api="ReqTransfer", initiator="npci", *,
          expected_status="Success", rc="00", extra=None):
    authority_batch = {"type": "PAY", "expected_rc": rc}
    authority_batch.update(extra or {})
    return {"case_id": case_id, "api": api, "initiator": initiator,
            "expected_status": expected_status, "authority_batch": authority_batch}


def _build(db, cases, *, pack=None, run=1, change_id=CHANGE):
    return cert_case_builder.build(
        db, change_id=change_id, cflow_id=CFLOW, run_number=run,
        available_cases=cases, pack=pack or FakePack())


# ── one row = one assertion; whole message; one execution ────────────────────

def test_one_assertion_per_constraint(db_session):
    _message(db_session, fields=[
        {"xpath": "ReqTransfer/Amt", "occurrence": "1..1", "datatype": "Numeric",
         "length_rule": "Max Length 10"},
    ])
    result = _build(db_session, [_case()])
    kinds = sorted(s.assertion_kind for s in result.specs)
    assert kinds == ["datatype", "length", "occurrence", "response_code"]


def test_ten_constrained_fields_produce_one_executed_variant(db_session):
    _message(db_session, fields=[
        {"xpath": f"ReqTransfer/F{i}", "occurrence": "1..1"} for i in range(10)
    ])
    result = _build(db_session, [_case()])
    assert len(result.variants) == 1, \
        "a message with many constrained fields must not become many transactions"
    assert len(result.specs) == 11  # 10 occurrence + 1 response_code


def test_several_rules_share_one_execution(db_session):
    """§3.1 fixture (a): all of a variant's assertion rows reference the SAME
    execution."""
    _message(db_session, fields=[
        {"xpath": "ReqTransfer/A", "occurrence": "1..1"},
        {"xpath": "ReqTransfer/B", "datatype": "Numeric"},
        {"xpath": "ReqTransfer/C", "mandatory": "Y"},
    ])
    result = _build(db_session, [_case()])
    variant_refs = {s.variant_id for s in result.specs}
    assert len(variant_refs) == 1
    assert len(result.specs) == 4


def test_whole_message_is_asserted_not_only_changed_fields(db_session):
    """Deviation (2), found by a test the first time: the changed field
    selects the API; the assertions cover every constrained field."""
    msg = _message(db_session, fields=[
        {"xpath": "ReqTransfer/Old", "occurrence": "1..1"},        # pre-existing
        {"xpath": "ReqTransfer/New", "occurrence": "0..1"},        # the change
    ])
    old, new = msg.fields
    old.introduced_by_change_id = None
    new.introduced_by_change_id = CHANGE
    db_session.commit()

    result = _build(db_session, [_case()])
    asserted_paths = {s.field_path for s in result.specs if s.field_path}
    assert asserted_paths == {"ReqTransfer/Old", "ReqTransfer/New"}


# ── scope selection ──────────────────────────────────────────────────────────

def test_untouched_api_is_not_certified(db_session):
    _message(db_session, "ReqTransfer")
    _message(db_session, "ReqMandate", change_id=None)   # untouched
    result = _build(db_session, [_case("TC001", "ReqTransfer"),
                                 _case("TC009", "ReqMandate")])
    assert {v.case_id for v in result.variants} == {"TC001"}


def test_api_name_match_is_case_insensitive(db_session):
    _message(db_session, "ReqTransfer")
    result = _build(db_session, [_case(api="reqtransfer")])
    assert len(result.variants) == 1


def test_field_level_delta_selects_the_message(db_session):
    msg = _message(db_session, change_id=None,
                   fields=[{"xpath": "ReqTransfer/New", "occurrence": "1..1"}])
    msg.fields[0].introduced_by_change_id = CHANGE
    db_session.commit()
    result = _build(db_session, [_case()])
    assert len(result.variants) == 1


def test_uncoverable_api_is_reported_and_leads_the_summary(db_session):
    _message(db_session, "ReqNewThing")
    result = _build(db_session, [_case(api="ReqTransfer")])   # no case for ReqNewThing
    assert result.uncovered_apis == ["ReqNewThing"]
    assert result.summary().startswith("NOT covered")


def test_changed_field_with_no_assertable_constraint_is_reported(db_session):
    _message(db_session, fields=[
        {"xpath": "ReqTransfer/Free", "introduced_by_change_id": CHANGE}])  # no constraints
    result = _build(db_session, [_case()])
    assert result.unconstrained_fields == ["ReqTransfer/Free"]


def test_empty_delta_falls_back_and_is_labelled(db_session):
    result = _build(db_session, [_case()], change_id="change-without-delta")
    assert result.fallback is True
    assert all(s.origin == "harness_baseline" for s in result.specs)
    assert "harness_baseline" in result.summary()
    assert len(result.variants) == 1     # baseline still executes the case


# ── determinism + replacement ────────────────────────────────────────────────

def test_generation_is_deterministic(db_session):
    _message(db_session, fields=[{"xpath": "ReqTransfer/A", "occurrence": "1..1"}])
    first = _build(db_session, [_case()])
    second = _build(db_session, [_case()])
    assert [v.variant_id for v in first.variants] == \
           [v.variant_id for v in second.variants]
    key = lambda s: (s.case_id, s.assertion_kind, s.field_path)  # noqa: E731
    assert sorted(map(key, first.specs)) == sorted(map(key, second.specs))


def test_redispatch_replaces_not_accumulates(db_session):
    _message(db_session, fields=[{"xpath": "ReqTransfer/A", "occurrence": "1..1"}])
    cert_case_builder.store(db_session, _build(db_session, [_case()]))
    cert_case_builder.store(db_session, _build(db_session, [_case()]))
    assert db_session.query(CertRequestVariant).count() == 1
    assert db_session.query(CertCaseSpec).count() == 2


def test_a_new_round_keeps_its_own_snapshot(db_session):
    _message(db_session, fields=[{"xpath": "ReqTransfer/A", "occurrence": "1..1"}])
    cert_case_builder.store(db_session, _build(db_session, [_case()], run=1))
    cert_case_builder.store(db_session, _build(db_session, [_case()], run=2))
    assert db_session.query(CertRequestVariant).count() == 2


def test_expected_survives_a_mid_flight_registry_edit(db_session):
    msg = _message(db_session, fields=[
        {"xpath": "ReqTransfer/Amt", "length_rule": "Max Length 10"}])
    cert_case_builder.store(db_session, _build(db_session, [_case()]))

    msg.fields[0].length_rule = "Max Length 99"          # mid-cert edit
    db_session.commit()

    stored = db_session.query(CertCaseSpec).filter_by(assertion_kind="length").one()
    assert stored.expected == {"length_rule": "Max Length 10"}, \
        "a registry edit must not retroactively change what the round asserted"


def test_conditional_mandatory_carries_its_condition(db_session):
    _message(db_session, fields=[
        {"xpath": "ReqTransfer/Pin", "mandatory": "C",
         "condition_text": "Required when type is PAY"}])
    result = _build(db_session, [_case()])
    spec = next(s for s in result.specs if s.assertion_kind == "mandatory")
    assert spec.expected == {"mandatory": "C",
                             "condition_text": "Required when type is PAY"}


# ── neutral vocabulary + snapshots ───────────────────────────────────────────

def test_initiator_is_stored_as_authority_or_partner(db_session):
    _message(db_session)
    result = _build(db_session, [_case("TC001", initiator="npci"),
                                 _case("TC002", initiator="bank")])
    by_case = {v.case_id: v.initiator for v in result.variants}
    assert by_case == {"TC001": "authority", "TC002": "partner"}


def test_wire_format_is_snapshotted_from_the_pack(db_session):
    _message(db_session)
    result = _build(db_session, [_case()],
                    pack=FakePack(wire_format="fake-fmt"))
    assert all(v.wire_format == "fake-fmt" for v in result.variants)
    assert all(s.wire_format == "fake-fmt" for s in result.specs)


def test_authority_data_rides_once_per_variant(db_session):
    """§3.1: input payloads must not be copied into every assertion row."""
    _message(db_session, fields=[
        {"xpath": "ReqTransfer/A", "occurrence": "1..1"},
        {"xpath": "ReqTransfer/B", "occurrence": "1..1"},
    ])
    result = _build(db_session, [_case()])
    carrying = [s for s in result.specs if s.authority_data]
    assert len(carrying) == 1
    assert carrying[0].assertion_kind == "response_code"


def test_pack_rules_drive_variant_generation(db_session):
    _message(db_session)
    rule = CrossFieldRule(
        api_name="ReqTransfer", kind="valid_tuple", fields=["type"],
        values={"tuples": [{"type": "PAY"}, {"type": "COLLECT"}]})
    result = _build(db_session, [_case()], pack=FakePack(rules=[rule]))
    assert len(result.variants) > 1
    assert {v.input_data.get("type") for v in result.variants} == {"PAY", "COLLECT"}
