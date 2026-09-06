# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Script-based UAT step — log parsing + the run/advance contract.

Two things must hold or the combined step lies: the counts must come from the
script's own output (summary line > markers > exit code, in that order), and
the pipeline must advance to TRIAGE whether the suite passed or failed —
failures are triage's input, not a reason to stall the step.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.uat_script import parse_test_log, run_uat_script


# ── parse_test_log: summary line > markers > exit code ───────────────────────

def test_summary_line_wins_over_markers():
    log = "PASS A one\nFAIL B two\nTESTS: total=7 passed=6 failed=0 skipped=1"
    assert parse_test_log(log, 0) == {"total": 7, "passed": 6, "failed": 0, "skipped": 1}


def test_last_summary_line_wins_when_a_wrapper_runs_several_suites():
    log = ("TESTS: total=3 passed=3 failed=0 skipped=0\n"
           "TESTS: total=5 passed=4 failed=1 skipped=0")
    assert parse_test_log(log, 1)["failed"] == 1
    assert parse_test_log(log, 1)["total"] == 5


def test_markers_are_counted_when_no_summary_present():
    log = "PASS T1 ok\nfail T2 broken\nSKIP T3 later\nsome noise\nPASS T4 ok"
    assert parse_test_log(log, 0) == {"total": 4, "passed": 2, "failed": 1, "skipped": 1}


def test_summary_without_skipped_defaults_to_zero():
    assert parse_test_log("TESTS: total=2 passed=2 failed=0", 0)["skipped"] == 0


@pytest.mark.parametrize("exit_code,failed", [(0, 0), (3, 1)])
def test_markerless_output_falls_back_to_exit_code(exit_code, failed):
    out = parse_test_log("no structured output at all", exit_code)
    assert out["failed"] == failed
    assert out["total"] == 1


def test_passing_words_mid_line_are_not_markers():
    # "PASSWORD" and an indented "FAILURE:" narrative line must not count.
    out = parse_test_log("PASSWORD accepted\nnote: FAILURE_MODES documented", 0)
    assert out == {"total": 1, "passed": 1, "failed": 0, "skipped": 0}


# ── run_uat_script: streams the log, records counts, advances to TRIAGE ──────

@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register models so metadata is complete
    from app.core.database import Base
    from app.models.phase_b import PhaseBRun, UATTestRun

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[PhaseBRun.__table__, UATTestRun.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _rows(db):
    from app.models.base import utcnow
    from app.models.phase_b import PhaseBRun, PhaseBStep, TestRunStatus, UATTestRun

    run = PhaseBRun(change_request_id="chg-1", current_step=PhaseBStep.TEST_GEN)
    db.add(run)
    db.flush()
    tr = UATTestRun(phase_b_run_id=run.id, suite_version=0, iteration_number=1,
                    status=TestRunStatus.RUNNING, started_at=utcnow(),
                    script_path="unused-in-test")
    db.add(tr)
    db.commit()
    return run, tr


def test_passing_script_records_counts_log_and_advances(db_session, tmp_path):
    from app.models.phase_b import PhaseBStep, TestRunStatus

    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/bash\n"
                      "echo 'PASS T1 first'\n"
                      "echo 'SKIP T2 later'\n"
                      "echo 'TESTS: total=2 passed=1 failed=0 skipped=1'\n")
    run, tr = _rows(db_session)
    asyncio.run(run_uat_script(run, db_session, test_run=tr, script_path=str(script)))
    assert tr.status == TestRunStatus.COMPLETED
    assert (tr.total, tr.passed, tr.failed, tr.skipped) == (2, 1, 0, 1)
    assert "PASS T1 first" in tr.log and "[exit=0" in tr.log
    assert run.current_step == PhaseBStep.TRIAGE


def test_failing_script_still_advances_to_triage(db_session, tmp_path):
    from app.models.phase_b import PhaseBStep, TestRunStatus

    script = tmp_path / "bad.sh"
    script.write_text("#!/bin/bash\n"
                      "echo 'FAIL T1 broken — expected 200 got 500'\n"
                      "echo 'TESTS: total=1 passed=0 failed=1 skipped=0'\n"
                      "exit 1\n")
    run, tr = _rows(db_session)
    asyncio.run(run_uat_script(run, db_session, test_run=tr, script_path=str(script)))
    assert tr.status == TestRunStatus.COMPLETED
    assert tr.failed == 1
    assert run.current_step == PhaseBStep.TRIAGE, \
        "failures must flow forward to triage, not stall the UAT step"


def test_base_url_reaches_the_script_as_first_argument(db_session, tmp_path):
    script = tmp_path / "args.sh"
    script.write_text("#!/bin/bash\necho \"target=$1\"\necho 'TESTS: total=1 passed=1 failed=0 skipped=0'\n")
    run, tr = _rows(db_session)
    asyncio.run(run_uat_script(run, db_session, test_run=tr, script_path=str(script),
                               base_url="https://uat.example.test"))
    assert "target=https://uat.example.test" in tr.log


def test_ansi_escapes_are_stripped_from_the_stored_log(db_session, tmp_path):
    """A colorized script must not leak escape codes into the log the UI
    renders and AI triage reads — same guarantee the build path gives."""
    script = tmp_path / "color.sh"
    script.write_text("#!/bin/bash\n"
                      "printf '\\033[32mPASS T1 colored\\033[0m\\n'\n"
                      "echo 'TESTS: total=1 passed=1 failed=0 skipped=0'\n")
    run, tr = _rows(db_session)
    asyncio.run(run_uat_script(run, db_session, test_run=tr, script_path=str(script)))
    assert "\x1b[" not in tr.log
    assert "PASS T1 colored" in tr.log
    assert tr.passed == 1


def test_legacy_test_exec_step_also_advances(db_session, tmp_path):
    from app.models.phase_b import PhaseBStep

    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/bash\necho 'TESTS: total=1 passed=1 failed=0 skipped=0'\n")
    run, tr = _rows(db_session)
    run.current_step = PhaseBStep.TEST_EXEC
    db_session.commit()
    asyncio.run(run_uat_script(run, db_session, test_run=tr, script_path=str(script)))
    assert run.current_step == PhaseBStep.TRIAGE
