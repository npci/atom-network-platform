# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-6, authority half: the start signal and the bank-reported result.

The verify bar's third clause is the load-bearing one: a partner-initiated
case the partner never reports must be recorded as NOT REPORTED — an
ERROR-class placeholder — rather than silently passing; and when the bank's
`cert_case_result` (reporter=bank) does arrive, it is THE result, replacing
the placeholder and recomputing the run's counters.
"""
from __future__ import annotations

import inspect

import pytest

from app.a2a_common.protocol import MESSAGES, A2ATaskType, Direction

CHANGE, PARTNER = "change-1", "partner-1"


# ── the wire type ────────────────────────────────────────────────────────────

def test_cert_execution_start_is_a_distinct_ext_instruction():
    tt = A2ATaskType.CERT_EXECUTION_START
    assert tt.is_ext(), "ext, never part of the frozen PDF contract"
    spec = MESSAGES[tt]
    assert spec.direction == Direction.AUTHORITY_TO_PARTNER
    assert spec.carries_pii is False, \
        "identifiers only — the case DATA travelled on CERT_TEST_PREPARATION"


# ── the bank-reported result is THE result ───────────────────────────────────

@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.phase_c import A2AMessage, CertRun, CertTestResult

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        CertRun.__table__, CertTestResult.__table__, A2AMessage.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _run_with_placeholder(db, case_id="TC6"):
    from app.models.base import generate_uuid
    from app.models.phase_c import (
        CertDirection, CertRun, CertRunStatus, CertTestResult, CertTestStatus,
    )

    run = CertRun(id=generate_uuid(), change_request_id=CHANGE, partner_id=PARTNER,
                  cflow_id="CFLOW-x", run_number=1, status=CertRunStatus.COMPLETED,
                  total=2, passed=1, failed=0, skipped=1)
    db.add(run)
    db.flush()
    # TC1: a case the AUTHORITY executed and adjudicated itself. No
    # not_reported marker — it is nobody's to report.
    db.add(CertTestResult(id=generate_uuid(), cert_run_id=run.id, test_case_id="TC1",
                          direction=CertDirection.AUTHORITY_TO_PARTNER,
                          status=CertTestStatus.PASS,
                          actual_response={"variant_id": "v-1"}))
    # The placeholder awaiting the bank. Direction stays AUTHORITY_TO_PARTNER on
    # purpose: that is the shape `cert_pack_run` stamps for an
    # APPLICATION-MODE case, which this side only TRIGGERED and which the
    # partner still reports. The `not_reported` marker — not the direction —
    # is what makes a row the partner's to write.
    db.add(CertTestResult(id=generate_uuid(), cert_run_id=run.id, test_case_id=case_id,
                          direction=CertDirection.AUTHORITY_TO_PARTNER,
                          status=CertTestStatus.ERROR,
                          actual_response={"not_reported": True,
                                           "variant_id": "v-6"}))
    db.commit()
    return run


def _message(db):
    from app.models.phase_c import A2ADirection, A2AMessage

    msg = A2AMessage(partner_id=PARTNER, direction=A2ADirection.INBOUND,
                     task_type="cert_case_result", payload={}, status="received")
    db.add(msg)
    db.commit()
    return msg


def _report(db, payload):
    from app.a2a_common.authority_handlers import process_cert_case_result_report

    return process_cert_case_result_report(PARTNER, CHANGE, payload, _message(db), db)


def test_bank_report_replaces_the_not_reported_placeholder(db_session):
    from app.models.phase_c import CertTestResult, CertTestStatus

    run = _run_with_placeholder(db_session)
    out = _report(db_session, {"reporter": "bank", "case_id": "TC6",
                               "status": "passed", "details": {"txn_id": "T9"}})
    assert "recorded" in out
    row = db_session.query(CertTestResult).filter_by(test_case_id="TC6").one()
    assert row.status == CertTestStatus.PASS
    assert row.actual_response["reporter"] == "bank"
    db_session.refresh(run)
    assert (run.passed, run.failed, run.skipped, run.total) == (2, 0, 0, 2), \
        "counters are recomputed, not nudged"


def test_bank_reported_failure_counts_as_a_failure(db_session):
    run = _run_with_placeholder(db_session)
    _report(db_session, {"reporter": "bank", "case_id": "TC6", "status": "failed"})
    db_session.refresh(run)
    assert run.failed == 1


def test_unreported_case_stays_not_reported(db_session):
    """The silent-pass guard: with NO report, the placeholder remains ERROR
    and the counters keep it out of `passed`."""
    from app.models.phase_c import CertTestResult, CertTestStatus

    _run_with_placeholder(db_session)
    row = db_session.query(CertTestResult).filter_by(test_case_id="TC6").one()
    assert row.status == CertTestStatus.ERROR


def test_a_report_for_an_unknown_case_creates_the_row(db_session):
    from app.models.phase_c import CertDirection, CertTestResult

    _run_with_placeholder(db_session)
    _report(db_session, {"reporter": "bank", "case_id": "TC7", "status": "passed"})
    row = db_session.query(CertTestResult).filter_by(test_case_id="TC7").one()
    assert row.direction == CertDirection.PARTNER_TO_AUTHORITY


# ── a report may only speak for the partner's OWN cases ─────────────────────

def test_a_report_cannot_overwrite_a_case_the_authority_adjudicated(db_session):
    """The integrity bar: TC1 is this side's own verdict, carrying this side's
    evidence. A bank claim naming it is refused outright — not downgraded,
    not merged — and the run's counters do not move."""
    from app.models.phase_c import CertTestResult, CertTestStatus

    run = _run_with_placeholder(db_session)
    before = db_session.query(CertTestResult).filter_by(test_case_id="TC1").one()
    assert before.status == CertTestStatus.PASS

    out = _report(db_session, {"reporter": "bank", "case_id": "TC1",
                               "status": "failed"})

    assert "refused" in out
    row = db_session.query(CertTestResult).filter_by(test_case_id="TC1").one()
    assert row.status == CertTestStatus.PASS, "the authority's verdict stands"
    assert row.actual_response == {"variant_id": "v-1"}, \
        "the authority's own evidence is untouched"
    db_session.refresh(run)
    assert (run.passed, run.failed, run.total) == (1, 0, 2), \
        "a refused report moves no counter"


