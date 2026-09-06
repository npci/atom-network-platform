# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SIM-6: the sim_pack harness — a full round, in-process, fully recorded.

Gate 3's recording claim is the headline: every result row says which case
AND variant ran, which mode each side executed in, and which pack graded it.
The grader's independence matters too: it grades the OBSERVED exchange, so a
deviating answer fails the round no matter what the pack intended.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import cert_pack_run
from app.services.sim_packs.builder import build_baseline_pack
from app.services.simulator import store

CHG, PARTNER = "chg-77", "partner-1"

CATALOGUE = [{"case_id": "TC1", "api": "ReqDispute", "initiator": "npci",
              "expected_status": "PASS", "authority_batch": {"expected_rc": "00"}}]


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.api_registry import ApiField, ApiMessage
    from app.models.phase_c import (
        CertCaseSpec, CertRequestVariant, CertRun, CertTestResult, PartnerAgent)
    from app.models.sim_pack import SimPackPublication, SimPackRecord

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        ApiMessage.__table__, ApiField.__table__, CertRun.__table__,
        CertTestResult.__table__, CertRequestVariant.__table__,
        CertCaseSpec.__table__, SimPackRecord.__table__,
        SimPackPublication.__table__, PartnerAgent.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed_registry(db, sample='<ReqDispute><Txn type="CHARGEBACK"/></ReqDispute>'):
    from app.models.api_registry import ApiField, ApiMessage
    from app.models.base import generate_uuid

    msg = ApiMessage(id=generate_uuid(), api_name="ReqDispute",
                     direction="request", sample_xml=sample,
                     introduced_by_change_id=CHG)
    db.add(msg)
    db.flush()
    db.add(ApiField(id=generate_uuid(), message_id=msg.id, position=1,
                    xml_tag="type", is_attribute=True,
                    xpath="ReqDispute/Txn/@type", occurrence="1..1",
                    mandatory="Y", enum_values=["CHARGEBACK", "PRE_ARB"],
                    introduced_by_change_id=CHG))
    db.commit()


def _publish_baseline(db):
    store.save_draft(db, build_baseline_pack(db, pack_ref="baseline@1",
                                             pack=object()))
    return store.publish(db, "baseline@1")


def _run(db, **kw):
    kw.setdefault("change_id", CHG)
    kw.setdefault("partner_id", PARTNER)
    kw.setdefault("test_data", {"case_catalogue": CATALOGUE})
    return asyncio.run(cert_pack_run.run_round(db, **kw))


def test_a_clean_round_certifies_and_records_everything(db_session):
    from app.models.phase_c import CertRun, CertTestResult

    _seed_registry(db_session)
    _publish_baseline(db_session)
    summary = _run(db_session, dispatch_meta={"dispatched_by": "operator"})

    assert summary["passed"] is True and summary["status"] == "certified"
    assert summary["pack_ref"] == f"{CHG}@r1"

    run = db_session.query(CertRun).one()
    assert (run.pack_ref, run.npci_mode, run.partner_mode) == \
        (f"{CHG}@r1", "simulator", "simulator")
    assert run.pack_id.startswith("sha256:")
    assert run.dispatched_by == "operator"
    assert run.coverage["specs"] > 0, "the coverage note is stamped"

    for row in db_session.query(CertTestResult).all():
        assert row.test_case_id == "TC1"
        assert row.actual_response["variant_id"], "which VARIANT ran"
        assert row.pack_ref == f"{CHG}@r1" and row.pack_id == run.pack_id, \
            "every result records which contract graded it — Gate 3"
        assert (row.npci_mode, row.partner_mode) == ("simulator", "simulator")


def test_no_published_baseline_refuses_and_persists_nothing(db_session):
    from app.models.phase_c import CertRun

    _seed_registry(db_session)
    out = _run(db_session)
    assert out["error"] == "no_baseline"
    assert db_session.query(CertRun).count() == 0


def test_an_empty_delta_with_no_catalogue_is_an_empty_round(db_session):
    """Registry seeded by OTHER changes only: this change's delta is empty,
    the demo catalogue derives nothing, and no catalogue was supplied — the
    round refuses rather than certifying nothing."""
    from app.models.api_registry import ApiMessage
    from app.models.base import generate_uuid
    from app.models.phase_c import CertRun

    db_session.add(ApiMessage(id=generate_uuid(), api_name="ReqOther",
                              direction="request", sample_xml="<ReqOther/>",
                              introduced_by_change_id="someone-else"))
    db_session.commit()
    _publish_baseline(db_session)

    out = _run(db_session, test_data={})
    assert out["error"] == "empty_scope"
    assert db_session.query(CertRun).count() == 0


