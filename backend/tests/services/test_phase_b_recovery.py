# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Startup sweep for orphaned Phase B script runs.

The build/UAT scripts execute as in-process asyncio tasks, so rows left
QUEUED/RUNNING at boot belonged to a dead process. The sweep must fail them
(so the one-run-at-a-time guard unblocks immediately, not after the staleness
window), route a swept UAT failure forward to TRIAGE, and be idempotent.
"""
from __future__ import annotations

import pytest

from app.services.phase_b_recovery import sweep_orphan_script_runs


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register models so metadata is complete
    from app.core.database import Base
    from app.models.phase_b import BuildRun, PhaseBRun, UATTestRun

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[PhaseBRun.__table__, BuildRun.__table__,
                                             UATTestRun.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed(db):
    from app.models.base import utcnow
    from app.models.phase_b import (
        BuildRun, BuildRunStatus, PhaseBRun, PhaseBStep, TestRunStatus, UATTestRun,
    )

    run = PhaseBRun(change_request_id="chg-1", current_step=PhaseBStep.TEST_GEN)
    db.add(run)
    db.flush()
    orphan_build = BuildRun(phase_b_run_id=run.id, iteration_number=0,
                            status=BuildRunStatus.RUNNING, triggered_at=utcnow(),
                            build_log="partial output")
    done_build = BuildRun(phase_b_run_id=run.id, iteration_number=0,
                          status=BuildRunStatus.SUCCESS, triggered_at=utcnow())
    orphan_uat = UATTestRun(phase_b_run_id=run.id, suite_version=0, iteration_number=1,
                            status=TestRunStatus.RUNNING, started_at=utcnow())
    db.add_all([orphan_build, done_build, orphan_uat])
    db.commit()
    return run, orphan_build, done_build, orphan_uat


def test_sweep_fails_orphans_and_advances_uat_to_triage(db_session):
    from app.models.phase_b import BuildRunStatus, PhaseBStep, TestRunStatus

    run, orphan_build, done_build, orphan_uat = _seed(db_session)
    assert sweep_orphan_script_runs(db_session) == 2

    assert orphan_build.status == BuildRunStatus.FAILURE
    assert orphan_build.completed_at is not None
    assert "restarted" in orphan_build.build_log
    assert "partial output" in orphan_build.build_log   # existing log preserved

    assert orphan_uat.status == TestRunStatus.COMPLETED
    assert orphan_uat.failed == 1 and orphan_uat.total == 1
    assert run.current_step == PhaseBStep.TRIAGE, \
        "a swept UAT failure flows forward to triage like any other failure"

    assert done_build.status == BuildRunStatus.SUCCESS   # terminal rows untouched


def test_sweep_is_idempotent(db_session):
    _seed(db_session)
    assert sweep_orphan_script_runs(db_session) == 2
    assert sweep_orphan_script_runs(db_session) == 0


def test_sweep_noop_on_empty_db(db_session):
    assert sweep_orphan_script_runs(db_session) == 0
