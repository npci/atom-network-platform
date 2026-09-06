# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-7: round history and the per-round diff.

THE test in this file is the fix-repairs-two-breaks-a-third scenario: the
failure count drops 2→1, the no-progress guard reads that as progress and
keeps dispatching — only the `newly_failing` column says a regression
happened. Everything else (no_longer_verified vs fixed, scope movement,
round-1 semantics, coverage as-built) guards the edges of that claim.
"""
from __future__ import annotations

import pytest

from app.services import cert_reporting

CFLOW, CHANGE, PARTNER = "CFLOW-r", "change-1", "partner-1"


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.phase_c import CertFlowState, CertRun, CertTestResult

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        CertRun.__table__, CertTestResult.__table__, CertFlowState.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _round(db, number, statuses: dict[str, str | list[str]], coverage=None):
    from app.models.base import generate_uuid
    from app.models.phase_c import (
        CertDirection, CertRun, CertRunStatus, CertTestResult, CertTestStatus,
    )

    run = CertRun(id=generate_uuid(), change_request_id=CHANGE, partner_id=PARTNER,
                  cflow_id=CFLOW, run_number=number,
                  status=CertRunStatus.COMPLETED, coverage=coverage)
    db.add(run)
    db.flush()
    for tc, st in statuses.items():
        for one in ([st] if isinstance(st, str) else st):
            db.add(CertTestResult(id=generate_uuid(), cert_run_id=run.id,
                                  test_case_id=tc,
                                  direction=CertDirection.AUTHORITY_TO_PARTNER,
                                  status=CertTestStatus(one)))
    db.commit()
    return run


# ── THE column ───────────────────────────────────────────────────────────────

def test_a_fix_that_repairs_two_and_breaks_a_third_is_surfaced(db_session):
    """Counts say 2→1 = progress; the loop keeps dispatching. Only this diff
    says TC3 is a REGRESSION."""
    _round(db_session, 1, {"TC1": "fail", "TC2": "fail", "TC3": "pass"})
    _round(db_session, 2, {"TC1": "pass", "TC2": "pass", "TC3": "fail"})

    diff = cert_reporting.round_diff(db_session, CFLOW, 2)
    assert diff["fixed"] == ["TC1", "TC2"]
    assert diff["newly_failing"] == ["TC3"]
    assert diff["still_failing"] == []


def test_fail_to_skip_is_no_longer_verified_never_fixed(db_session):
    """A defect that went UNOBSERVED did not go away."""
    _round(db_session, 1, {"TC1": "fail"})
    _round(db_session, 2, {"TC1": "skip"})
    diff = cert_reporting.round_diff(db_session, CFLOW, 2)
    assert diff["fixed"] == []
    assert diff["no_longer_verified"] == ["TC1"]


def test_a_case_entering_scope_failing_is_newly_failing(db_session):
    _round(db_session, 1, {"TC1": "pass"})
    _round(db_session, 2, {"TC1": "pass", "TC9": "fail"})
    diff = cert_reporting.round_diff(db_session, CFLOW, 2)
    assert diff["newly_failing"] == ["TC9"]
    assert diff["entered_scope"] == ["TC9"]


def test_a_shrinking_scope_is_surfaced_not_read_as_progress(db_session):
    _round(db_session, 1, {"TC1": "fail", "TC2": "pass"})
    _round(db_session, 2, {"TC2": "pass"})
    diff = cert_reporting.round_diff(db_session, CFLOW, 2)
    assert diff["left_scope"] == ["TC1"]
    assert diff["fixed"] == [], "a case that vanished was not fixed"


def test_round_one_diffs_against_nothing(db_session):
    _round(db_session, 1, {"TC1": "fail", "TC2": "pass"})
    diff = cert_reporting.round_diff(db_session, CFLOW, 1)
    assert diff["previous_run_number"] is None
    assert diff["newly_failing"] == ["TC1"], \
        "every first-round failure is newly failing — the true statement"


def test_any_failing_variant_fails_the_case(db_session):
    """Multiple variant executions per case: worst-of, matching the loop."""
    _round(db_session, 1, {"TC1": ["pass", "fail", "pass"]})
    diff = cert_reporting.round_diff(db_session, CFLOW, 1)
    assert diff["newly_failing"] == ["TC1"]


def test_unknown_round_is_an_error(db_session):
    assert "error" in cert_reporting.round_diff(db_session, CFLOW, 7)


# ── history + coverage as built ──────────────────────────────────────────────

def test_history_carries_the_coverage_note_as_stamped(db_session):
    note = {"summary": "NOT covered: 1 changed API(s)…", "fallback": False,
            "uncovered_apis": ["ReqNewThing"], "gaps": []}
    _round(db_session, 1, {"TC1": "pass"}, coverage=note)
    _round(db_session, 2, {"TC1": "pass"})
    history = cert_reporting.round_history(db_session, CFLOW)
    assert [h["run_number"] for h in history] == [1, 2]
    assert history[0]["coverage"] == note
    assert history[1]["coverage"] is None


def test_flow_report_includes_the_persisted_lifecycle(db_session):
    from app.models.phase_c import CertFlowState

    _round(db_session, 1, {"TC1": "fail"})
    db_session.add(CertFlowState(
        cflow_id=CFLOW, change_request_id=CHANGE, partner_id=PARTNER,
        phase="RUNNING", current_round=1, halted_reason="round 1 failed the same set",
        history=[["readiness_declared", "CONFIG_REQUESTED", "2026-08-31T00:00:00"]]))
    db_session.commit()

    report = cert_reporting.flow_report(db_session, CFLOW)
    assert len(report["rounds"]) == 1 and len(report["diffs"]) == 1
    assert report["flow"]["halted_reason"] == "round 1 failed the same set"
    assert report["flow"]["transitions"][0][0] == "readiness_declared"


def test_orchestrator_stamps_the_coverage_note():
    import inspect

    from app.services import cert_orchestrator

    src = inspect.getsource(cert_orchestrator.orchestrate_cert_run_precert_engine)
    assert '"summary": build.summary()' in src
    assert '"uncovered_apis": build.uncovered_apis' in src


def test_report_endpoint_404s_an_unknown_flow(db_session):
    from fastapi import HTTPException

    from app.api.cert_report import cert_flow_report

    with pytest.raises(HTTPException) as exc:
        cert_flow_report("CFLOW-none", db_session, None)
    assert exc.value.status_code == 404