def test_the_grader_is_independent_of_the_simulators_answer(db_session, monkeypatch):
    """A deviating rc FAILs the case even though the pack's own scenario
    declares agreement — the harness grades what it OBSERVED."""
    from app.models.phase_c import CertTestResult
    from app.services.simulator.runtime import SimReply

    _seed_registry(db_session)
    _publish_baseline(db_session)

    async def _deviating(db, *, body, pack, tc_id=None, variant_id=None):
        return SimReply(rc="ZZ", content='<Response rc="ZZ"/>',
                        media_type="application/xml",
                        pack_header="x", scenario="stub")
    monkeypatch.setattr(cert_pack_run.runtime, "handle", _deviating)

    summary = _run(db_session)
    assert summary["passed"] is False and summary["fail"] >= 1
    row = db_session.query(CertTestResult).first()
    assert any(f["kind"] == "response_code"
               for f in row.actual_response["assertion_failures"])


def test_a_missing_template_grades_error_not_fail(db_session):
    """Nothing to execute is THIS side's defect — never the partner's FAIL."""
    from app.models.phase_c import CertTestResult, CertTestStatus

    _seed_registry(db_session, sample=None)
    _publish_baseline(db_session)
    summary = _run(db_session)
    assert summary["passed"] is False and summary["error"] >= 1
    row = db_session.query(CertTestResult).first()
    assert row.status == CertTestStatus.ERROR
    assert "not the partner's failure" in row.actual_response["error"]


def test_round_two_reuses_the_cflow_and_advances_the_round(db_session):
    from app.models.phase_c import CertRun

    _seed_registry(db_session)
    _publish_baseline(db_session)
    one = _run(db_session)
    two = _run(db_session)
    assert two["cflow_id"] == one["cflow_id"]
    assert [r.run_number for r in db_session.query(CertRun)
            .order_by(CertRun.run_number)] == [1, 2]
    assert two["pack_ref"] == f"{CHG}@r2"


def test_the_harness_axis_is_a_declared_setting(monkeypatch):
    from app.core.config import settings
    from app.packs.network.certification import default_harness

    # Assert the DECLARED default off the field, not off the live settings
    # object -- the latter carries whatever this host's .env selected, so on a
    # sim_pack-configured host this was asserting the operator's choice (F-1).
    assert type(settings).model_fields["cert_harness"].default == "", \
        "declared field, empty default"
    monkeypatch.setattr(settings, "cert_harness", "", raising=False)
    assert default_harness().key == "cert_agent", "legacy selection preserved"
    monkeypatch.setattr(settings, "cert_harness", "sim_pack")
    assert default_harness().key == "sim_pack"


# ── Gate 3: each side executes only its own class ────────────────────────────

def _two_sided_catalogue():
    return [
        {"case_id": "TC1", "api": "ReqDispute", "initiator": "npci",
         "expected_status": "PASS", "authority_batch": {"expected_rc": "00"}},
        {"case_id": "TC2", "api": "ReqDispute", "initiator": "bank",
         "expected_status": "PASS", "authority_batch": {"expected_rc": "00"}},
    ]


def _enable_tunnel(db, monkeypatch, *, onboard=True, sent=None):
    from app.core.config import settings
    from app.models.phase_c import PartnerAgent

    monkeypatch.setattr(settings, "integration_testing_enabled", True)
    if onboard:
        db.add(PartnerAgent(id=PARTNER, name="Bank One"))
        db.commit()

    async def _send(**kw):
        if sent is not None:
            sent.append(kw)
        return SimpleNamespace(id="msg-1")

    monkeypatch.setattr("app.services.a2a_client.send_task_to_partner", _send)


