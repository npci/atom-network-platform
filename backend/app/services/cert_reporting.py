# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Certification reporting (CERT-7) — round history, per-round diff, coverage.

Read-only over what the framework already persists: `cert_runs` (+ the
per-round `coverage` note stamped at build time), `cert_test_results` and
`cert_flow_states`. Harness-agnostic and domain-neutral; every name here is
Part B vocabulary (the recorded decision: new surfaces use Part B only, the
older Phase C set is not spoken here).

THE COLUMN THAT EARNS ITS KEEP: **newly failing**. A fix that repairs two
cases and breaks a third is the failure mode the loop can otherwise hide for
several rounds — the no-progress guard sees a SHRINKING set and keeps
dispatching. The diff therefore refuses to flatten outcomes into one number:

  * `fixed`            — FAILED last round, PASSED this round. Only a PASS
                         counts as fixed; FAIL→SKIP is *no longer verified*.
  * `still_failing`    — FAILED both rounds.
  * `newly_failing`    — FAILING this round without having failed last round:
                         previously passing, previously skipped/errored, or
                         not in the previous round's scope at all. All three
                         are regressions of the claim "this round only
                         improved on the last one".
  * `no_longer_verified` — FAILED last round, SKIP/ERROR now: the defect did
                         not go away, it went unobserved.
  * `entered_scope` / `left_scope` — cases the rounds do not share; a
                         shrinking scope can masquerade as progress.

"Failing" means FAIL exactly — the same FAIL-only rule as the loop
(`cert_loop.failed_cases`); SKIP and ERROR are never failures, and never
successes either.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.phase_c import CertRun, CertTestStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = ["round_history", "round_diff", "flow_report"]


def _runs(db: "Session", cflow_id: str) -> list[CertRun]:
    return list(db.scalars(
        select(CertRun).where(CertRun.cflow_id == cflow_id)
        .order_by(CertRun.run_number)
    ))


def _statuses(run: CertRun) -> dict[str, CertTestStatus]:
    """case_id -> status. A case with several rows (variant executions) is
    FAIL if any row failed — the same worst-of rule the loop applies."""
    by_case: dict[str, CertTestStatus] = {}
    for r in run.results:
        tc = r.test_case_id or ""
        if by_case.get(tc) == CertTestStatus.FAIL:
            continue
        if r.status == CertTestStatus.FAIL or tc not in by_case:
            by_case[tc] = r.status
    return by_case


def _run_entry(run: CertRun) -> dict:
    return {
        "run_id": run.id,
        "run_number": run.run_number,
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),
        "dispatched_by": run.dispatched_by,
        "previous_run_id": run.previous_run_id,
        "total": run.total, "passed": run.passed,
        "failed": run.failed, "skipped": run.skipped,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "coverage": run.coverage,
    }


def round_history(db: "Session", cflow_id: str) -> list[dict]:
    """Every round of one certification flow, oldest first, with its coverage
    note exactly as built."""
    return [_run_entry(run) for run in _runs(db, cflow_id)]


def round_diff(db: "Session", cflow_id: str, run_number: int) -> dict:
    """This round against the one before it. Round 1 diffs against nothing:
    every failure is newly failing — which is the true statement."""
    runs = {run.run_number: run for run in _runs(db, cflow_id)}
    current = runs.get(run_number)
    if current is None:
        return {"error": f"no round {run_number} for {cflow_id}"}
    previous = runs.get(run_number - 1)

    now = _statuses(current)
    before = _statuses(previous) if previous is not None else {}

    failing_now = {tc for tc, st in now.items() if st == CertTestStatus.FAIL}
    failing_before = {tc for tc, st in before.items() if st == CertTestStatus.FAIL}
    shared = set(now) & set(before)

    return {
        "cflow_id": cflow_id,
        "run_number": run_number,
        "previous_run_number": previous.run_number if previous is not None else None,
        "fixed": sorted(tc for tc in failing_before & set(now)
                        if now[tc] == CertTestStatus.PASS),
        "still_failing": sorted(failing_before & failing_now),
        "newly_failing": sorted(failing_now - failing_before),
        "no_longer_verified": sorted(
            tc for tc in failing_before & set(now)
            if now[tc] in (CertTestStatus.SKIP, CertTestStatus.ERROR)),
        "entered_scope": sorted(set(now) - set(before)) if previous is not None else [],
        "left_scope": sorted(set(before) - set(now)),
        "unchanged_passing": sorted(
            tc for tc in shared
            if now[tc] == CertTestStatus.PASS and before[tc] == CertTestStatus.PASS),
    }


def flow_report(db: "Session", cflow_id: str) -> dict:
    """The whole story of one flow: rounds, per-round diffs, and the persisted
    lifecycle state (phase, halt reason, the audited transition history)."""
    history = round_history(db, cflow_id)
    diffs = [round_diff(db, cflow_id, entry["run_number"]) for entry in history]

    from app.services.cert_agent import flow_store

    flow_row = flow_store.load(db, cflow_id)
    flow = None
    if flow_row is not None:
        flow = {
            "phase": flow_row.phase,
            "current_round": flow_row.current_round,
            "halted_reason": flow_row.halted_reason,
            "transitions": flow_row.history or [],
        }
    return {"cflow_id": cflow_id, "rounds": history, "diffs": diffs, "flow": flow}
