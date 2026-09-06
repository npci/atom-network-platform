# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Startup reconciliation for Phase B script runs (build + UAT).

The build and UAT scripts run as in-process asyncio tasks (api/phase_b.py
``_drive``), so — exactly like the agent-jobs registry's own startup sweep in
``main.py`` — any BuildRun still QUEUED/RUNNING or UATTestRun still RUNNING at
boot belonged to a process that no longer exists: the task died with it before
its failure handler could flip the row. Without this sweep the zombie row
blocks re-triggering (the one-run-at-a-time guard) until the staleness window
expires, and the UI shows a run as live that never finishes.

Same single-process assumption the agent-jobs sweep documents; flipping an
already-terminal row is a no-op by construction (status filters).
"""
from __future__ import annotations

import logging

from app.models.base import utcnow
from app.models.phase_b import (
    BuildRun, BuildRunStatus, PhaseBRun, PhaseBStep,
    TestRunStatus, UATTestRun,
)

logger = logging.getLogger(__name__)

_ORPHAN_NOTE = "\n[backend] backend restarted while this script was running — re-run it"


def sweep_orphan_script_runs(db) -> int:
    """Fail every Phase B script run orphaned by a dead process. Returns count."""
    flipped = 0

    for row in (db.query(BuildRun)
                .filter(BuildRun.status.in_((BuildRunStatus.QUEUED,
                                             BuildRunStatus.RUNNING))).all()):
        row.status = BuildRunStatus.FAILURE
        row.completed_at = utcnow()
        row.build_log = ((row.build_log or "") + _ORPHAN_NOTE).strip()
        flipped += 1

    for row in (db.query(UATTestRun)
                .filter(UATTestRun.status == TestRunStatus.RUNNING).all()):
        row.status = TestRunStatus.COMPLETED
        row.completed_at = utcnow()
        row.failed = row.failed or 1
        row.total = row.total or 1
        row.log = ((row.log or "") + _ORPHAN_NOTE).strip()
        # Failures flow forward: triage is the step that looks at them (same
        # rule as the runner's own crash handler).
        run = db.get(PhaseBRun, row.phase_b_run_id)
        if run is not None and run.current_step in (PhaseBStep.TEST_GEN,
                                                    PhaseBStep.TEST_EXEC):
            run.current_step = PhaseBStep.TRIAGE
        flipped += 1

    if flipped:
        db.commit()
    return flipped