def test_the_partners_class_is_announced_and_awaited(db_session, monkeypatch):
    from app.models.phase_c import CertRun, CertRunStatus, CertTestResult

    _seed_registry(db_session)
    _publish_baseline(db_session)
    sent = []
    _enable_tunnel(db_session, monkeypatch, sent=sent)

    summary = _run(db_session, test_data={"case_catalogue": _two_sided_catalogue()})

    assert summary["status"] == "awaiting_partner"
    assert summary["passed"] is None, "no verdict until the join reaches one"
    assert summary["awaiting_partner_cases"] == ["TC2"]

    run = db_session.query(CertRun).one()
    assert run.status is CertRunStatus.RUNNING and run.completed_at is None

    rows = {r.test_case_id: r for r in db_session.query(CertTestResult)}
    assert rows["TC1"].actual_response.get("rc") == "00", "this side ran TC1"
    assert rows["TC2"].actual_response["not_reported"] is True
    assert rows["TC2"].actual_response["variant_id"], "per VARIANT, not per API"

    # The announcement carries the alias and the pack that grades the round.
    payload = sent[0]["payload"]
    assert payload["simulator"]["endpoint"].startswith("a2a://cert_simulator?pack=")
    assert "chg-77%40r1" in payload["simulator"]["endpoint"], "percent-encoded"
    assert payload["cert_context"]["npci_mode"] == "simulator"


def test_application_mode_publishes_the_application_alias(db_session, monkeypatch):
    _seed_registry(db_session)
    _publish_baseline(db_session)
    sent = []
    _enable_tunnel(db_session, monkeypatch, sent=sent)

    _run(db_session, test_data={"case_catalogue": _two_sided_catalogue(),
                                "modes": {"partner_mode": "application"}})
    payload = sent[0]["payload"]
    assert payload["cert_context"]["partner_mode"] == "application"
    assert payload["simulator"]["endpoint"].startswith("a2a://cert_simulator"), \
        "the NPCI side is still a simulator — the sides are independent"


def test_the_join_owns_an_awaiting_run(db_session, monkeypatch):
    """cert_join is harness-agnostic: the row markers alone hand it the run."""
    from app.models.phase_c import CertRun
    from app.services import cert_join

    _seed_registry(db_session)
    _publish_baseline(db_session)
    _enable_tunnel(db_session, monkeypatch)
    _run(db_session, test_data={"case_catalogue": _two_sided_catalogue()})

    run = db_session.query(CertRun).one()
    assert cert_join._join_managed(run) is True
    assert cert_join.pending_case_ids(run) == ["TC2"]


def test_an_unonboarded_partner_runs_this_sides_class_only(db_session, monkeypatch):
    """A failed announcement must not strand the round awaiting reports that
    can never arrive."""
    from app.models.phase_c import CertRun, CertRunStatus

    _seed_registry(db_session)
    _publish_baseline(db_session)
    _enable_tunnel(db_session, monkeypatch, onboard=False)

    summary = _run(db_session, test_data={"case_catalogue": _two_sided_catalogue()})
    assert summary["status"] != "awaiting_partner"
    assert db_session.query(CertRun).one().status is CertRunStatus.COMPLETED


def test_with_the_tunnel_off_the_whole_scope_is_ours(db_session):
    from app.models.phase_c import CertRunStatus, CertRun

    _seed_registry(db_session)
    _publish_baseline(db_session)
    summary = _run(db_session, test_data={"case_catalogue": _two_sided_catalogue()})
    assert summary["status"] in ("certified", "failed")
    assert db_session.query(CertRun).one().status is CertRunStatus.COMPLETED


# ── variants are MATERIALISED, not merely identified ─────────────────────────

def test_two_variants_of_a_case_send_different_bytes(db_session, monkeypatch):
    """Without rendering, the §3.1 variant axis is decorative: every variant
    certifies the identical request while the report claims they differed."""
    from app.models.base import generate_uuid
    from app.models.phase_c import CertRequestVariant

    _seed_registry(db_session)
    _publish_baseline(db_session)

    real_derive = cert_pack_run.derive_round_scope

    def _two_variants(db, **kw):
        scope = real_derive(db, **kw)
        first = scope.result.variants[0]
        second = CertRequestVariant(
            id=generate_uuid(), cflow_id=first.cflow_id,
            run_number=first.run_number, case_id=first.case_id,
            variant_id="v-collect", api_message_id=first.api_message_id,
            initiator=first.initiator, wire_format="xml",
            input_data={"ReqDispute/Txn/@type": "PRE_ARB"},
            expected=dict(first.expected), strategy="manual")
        db.add(second)
        db.commit()
        scope.variants_by_case[first.case_id].append(second)
        return scope

    monkeypatch.setattr(cert_pack_run, "derive_round_scope", _two_variants)

    bodies = []
    real_handle = cert_pack_run.runtime.handle

    async def _spy(db, *, body, **kw):
        bodies.append(body)
        return await real_handle(db, body=body, **kw)

    monkeypatch.setattr(cert_pack_run.runtime, "handle", _spy)

    _run(db_session)
    assert len(bodies) == 2
    assert bodies[0] != bodies[1], "the variant's inputs reached the wire"
    assert 'type="PRE_ARB"' in bodies[1]


