# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SIM-3 + SIM-4: ingress validation, scenario tiers, the execute edge.

Two claims carry the weight: validation IS `cert_assertions` (same code both
sides, so simulator and grader fail a bank for the same documented reason),
and a request naming a variant hits that variant's declared scenario even
when a broader predicate sits earlier in the merged list (Gate 2's
variant-binding claim).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.wire.registry import codec_for
from app.services.sim_packs.contract import SimPack, stamp
from app.services.simulator import scenario, store, validation

CODEC = codec_for("xml")

FIELDS = [
    {"path": "ReqTransfer/Head/@ver", "mandatory": "Y", "occurrence": "1..1"},
    {"path": "ReqTransfer/Txn/@type", "enum_values": ["PAY", "COLLECT"]},
    {"path": "ReqTransfer/Txn/note", "mandatory": "N", "datatype": "Numeric"},
]
GOOD_BODY = '<ReqTransfer><Head ver="2.0"/><Txn type="PAY"/></ReqTransfer>'


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.sim_pack import SimPackPublication, SimPackRecord

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[SimPackRecord.__table__,
                                             SimPackPublication.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _entry(**over) -> dict:
    return {"api": "ReqTransfer", "wire_format": "xml", "fields": FIELDS, **over}


# ── SIM-3: validation is the grader's own code ───────────────────────────────

def test_a_violation_names_the_path_and_the_constraint():
    violations = validation.validate_request(
        _entry(), '<ReqTransfer><Txn type="REFUND"/></ReqTransfer>', codec=CODEC)
    kinds = {(v["field"], v["kind"]) for v in violations}
    # The genuine violation: Txn/@type="REFUND" outside the enum.
    assert ("ReqTransfer/Txn/@type", "enum") in kinds
    # Head/@ver is NOT flagged: its containing element `ReqTransfer/Head` is absent,
    # and a required attribute is only owed when its element is present (the
    # element's own occurrence governs the absence). This FIXTURE declares no
    # `ReqTransfer/Head` element row; the real ingest always emits one, so a missing
    # Head would itself be flagged there.
    assert ("ReqTransfer/Head/@ver", "occurrence") not in kinds


def test_a_conforming_body_passes():
    assert validation.validate_request(_entry(), GOOD_BODY, codec=CODEC) == []


def test_a_skip_never_rejects():
    """Optional-and-absent `note` SKIPs its datatype assertion — the same
    honest non-answer the grading side gives."""
    assert validation.validate_request(_entry(), GOOD_BODY, codec=CODEC) == []


def test_the_engine_is_literally_cert_assertions():
    import inspect

    src = inspect.getsource(validation)
    assert "from app.services.cert_assertions import" in src, \
        "one implementation of the six kinds, not two sides of a contract"


def test_a_template_passes_its_own_field_table(db_session):
    """If the pack cannot validate the template it ships, one of the two is
    wrong — the S-3 round-trip rule."""
    entry = _entry(request_template=GOOD_BODY)
    assert validation.validate_request(entry, entry["request_template"],
                                       codec=CODEC) == []


# ── SIM-4: scenario tiers ────────────────────────────────────────────────────

SCENARIOS = [
    {"when": {"field": "ReqTransfer/Txn/@type", "eq": "PAY"}, "respond": {"rc": "ZF"}},
    {"when": {"tc_id": "PR_1"}, "respond": {"rc": "ZT"}},
    {"when": {"variant_id": "v-1"}, "respond": {"rc": "ZM"}},
]


def test_a_variant_hits_its_declared_scenario_past_a_broader_predicate():
    doc = CODEC.parse(GOOD_BODY)
    chosen = scenario.choose(SCENARIOS, variant_id="v-1", tc_id="PR_1",
                             doc=doc, codec=CODEC)
    assert chosen["respond"]["rc"] == "ZM", \
        "variant identity outranks list order — Gate 2's variant-binding claim"


def test_tc_tier_then_field_predicate_then_none():
    doc = CODEC.parse(GOOD_BODY)
    assert scenario.choose(SCENARIOS, tc_id="PR_1", doc=doc,
                           codec=CODEC)["respond"]["rc"] == "ZT"
    assert scenario.choose(SCENARIOS, doc=doc, codec=CODEC)["respond"]["rc"] == "ZF"
    assert scenario.choose(SCENARIOS[1:], doc=CODEC.parse(
        '<ReqTransfer><Txn type="COLLECT"/></ReqTransfer>'), codec=CODEC) is None


# ── the execute edge ─────────────────────────────────────────────────────────

class _Req:
    def __init__(self, body: str, token: str | None = None):
        self._body = body.encode()
        self.headers = {"X-Internal-Token": token} if token else {}

    async def body(self) -> bytes:
        return self._body


_AMBIENT = object()


def _call(db, body=GOOD_BODY, *, token=_AMBIENT, **kw):
    """Call the execute edge, satisfying the internal-token guard by default.

    These tests are about pack resolution, scenarios and validation. Sending no
    token made every one of them 401 short-circuit on any host whose .env sets
    CERT_AGENT_INTERNAL_TOKEN, so they silently stopped asserting what they
    claim to — a test that flips on the developer's environment is not a test.

    Pass `token=` explicitly (including `token=None`) to exercise the guard
    itself; see test_internal_token_gates_when_configured.
    """
    from app.api.sim_execute import execute
    from app.core.config import settings

    if token is _AMBIENT:
        token = settings.cert_agent_internal_token or None
    return asyncio.run(execute(_Req(body, token), db, **kw))


def _publish_baseline(db, scenarios=SCENARIOS):
    pack = stamp(SimPack.model_validate({
        "pack_ref": "baseline@1", "base_pack": "baseline@1",
        "apis": [_entry(response_template='<RespTransfer rc="{{rc}}"/>')],
        "scenarios": scenarios,
    }))
    store.save_draft(db, pack)
    return store.publish(db, "baseline@1")


def test_unknown_pack_is_a_400_never_a_fallback(db_session):
    from fastapi import HTTPException

    _publish_baseline(db_session)
    with pytest.raises(HTTPException) as exc:
        _call(db_session, pack="CHG-9999@1")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "unknown_pack"


def test_no_baseline_is_the_stated_pre_pack_default(db_session):
    resp = _call(db_session)
    assert resp.headers["X-Sim-Pack"] == "none"
    assert b'rc="00"' in resp.body


def test_every_response_names_the_contract_that_produced_it(db_session):
    row = _publish_baseline(db_session)
    resp = _call(db_session, variant_id="v-1")
    assert resp.headers["X-Sim-Pack"] == f"baseline@1 {row.pack_id}"
    assert resp.headers["X-Sim-Scenario"] == "variant_id:v-1"
    assert resp.body == b'<RespTransfer rc="ZM"/>', "rendered from the pack template"


def test_no_scenario_match_is_a_stated_default(db_session):
    _publish_baseline(db_session, scenarios=[])
    resp = _call(db_session)
    assert resp.headers["X-Sim-Scenario"] == "default"
    assert resp.body == b'<RespTransfer rc="00"/>'


def test_a_violating_body_is_rejected_with_the_violations(db_session):
    from fastapi import HTTPException

    _publish_baseline(db_session)
    with pytest.raises(HTTPException) as exc:
        _call(db_session, body='<ReqTransfer><Txn type="REFUND"/></ReqTransfer>')
    assert exc.value.status_code == 422
    # The body's genuine violation: Txn/@type="REFUND" is not in the enum.
    # (It also omits Head/@ver, but @ver is skipped because its containing
    # element `ReqTransfer/Head` is absent — a required attribute is only required
    # WHEN its element is present, so the element's own occurrence governs the
    # absence, not the attribute's. This FIXTURE has no `ReqTransfer/Head` element
    # row, unlike the real ingest which always emits a row for every container;
    # there the missing Head would itself be flagged. Assert on the violation
    # the body actually commits.)
    assert any(v["field"] == "ReqTransfer/Txn/@type" and v["kind"] == "enum"
               for v in exc.value.detail["violations"])


def test_an_api_the_pack_does_not_speak_for_is_refused(db_session):
    from fastapi import HTTPException

    _publish_baseline(db_session)
    with pytest.raises(HTTPException) as exc:
        _call(db_session, body="<ReqSomethingElse/>")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "unknown_api"


def test_no_response_scenario_answers_504(db_session):
    from fastapi import HTTPException

    _publish_baseline(db_session, scenarios=[
        {"when": {"tc_id": "PR_9"}, "respond": {"rc": "ZZ", "no_response": True}}])
    with pytest.raises(HTTPException) as exc:
        _call(db_session, tc_id="PR_9")
    assert exc.value.status_code == 504
    assert exc.value.detail["error"] == "scenario_no_response"


def test_internal_token_gates_when_configured(db_session, monkeypatch):
    from fastapi import HTTPException

    from app.core.config import settings

    monkeypatch.setattr(settings, "cert_agent_internal_token", "sekrit")
    _publish_baseline(db_session)
    with pytest.raises(HTTPException) as exc:
        _call(db_session, token=None)  # explicit: send NO token, and be rejected
    assert exc.value.status_code == 401
    resp = asyncio.run(__import__("app.api.sim_execute", fromlist=["execute"])
                       .execute(_Req(GOOD_BODY, token="sekrit"), db_session))
    assert resp.status_code == 200


# ── the baseline builder (SIM-4's second half) ───────────────────────────────

def test_baseline_refuses_an_empty_registry(db_session):
    from sqlalchemy import inspect as sa_inspect

    from app.core.database import Base
    from app.models.api_registry import ApiField, ApiMessage
    from app.services.sim_packs.builder import build_baseline_pack

    Base.metadata.create_all(db_session.get_bind(),
                             tables=[ApiMessage.__table__, ApiField.__table__])
    with pytest.raises(ValueError, match="seeding failure"):
        build_baseline_pack(db_session, pack_ref="baseline@1", pack=object())


def test_baseline_is_the_whole_registry_plus_catalogue_scenarios(db_session):
    from app.core.database import Base
    from app.models.api_registry import ApiField, ApiMessage
    from app.models.base import generate_uuid
    from app.services.sim_packs.builder import build_baseline_pack

    Base.metadata.create_all(db_session.get_bind(),
                             tables=[ApiMessage.__table__, ApiField.__table__])
    db_session.add(ApiMessage(id=generate_uuid(), api_name="ReqTransfer",
                              direction="request", sample_xml="<ReqTransfer/>"))
    db_session.commit()

    pack = build_baseline_pack(
        db_session, pack_ref="baseline@1", pack=object(),
        available_cases=[
            {"case_id": "PR_1", "authority_batch": {"expected_rc": "00"}},
            {"case_id": "PR_2", "authority_batch": {}},
        ])
    assert pack.base_pack == pack.pack_ref, "a root declares itself its base"
    assert [a.api for a in pack.apis] == ["ReqTransfer"]
    assert [(s.when.tc_id, s.respond.rc) for s in pack.scenarios] == [("PR_1", "00")]
    assert any("PR_2" in g for g in pack.provenance.coverage.gaps), \
        "a case with no expected rc is a gap, not a guessed 00"

    # And it stores + publishes + resolves as the absent-?pack= default.
    from app.services.simulator import resolver
    store.save_draft(db_session, pack)
    store.publish(db_session, "baseline@1")
    assert resolver.resolve_request(db_session, None).pack_ref == "baseline@1"


def test_an_unconfigured_case_defaults_loudly(db_session, caplog):
    """Plan S-4: losing this warning trades a loud gap for a silent pass —
    a case nobody configured answers 00, indistinguishable from a real pass."""
    import logging

    _publish_baseline(db_session, scenarios=[])
    with caplog.at_level(logging.WARNING):
        resp = _call(db_session, tc_id="PR_UNCONFIGURED")
    assert resp.headers["X-Sim-Scenario"] == "default"
    assert any("respcode defaulted" in r.getMessage() for r in caplog.records)
