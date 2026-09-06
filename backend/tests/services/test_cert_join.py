# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-7: the suite join — the verify bar, as behaviour.

1. the run completes when the LAST partner result arrives;
2. a partner that reports nothing hits the deadline and the run still
   terminates with a readable verdict (unreported recorded, never certified);
3. a restart mid-wait resumes — pinned structurally: the sweep runs on a
   FRESH session with nothing but the database rows.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.core.config import settings
from app.services import cert_join

CHANGE, PARTNER, CFLOW = "change-1", "partner-1", "CFLOW-j"


@pytest.fixture
def db_env(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    import app.core.database as database
    from app.core.database import Base
    from app.models.phase_c import (
        A2AMessage, CertFlowState, CertRun, CertTestResult,
        ChangePartnerAssignment, PartnerAgent,
    )

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)
    Base.metadata.create_all(engine, tables=[
        CertRun.__table__, CertTestResult.__table__, CertFlowState.__table__,
        ChangePartnerAssignment.__table__, PartnerAgent.__table__,
        A2AMessage.__table__,
    ])
    maker = sessionmaker(bind=engine)
    # check_and_finalize / sweep_expired open their OWN sessions — point the
    # app's SessionLocal at the test engine (StaticPool: one shared connection).
    monkeypatch.setattr(database, "SessionLocal", maker)
    session = maker()
    # The signoff needs a resolvable partner (finalize skips the send when the
    # partner row is gone — a deleted partner must not crash the join).
    session.add(PartnerAgent(id=PARTNER, name="Bank One"))
    session.commit()
    yield session
    session.close()


def _run(db, *, statuses: dict[str, tuple[str, bool]], started_offset_s=0):
    """statuses: case_id -> (status, is_not_reported_placeholder)."""
    from app.models.base import generate_uuid, utcnow
    from app.models.phase_c import (
        CertDirection, CertRun, CertRunStatus, CertTestResult, CertTestStatus,
    )

    run = CertRun(id=generate_uuid(), change_request_id=CHANGE, partner_id=PARTNER,
                  cflow_id=CFLOW, run_number=1, status=CertRunStatus.RUNNING,
                  started_at=utcnow() - timedelta(seconds=started_offset_s))
    db.add(run)
    db.flush()
    for tc, (st, placeholder) in statuses.items():
        db.add(CertTestResult(
            id=generate_uuid(), cert_run_id=run.id, test_case_id=tc,
            direction=CertDirection.AUTHORITY_TO_PARTNER,
            status=CertTestStatus(st),
            actual_response={"not_reported": True, "reason": "not_reported — awaiting"}
            if placeholder else None,
        ))
    db.commit()
    return run


def _stub_signoff(monkeypatch):
    sent = []

    async def fake(**kwargs):
        sent.append(kwargs)

        class _M:
            status = "delivered"
        return _M()

    import app.services.a2a_client as a2a_client

    monkeypatch.setattr(a2a_client, "send_task_to_partner", fake)
    return sent


# ── pending semantics ────────────────────────────────────────────────────────

def test_only_the_marker_counts_as_pending(db_env):
    run = _run(db_env, statuses={
        "TC1": ("pass", False),
        "TC2": ("error", False),      # genuine authority-side ERROR
        "TC6": ("error", True),       # awaiting the bank
    })
    assert cert_join.pending_case_ids(run) == ["TC6"]


# ── verify bar 1: the last report completes the run ──────────────────────────

def test_last_bank_report_finalizes_the_run(db_env, monkeypatch):
    from app.models.phase_c import CertRunStatus, CertTestResult, CertTestStatus

    sent = _stub_signoff(monkeypatch)
    run = _run(db_env, statuses={"TC1": ("pass", False), "TC6": ("error", True)})

    # The bank's report lands (as process_cert_case_result_report does it):
    row = db_env.query(CertTestResult).filter_by(test_case_id="TC6").one()
    row.status = CertTestStatus.PASS
    row.actual_response = {"reporter": "bank", "status": "passed"}
    db_env.commit()

    out = asyncio.run(cert_join.check_and_finalize(run.id))
    assert out["finalized"] and out["all_passed"]
    db_env.expire_all()
    assert db_env.get(type(run), run.id).status == CertRunStatus.COMPLETED
    assert len(sent) == 1, "a clean sweep signs off exactly once"
    assert sent[0]["payload"]["all_passed"] is True


def test_a_non_last_report_does_not_finalize(db_env, monkeypatch):
    from app.models.phase_c import CertRunStatus

    _stub_signoff(monkeypatch)
    run = _run(db_env, statuses={"TC5": ("error", True), "TC6": ("error", True)})
    out = asyncio.run(cert_join.check_and_finalize(run.id))
    assert not out["finalized"]
    db_env.expire_all()
    assert db_env.get(type(run), run.id).status == CertRunStatus.RUNNING


# ── verify bar 2: the deadline yields a readable verdict ─────────────────────

