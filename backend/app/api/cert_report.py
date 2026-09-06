# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-7 — the certification reporting surface.

Thin over `services/cert_reporting.py`: round history per cflow, per-round
diffs (the *newly failing* column is the point), the coverage note as built,
and the persisted lifecycle state. Part B vocabulary only.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.core.deps import CurrentUser, DbDep
from app.services import cert_reporting

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cert/flows", tags=["certification"])


@router.get("/{cflow_id}/report")
def cert_flow_report(cflow_id: str, db: DbDep, _: CurrentUser) -> dict:
    """The whole story of one certification flow."""
    report = cert_reporting.flow_report(db, cflow_id)
    if not report["rounds"] and report["flow"] is None:
        raise HTTPException(status_code=404, detail=f"unknown cflow {cflow_id!r}")
    return report


@router.get("/{cflow_id}/rounds/{run_number}/diff")
def cert_round_diff(cflow_id: str, run_number: int, db: DbDep, _: CurrentUser) -> dict:
    """This round against the one before it. `newly_failing` is the column a
    fix-that-broke-something hides in everywhere else."""
    diff = cert_reporting.round_diff(db, cflow_id, run_number)
    if "error" in diff:
        raise HTTPException(status_code=404, detail=diff["error"])
    return diff
