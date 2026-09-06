# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-6: the loop decision (pure) and the acting handler behind the flag.

The decision is a pure function — every branch is exercised with plain data.
The handler tests pin the four behaviours that carry the safety story: flag
off returns the OLD wording verbatim and dispatches nothing; a dispatch goes
through `certification_dispatch.run_certification` (the harness-agnostic
seam), never the orchestrator directly; a halt writes
`cert_flow_states.halted_reason`; and the audit stamps ride the dispatch.
The two scripted WALKS at the end are the ones that matter: converging signs
off at round 3, non-converging halts at round 2 instead of burning 3–5.
"""
from __future__ import annotations

import pytest

from app.services.cert_loop import (
    LoopAction, RoundOutcome, decide, failed_cases,
)

R = RoundOutcome


def _r(n, *failed):
    return R(round_number=n, failed_case_ids=frozenset(failed))


# ── failed_cases: only FAIL counts ───────────────────────────────────────────

def test_only_fail_counts_as_failure():
    """SKIP (held for missing data) and ERROR (our infrastructure) are not
    defects the partner can fix; counting them would misfire the guard."""
    assert failed_cases({"a": "FAIL", "b": "SKIP", "c": "ERROR", "d": "PASS"}) \
        == frozenset({"a"})


def test_any_failing_variant_fails_the_case():
    assert failed_cases({"a": ["PASS", "FAIL", "PASS"], "b": ["PASS", "SKIP"]}) \
        == frozenset({"a"})


def test_status_comparison_is_case_insensitive():
    assert failed_cases({"a": "fail"}) == frozenset({"a"})


# ── decide: branch coverage ──────────────────────────────────────────────────

def test_zero_failures_signs_off():
    decision = decide([_r(1)], max_rounds=5)
    assert decision.action is LoopAction.SIGNOFF


def test_signoff_reports_it_never_sends_a_second_signoff():
    decision = decide([_r(1)], max_rounds=5)
    assert "already issued" in decision.reason


def test_identical_failed_set_halts_no_progress():
    decision = decide([_r(1, "a", "b"), _r(2, "a", "b")], max_rounds=5)
    assert decision.action is LoopAction.HALT_NO_PROGRESS
    assert decision.halted


def test_differing_sets_count_as_progress_even_at_the_same_count():
    """A fix that repairs one case and breaks another changed the problem —
    that is progress, not oscillation, at this altitude."""
    decision = decide([_r(1, "a", "b"), _r(2, "a", "c")], max_rounds=5)
    assert decision.action is LoopAction.DISPATCH


def test_round_cap_is_inclusive():
    decision = decide([_r(4, "a", "b"), _r(5, "a")], max_rounds=5)
    assert decision.action is LoopAction.HALT_ROUND_CAP


def test_zero_failures_at_the_cap_still_signs_off():
    """Converging on the last permitted round is success, not a halt."""
    decision = decide([_r(4, "a"), _r(5)], max_rounds=5)
    assert decision.action is LoopAction.SIGNOFF


def test_no_progress_beats_the_cap():
    """The guard matters more than the number: an identical set halts even on
    the capped round, with the more informative reason."""
    decision = decide([_r(4, "a"), _r(5, "a")], max_rounds=5)
    assert decision.action is LoopAction.HALT_NO_PROGRESS


def test_under_cap_with_progress_dispatches():
    decision = decide([_r(1, "a", "b"), _r(2, "a")], max_rounds=5)
    assert decision.action is LoopAction.DISPATCH


def test_first_round_with_failures_dispatches():
    assert decide([_r(1, "a")], max_rounds=5).action is LoopAction.DISPATCH


def test_no_rounds_dispatches_the_first():
    assert decide([], max_rounds=5).action is LoopAction.DISPATCH


# ── the acting handler ───────────────────────────────────────────────────────

OLD_WORDING = "Fix recorded for {n} case(s); {m} triage resolved (re-run on operator action)"
CHANGE, PARTNER = "change-1", "partner-1"
CFLOW = f"CFLOW-{CHANGE[:8]}-{PARTNER[:8]}"


class _Bg:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.phase_c import (
        A2AMessage, CertFlowState, CertRun, CertTestResult, CertTriage,
    )

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        CertRun.__table__, CertTestResult.__table__, CertTriage.__table__,
        A2AMessage.__table__, CertFlowState.__table__,
    ])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _message(db):
    from app.models.phase_c import A2ADirection, A2AMessage

    msg = A2AMessage(partner_id=PARTNER, direction=A2ADirection.INBOUND,
                     task_type="cert_fix_notification",
                     payload={"fixed_case_ids": ["TC1"]}, status="received")
    db.add(msg)
    db.commit()
    return msg


def _run(db, number, statuses: dict[str, str]):
    from app.models.base import generate_uuid
    from app.models.phase_c import (
        CertDirection, CertRun, CertRunStatus, CertTestResult, CertTestStatus,
    )

    run = CertRun(id=generate_uuid(), change_request_id=CHANGE, partner_id=PARTNER,
                  cflow_id=CFLOW, run_number=number, status=CertRunStatus.COMPLETED)
    db.add(run)
    db.flush()
    for tc, st in statuses.items():
        db.add(CertTestResult(id=generate_uuid(), cert_run_id=run.id,
                              test_case_id=tc,
                              direction=CertDirection.AUTHORITY_TO_PARTNER,
                              status=CertTestStatus(st)))
    db.commit()
    return run


def _flow_row(db, phase="FIX_PENDING", current_round=1):
    from app.models.phase_c import CertFlowState

    row = CertFlowState(cflow_id=CFLOW, change_request_id=CHANGE,
                        partner_id=PARTNER, phase=phase,
                        current_round=current_round, history=[])
    db.add(row)
    db.commit()
    return row


def _notify(db, bg):
    from app.a2a_common.authority_handlers import process_cert_fix_notification

    return process_cert_fix_notification(
        PARTNER, CHANGE, {"fixed_case_ids": ["TC1"]}, _message(db), db, bg=bg)


def test_flag_off_returns_the_old_wording_verbatim_and_dispatches_nothing(db_session):
    from app.core.config import settings

    assert settings.cert_auto_loop_enabled is False, "off by default (COMBINED §6)"
    _run(db_session, 1, {"TC1": "fail"})
    bg = _Bg()
    result = _notify(db_session, bg)
    assert result == OLD_WORDING.format(n=1, m=0)
    assert bg.tasks == []


def test_dispatch_goes_through_the_certification_dispatch_seam(db_session, monkeypatch):
    from app.core.config import settings
    from app.services.certification_dispatch import run_certification

    monkeypatch.setattr(settings, "cert_auto_loop_enabled", True)
    _flow_row(db_session)
    run1 = _run(db_session, 1, {"TC1": "fail", "TC2": "fail"})
    run2 = _run(db_session, 2, {"TC1": "fail", "TC2": "pass"})
    bg = _Bg()
    result = _notify(db_session, bg)

    assert len(bg.tasks) == 1
    fn, args, kwargs = bg.tasks[0]
    assert fn is run_certification, \
        "round N+1 must dispatch through the seam, never orchestrate_cert_run*"
    meta = kwargs["dispatch_meta"]
    assert meta["dispatched_by"] == "auto"
    assert meta["previous_run_id"] == run2.id
    assert meta["fix_notification_message_id"]
    assert "auto-loop" in result


def test_dispatch_fires_fix_received_on_the_persisted_flow(db_session, monkeypatch):
    from app.core.config import settings
    from app.services.cert_agent import flow_store

    monkeypatch.setattr(settings, "cert_auto_loop_enabled", True)
    _flow_row(db_session, phase="FIX_PENDING")
    _run(db_session, 1, {"TC1": "fail"})
    _notify(db_session, _Bg())
    row = flow_store.load(db_session, CFLOW)
    assert row.phase == "RUNNING", "FIX_PENDING -> RUNNING via fix_received"


def test_halt_writes_halted_reason_and_dispatches_nothing(db_session, monkeypatch):
    from app.core.config import settings
    from app.services.cert_agent import flow_store

    monkeypatch.setattr(settings, "cert_auto_loop_enabled", True)
    _flow_row(db_session)
    _run(db_session, 1, {"TC1": "fail", "TC2": "fail"})
    _run(db_session, 2, {"TC1": "fail", "TC2": "fail"})
    bg = _Bg()
    result = _notify(db_session, bg)
    assert bg.tasks == []
    assert "halted" in result
    assert flow_store.load(db_session, CFLOW).halted_reason


def test_signoff_dispatches_nothing_and_sends_no_second_signoff(db_session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "cert_auto_loop_enabled", True)
    _run(db_session, 1, {"TC1": "pass", "TC2": "pass"})
    bg = _Bg()
    result = _notify(db_session, bg)
    assert bg.tasks == []
    assert "already issued" in result


# ── the two scripted walks ───────────────────────────────────────────────────

def test_converging_walk_dispatches_twice_then_signs_off(db_session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "cert_auto_loop_enabled", True)
    _flow_row(db_session)
    bg = _Bg()

    _run(db_session, 1, {"TC1": "fail", "TC2": "fail"})
    _notify(db_session, bg)                    # round 1: 2 fail → dispatch
    assert len(bg.tasks) == 1

    _run(db_session, 2, {"TC1": "fail", "TC2": "pass"})
    _notify(db_session, bg)                    # round 2: 1 fail, 1 fixed → dispatch
    assert len(bg.tasks) == 2

    _run(db_session, 3, {"TC1": "pass", "TC2": "pass"})
    result = _notify(db_session, bg)           # round 3: clean → sign-off
    assert len(bg.tasks) == 2
    assert "already issued" in result


def test_non_converging_walk_halts_at_round_two_not_the_cap(db_session, monkeypatch):
    """The walk that matters: an identical failed set stops the loop at round
    2 — it must NOT burn rounds 3, 4 and 5 discovering nothing new."""
    from app.core.config import settings
    from app.services.cert_agent import flow_store

    monkeypatch.setattr(settings, "cert_auto_loop_enabled", True)
    _flow_row(db_session)
    bg = _Bg()

    _run(db_session, 1, {"TC1": "fail", "TC2": "fail"})
    _notify(db_session, bg)                    # round 1 → dispatch
    assert len(bg.tasks) == 1

    _run(db_session, 2, {"TC1": "fail", "TC2": "fail"})
    result = _notify(db_session, bg)           # identical set → halt
    assert len(bg.tasks) == 1, "no round 3 may be dispatched"
    assert "halted" in result
    assert flow_store.load(db_session, CFLOW).halted_reason

    result = _notify(db_session, bg)           # still halted on a repeat poke
    assert len(bg.tasks) == 1
    assert "halted" in result