def test_deadline_finalizes_with_unreported_recorded_and_no_certification(db_env, monkeypatch):
    from app.models.phase_c import CertRunStatus

    sent = _stub_signoff(monkeypatch)
    monkeypatch.setattr(settings, "cert_suite_deadline_s", 60.0, raising=False)
    run = _run(db_env, statuses={"TC1": ("pass", False), "TC6": ("error", True)},
               started_offset_s=120)   # past the deadline

    counts = asyncio.run(cert_join.sweep_expired(db_env))
    assert counts["finalized"] == 1
    db_env.expire_all()
    refreshed = db_env.get(type(run), run.id)
    assert refreshed.status == CertRunStatus.COMPLETED
    assert refreshed.passed == 1 and refreshed.skipped == 1
    assert cert_join.pending_case_ids(refreshed) == ["TC6"], \
        "the unreported case stays recorded explicitly"
    assert sent == [], "an unreported case must never be signed off as a pass"


def test_a_run_inside_its_deadline_is_left_waiting(db_env, monkeypatch):
    from app.models.phase_c import CertRunStatus

    _stub_signoff(monkeypatch)
    monkeypatch.setattr(settings, "cert_suite_deadline_s", 600.0, raising=False)
    run = _run(db_env, statuses={"TC6": ("error", True)}, started_offset_s=10)
    counts = asyncio.run(cert_join.sweep_expired(db_env))
    assert counts["finalized"] == 0
    db_env.expire_all()
    assert db_env.get(type(run), run.id).status == CertRunStatus.RUNNING


# ── verify bar 3: restart-safe by construction ───────────────────────────────

def test_sweep_resumes_from_nothing_but_the_database(db_env, monkeypatch):
    """The dispatching process is gone; a FRESH session (the sweep's own
    SessionLocal) sees the rows and joins a fully-reported run the report
    hook missed."""
    from app.models.phase_c import CertRunStatus, CertTestResult, CertTestStatus

    _stub_signoff(monkeypatch)
    run = _run(db_env, statuses={"TC6": ("error", True)}, started_offset_s=1)
    row = db_env.query(CertTestResult).filter_by(test_case_id="TC6").one()
    row.status = CertTestStatus.PASS
    row.actual_response = {"reporter": "bank", "status": "passed"}
    db_env.commit()

    counts = asyncio.run(cert_join.sweep_expired())    # its OWN session
    assert counts["finalized"] == 1
    db_env.expire_all()
    assert db_env.get(type(run), run.id).status == CertRunStatus.COMPLETED


def test_sweep_never_touches_a_foreign_running_run(db_env, monkeypatch):
    """The cert-agent path also parks runs at RUNNING while it works. A run
    with no join fingerprints (no marker, no bank row) is not ours — even far
    past any deadline, the sweep must leave it alone."""
    from app.models.phase_c import CertRunStatus

    _stub_signoff(monkeypatch)
    monkeypatch.setattr(settings, "cert_suite_deadline_s", 1.0, raising=False)
    run = _run(db_env, statuses={"TC1": ("pass", False)}, started_offset_s=9999)
    counts = asyncio.run(cert_join.sweep_expired(db_env))
    assert counts["finalized"] == 0
    db_env.expire_all()
    assert db_env.get(type(run), run.id).status == CertRunStatus.RUNNING


# ── idempotence + flow closure ───────────────────────────────────────────────

def test_finalize_is_idempotent(db_env, monkeypatch):
    _stub_signoff(monkeypatch)
    run = _run(db_env, statuses={"TC1": ("pass", False)})
    first = asyncio.run(cert_join.finalize_run(db_env, run, reason="t"))
    second = asyncio.run(cert_join.finalize_run(db_env, run, reason="t"))
    assert first["finalized"] and not second["finalized"]


def test_clean_join_closes_the_persisted_flow(db_env, monkeypatch):
    from app.models.phase_c import CertFlowState

    _stub_signoff(monkeypatch)
    db_env.add(CertFlowState(cflow_id=CFLOW, change_request_id=CHANGE,
                             partner_id=PARTNER, phase="RUNNING",
                             current_round=1, history=[]))
    db_env.commit()
    run = _run(db_env, statuses={"TC1": ("pass", False)})
    asyncio.run(cert_join.finalize_run(db_env, run, reason="t"))
    db_env.expire_all()
    assert db_env.get(CertFlowState, CFLOW).phase == "COMPLETED"


# ── the orchestrator defers; the join owns the terminal actions ──────────────

def test_orchestrator_defers_terminal_actions_while_awaiting():
    import inspect

    from app.services import cert_orchestrator

    src = inspect.getsource(cert_orchestrator.orchestrate_cert_run_precert_engine)
    assert "_awaiting = bool(_partner_owned)" in src
    assert "CertRunStatus.RUNNING if _awaiting" in src
    assert "not _awaiting and assignment" in src


def test_join_is_harness_agnostic():
    """§12.1's pin: the join must survive SIM-8 deleting the precert path —
    it may import no harness and no pack. Checked on EXECUTABLE code only:
    docstrings legitimately explain the constraint by naming it (gotcha #2 —
    a comment tripping its own grep)."""
    import inspect
    import re

    from app.services import cert_join as module

    src = inspect.getsource(module)
    body = re.sub(r'"""(?:.|\n)*?"""', "", src)
    body = "\n".join(ln for ln in body.splitlines()
                     if not ln.strip().startswith("#"))
    assert "precert" not in body.lower()
    assert "packs" not in body
