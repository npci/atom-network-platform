# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Run a certification scope through the engine and normalise the outcome.

Thin orchestration over provisioning + connector: provision the bank, resolve the cases in the
scope, run each against the real switch, and return a plain dict in the SAME shape the platform's
cert_orchestrator already consumes from the (soon-retired) cert-agent REST path:

    {run_id, passed, failed, elapsed_ms,
     results: [{test_case_id, status, expected_resp_code, actual_resp_code, txn_id, ...}]}

That shape-compatibility is the whole point — it lets the orchestrator swap its run step for this
with a one-line branch, and keep its persistence + outbound A2A untouched. Still transport- and
framework-agnostic: no A2A, no ORM, no HTTP framework here.
"""
from __future__ import annotations

import uuid

from .connector import CaseSpec, NfiniteConnector, SimulatorRejected, Unverifiable
from .provisioning import BankConfig, NfiniteProvisioner
from .verdict import Verdict


def _status(v: Verdict) -> str:
    return {Verdict.PASSED: "PASS", Verdict.FAILED: "FAIL"}.get(v, "SKIP")


def run_certification(
    bank: BankConfig,
    *,
    cert_name: str,
    subset: str,
    certgroup: str,
    cases: list[str] | None = None,
    precert_url: str = "https://localhost:8090",
    db: dict | None = None,
    provision: bool = True,
    timeout_s: float = 20.0,
) -> dict:
    """Provision `bank`, run `cases` (or every case mapped to `subset`) and return a run_data dict.

    A single case that can't be graded (no expected result), that precert refuses, or that errors
    is recorded as SKIP/ERROR and does NOT abort the run — matching the connector's real-scope
    robustness. `provision=False` skips the DB writes (for a re-run against an already-set-up bank).
    """
    prov = NfiniteProvisioner(db)
    if provision:
        prov.configure_bank(bank)
        if cases:
            prov.assign_scope(bank, cert_name=cert_name, subset=subset, cases=cases)

    conn = NfiniteConnector(precert_url=precert_url, db=db)
    scope = [(tc, certgroup) for tc in cases] if cases else conn.cases_in_subset(subset)

    results: list[dict] = []
    passed = failed = 0
    for tc, cg in scope:
        row: dict = {"test_case_id": tc, "certgroup": cg}
        try:
            r = conn.run_case(CaseSpec(bank.psp_org_id, subset, cert_name, tc, cg), timeout_s=timeout_s)
            status = _status(r.verdict)
            row.update({
                "status": status,
                "expected_resp_code": r.expected.code,
                "actual_resp_code": r.observed.code,
                "txn_id": r.txnid,
                "precert_review": r.precert_review,
            })
            if status == "PASS":
                passed += 1
            elif status == "FAIL":
                failed += 1
        except Unverifiable as exc:
            row.update({"status": "SKIP", "error_message": f"unverifiable: {exc}"})
        except SimulatorRejected as exc:
            row.update({"status": "SKIP", "error_message": f"unsupported: {exc}"})
        except Exception as exc:  # noqa: BLE001 — one bad case must not abort the whole run
            row.update({"status": "ERROR", "error_message": str(exc)})
        results.append(row)

    return {
        "run_id": "ENG-" + uuid.uuid4().hex[:8].upper(),
        "passed": passed,
        "failed": failed,
        "elapsed_ms": 0,
        "results": results,
    }