def test_a_refused_report_cannot_clear_an_authority_recorded_failure(db_session):
    """The exploit as reported: flip this side's FAIL to PASS, erase the
    assertion evidence, and let the join finalize the run as CERTIFIED."""
    from app.models.base import generate_uuid
    from app.models.phase_c import CertDirection, CertTestResult, CertTestStatus

    run = _run_with_placeholder(db_session)
    db_session.add(CertTestResult(
        id=generate_uuid(), cert_run_id=run.id, test_case_id="LL_4",
        direction=CertDirection.AUTHORITY_TO_PARTNER, status=CertTestStatus.FAIL,
        expected_response={"result": "PASS", "code": "E004"},
        actual_response={"variant_id": "v-4",
                         "assertion_failures": [{"field": "amount"}]}))
    db_session.commit()

    _report(db_session, {"reporter": "bank", "case_id": "LL_4",
                         "status": "passed"})

    row = db_session.query(CertTestResult).filter_by(test_case_id="LL_4").one()
    assert row.status == CertTestStatus.FAIL
    assert row.actual_response["assertion_failures"] == [{"field": "amount"}], \
        "the failure evidence survives the claim"
    db_session.refresh(run)
    assert sum(1 for r in run.results if r.status == CertTestStatus.FAIL) == 1, \
        "the run still carries its failure, so it cannot finalize as CERTIFIED"


def test_a_bank_report_may_be_corrected_by_a_later_one(db_session):
    """A row a previous report wrote stays the partner's to speak for —
    re-delivery and correction must keep working."""
    from app.models.phase_c import CertTestResult, CertTestStatus

    run = _run_with_placeholder(db_session)
    _report(db_session, {"reporter": "bank", "case_id": "TC6",
                         "status": "passed"})
    out = _report(db_session, {"reporter": "bank", "case_id": "TC6",
                               "status": "failed"})

    assert "recorded" in out
    row = db_session.query(CertTestResult).filter_by(test_case_id="TC6").one()
    assert row.status == CertTestStatus.FAIL
    db_session.refresh(run)
    assert run.failed == 1


def test_the_variant_a_placeholder_named_survives_the_report(db_session):
    """`variant_id` is what binds a row to its variant once `not_reported` is
    gone — a multi-variant case could not be matched again without it."""
    from app.models.phase_c import CertTestResult

    _run_with_placeholder(db_session)
    _report(db_session, {"reporter": "bank", "case_id": "TC6",
                         "status": "passed"})
    row = db_session.query(CertTestResult).filter_by(test_case_id="TC6").one()
    assert row.actual_response["variant_id"] == "v-6"


def test_a_report_fills_one_placeholder_per_variant(db_session):
    """A case holds one row per VARIANT, so `(run, case_id)` does not identify
    a row: each report must land on its own placeholder, not rewrite the same
    arbitrary one twice."""
    from app.models.base import generate_uuid
    from app.models.phase_c import CertDirection, CertTestResult, CertTestStatus

    run = _run_with_placeholder(db_session)
    db_session.add(CertTestResult(
        id=generate_uuid(), cert_run_id=run.id, test_case_id="TC6",
        direction=CertDirection.PARTNER_TO_AUTHORITY, status=CertTestStatus.ERROR,
        actual_response={"not_reported": True, "variant_id": "v-6b"}))
    db_session.commit()

    _report(db_session, {"reporter": "bank", "case_id": "TC6", "status": "passed",
                         "details": {"variant_id": "v-6b"}})

    rows = {r.actual_response.get("variant_id"): r for r in
            db_session.query(CertTestResult).filter_by(test_case_id="TC6").all()}
    assert rows["v-6b"].status == CertTestStatus.PASS, "the named variant is written"
    assert rows["v-6"].actual_response.get("not_reported"), \
        "the other variant is still awaited"


def test_an_echo_keeps_the_pre_i6_ack_verbatim(db_session):
    from app.models.phase_c import CertTestResult, CertTestStatus

    _run_with_placeholder(db_session)
    out = _report(db_session, {"case_id": "TC6", "status": "passed"})   # no reporter
    assert out == "cert_case_result received"
    row = db_session.query(CertTestResult).filter_by(test_case_id="TC6").one()
    assert row.status == CertTestStatus.ERROR, "an echo must not overwrite anything"


def test_a_report_with_no_run_is_acknowledged_gracefully(db_session):
    out = _report(db_session, {"reporter": "bank", "case_id": "TC1",
                               "status": "passed"})
    assert "no run to attach" in out


# ── the orchestrator's split (source pins on the async path) ─────────────────

def _engine_src():
    from app.services import cert_orchestrator

    return inspect.getsource(cert_orchestrator.orchestrate_cert_run_precert_engine)


def test_start_signal_is_gated_on_the_tunnel_and_names_the_alias():
    src = _engine_src()
    assert "CERT_EXECUTION_START" in src
    assert "settings.integration_testing_enabled" in src
    assert "integration_testing_simulator_alias" in src


def test_partner_owned_cases_are_placeholdered_not_executed():
    src = _engine_src()
    assert "if tc in _partner_owned:" in src
    assert '"not_reported — partner-initiated "' in src
