# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""S-1: pack builder from the registry delta.

THE pin here: an empty delta produces NO pack at all — an empty pack
published over a baseline is indistinguishable from "nothing changed" and
hides a broken delta. Everything else guards determinism (same rows in →
identical pack_id), the one-delta rule, and honest gap counting.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.sim_packs import builder

CHANGE = "chg-42"


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.api_registry import ApiField, ApiMessage

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[ApiMessage.__table__, ApiField.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed(db, api="ReqDispute", direction="request", change_id=CHANGE,
          field_change_id=None):
    from app.models.base import generate_uuid
    from app.models.api_registry import ApiField, ApiMessage

    msg = ApiMessage(id=generate_uuid(), api_name=api, direction=direction,
                     sample_xml=f"<{api}><Txn type=\"CHARGEBACK\"/></{api}>",
                     introduced_by_change_id=change_id)
    db.add(msg)
    db.flush()
    db.add_all([
        ApiField(id=generate_uuid(), message_id=msg.id, position=1,
                 xml_tag="type", is_attribute=True, xpath=f"{api}/Txn/@type",
                 occurrence="1..1", datatype="string", mandatory="Y",
                 enum_values=["CHARGEBACK", "PRE_ARB"],
                 introduced_by_change_id=field_change_id),
        ApiField(id=generate_uuid(), message_id=msg.id, position=2,
                 xml_tag="note", xpath=f"{api}/Txn/note",
                 introduced_by_change_id=field_change_id),  # no constraints
    ])
    db.commit()
    return msg


def _variant(vid, case_id="TC1", rc="ZA"):
    return SimpleNamespace(variant_id=vid, case_id=case_id,
                           expected={"result": "PASS", "code": rc})


def _build(db, **kw):
    kw.setdefault("change_id", CHANGE)
    kw.setdefault("pack_ref", "chg-42@1")
    kw.setdefault("base_pack_ref", "baseline@2026-08")
    kw.setdefault("pack", object())     # wire_format_of falls through to "xml"
    return builder.build_pack(db, **kw)


# ── THE pin ──────────────────────────────────────────────────────────────────

def test_an_empty_delta_builds_no_pack_at_all(db_session):
    _seed(db_session, change_id="some-other-change")
    assert _build(db_session) is None


def test_the_delta_is_the_same_function_the_grader_uses():
    from app.services import cert_case_builder

    assert builder.delta_messages is cert_case_builder.delta_messages, \
        "two implementations of 'what did this change touch' is the drift " \
        "this design exists to prevent"


# ── projection ───────────────────────────────────────────────────────────────

def test_known_delta_projects_apis_and_the_whole_field_table(db_session):
    _seed(db_session)
    pack = _build(db_session, routes={"reqdispute": {"path": "/execute",
                                                     "flow_code": "DISPUTE"}})
    assert [a.api for a in pack.apis] == ["ReqDispute"]
    api = pack.apis[0]
    assert api.route.path == "/execute" and api.route.flow_code == "DISPUTE"
    assert api.wire_format == "xml"
    assert api.request_template and api.response_template is None
    assert [f.path for f in api.fields] == \
        ["ReqDispute/Txn/@type", "ReqDispute/Txn/note"], \
        "the WHOLE message ships, not only constrained fields"
    assert api.fields[0].enum_values == ["CHARGEBACK", "PRE_ARB"]
    assert pack.base_pack == "baseline@2026-08" and pack.change_id == CHANGE


def test_a_message_touched_only_via_a_field_is_in_the_pack(db_session):
    _seed(db_session, change_id=None, field_change_id=CHANGE)
    pack = _build(db_session)
    assert [a.api for a in pack.apis] == ["ReqDispute"]


def test_response_direction_fills_the_response_template(db_session):
    _seed(db_session, api="RespDispute", direction="response")
    api = _build(db_session).apis[0]
    assert api.response_template and api.request_template is None


def test_variants_become_scenarios_and_a_missing_rc_is_a_gap(db_session):
    _seed(db_session)
    pack = _build(db_session, variants=[
        _variant("v-b", rc="ZM"), _variant("v-a", rc="ZA"),
        SimpleNamespace(variant_id="v-c", case_id="TC2", expected={}),
    ])
    assert [(s.when.variant_id, s.respond.rc) for s in pack.scenarios] == \
        [("v-a", "ZA"), ("v-b", "ZM")], "ordered; no guessed response for v-c"
    assert any("v-c" in g for g in pack.provenance.coverage.gaps)


def test_a_missing_route_is_counted_never_invented(db_session):
    _seed(db_session)
    pack = _build(db_session)                       # no routes passed
    assert pack.apis[0].route is None
    assert any("no route declared for ReqDispute" in g
               for g in pack.provenance.coverage.gaps)


def test_coverage_counts_are_honest(db_session):
    _seed(db_session)
    cov = _build(db_session).provenance.coverage
    assert (cov.apis, cov.fields_total, cov.fields_with_constraints) == (1, 2, 1)


# ── determinism ──────────────────────────────────────────────────────────────

def test_build_twice_is_the_same_pack_id(db_session):
    _seed(db_session)
    variants = [_variant("v-1")]
    one = _build(db_session, variants=variants, generated_at="2026-08-31T00:00:00+00:00")
    two = _build(db_session, variants=variants, generated_at="2031-01-01T09:09:09+00:00")
    assert one.pack_id == two.pack_id, \
        "generated_at lives in provenance, OUTSIDE the hash"
    assert one.pack_id.startswith("sha256:")

    three = _build(db_session, variants=[_variant("v-1", rc="ZM")])
    assert three.pack_id != one.pack_id, "behaviour change moves the address"