def test_an_input_that_names_no_path_is_recorded_not_dropped(db_session, monkeypatch):
    from app.models.base import generate_uuid
    from app.models.phase_c import CertRequestVariant, CertTestResult

    _seed_registry(db_session)
    _publish_baseline(db_session)
    real_derive = cert_pack_run.derive_round_scope

    def _odd_variant(db, **kw):
        scope = real_derive(db, **kw)
        first = scope.result.variants[0]
        odd = CertRequestVariant(
            id=generate_uuid(), cflow_id=first.cflow_id,
            run_number=first.run_number, case_id=first.case_id,
            variant_id="v-odd", initiator=first.initiator, wire_format="xml",
            input_data={"ReqDispute/NotAField": "x"},
            expected=dict(first.expected), strategy="manual")
        db.add(odd)
        db.commit()
        scope.variants_by_case[first.case_id] = [odd]
        return scope

    monkeypatch.setattr(cert_pack_run, "derive_round_scope", _odd_variant)
    _run(db_session)

    row = db_session.query(CertTestResult).one()
    assert row.actual_response["unrendered_inputs"] == ["ReqDispute/NotAField"]


# ── §3.6.1: the authority side in application mode is TRIGGERED ──────────────

def test_application_mode_triggers_the_app_and_awaits_its_outbound_call(
        db_session, monkeypatch):
    """A deployed application is driven, not called: the trigger says only
    'start' and the outcome arrives as the app's own call through the
    tunnel — so the case becomes one the join is waiting on."""
    from app.core.config import settings
    from app.models.phase_c import CertRunStatus, CertRun, CertTestResult
    from app.services import cert_join

    _seed_registry(db_session)
    _publish_baseline(db_session)
    monkeypatch.setattr(settings, "cert_trigger_url", "https://app.test/__cert/v1/trigger")
    monkeypatch.setattr(settings, "cert_trigger_secret", "s3cret")

    fired = []

    def _fire(url, secret, **kw):
        fired.append((url, secret, kw))
        return True

    monkeypatch.setattr("app.services.integration_testing.trigger.fire_trigger",
                        _fire)

    summary = _run(db_session, test_data={
        "case_catalogue": CATALOGUE, "modes": {"npci_mode": "application"}})

    assert summary["status"] == "awaiting_partner" and summary["passed"] is None
    assert summary["npci_mode"] == "application"

    url, secret, kw = fired[0]
    assert url == "https://app.test/__cert/v1/trigger" and secret == "s3cret"
    assert kw["reply_via"].startswith("a2a://"), "an alias, never a URL"
    assert kw["cert_context"]["npci_mode"] == "application"

    run = db_session.query(CertRun).one()
    assert run.status is CertRunStatus.RUNNING
    assert run.npci_mode == "application"
    row = db_session.query(CertTestResult).one()
    assert row.actual_response["triggered"] is True
    assert cert_join._join_managed(run) is True, "the join owns it"


def test_a_trigger_that_could_not_fire_says_so_on_the_row(db_session, monkeypatch):
    """An accepted-but-never-triggered case would silently wait out the whole
    deadline; the row records which it was."""
    from app.core.config import settings
    from app.models.phase_c import CertTestResult

    _seed_registry(db_session)
    _publish_baseline(db_session)
    monkeypatch.setattr(settings, "cert_trigger_url", "https://app.test/t")
    monkeypatch.setattr("app.services.integration_testing.trigger.fire_trigger",
                        lambda *a, **k: False)

    _run(db_session, test_data={"case_catalogue": CATALOGUE,
                                "modes": {"npci_mode": "application"}})
    row = db_session.query(CertTestResult).one()
    assert row.actual_response["triggered"] is False
    assert "never report" in row.actual_response["reason"]
