#!/usr/bin/env python
"""Dispatch ONE certification round for a (change, partner) pair.

WHY THIS SCRIPT EXISTS. `services/certification_dispatch.run_certification` is
the harness-agnostic seam every certification goes through, and it has exactly
three callers — all A2A-inbound (readiness declaration, the lifecycle
`ready_for_certification` transition, and the auto-loop on a fix notification).
There is deliberately no operator HTTP endpoint onto it: a round is something a
PARTNER's readiness triggers, not something the authority pushes.

That is right for production and awkward for testing, because it means an
operator cannot start a round without a partner that declares readiness. This
script is the operator-side equivalent: it calls the same seam with the same
arguments the executor would pass, so what runs is the real dispatch — the real
harness, the real pack build, the real A2A announcement to the partner — and
not a test double.

    ./scripts/dispatch_certification.py <change_id> <partner_id> [--role ROLE]
                                        [--advance] [--modes npci=..,partner=..]

`--advance` walks the assignment to CERTIFYING first (received -> accepted ->
applied -> tested -> ready_for_certification -> certifying) through the real
status setter, for a change whose partner has not driven that itself.

Env: the same DOMAIN_PACK / DATABASE_URL / REDIS_URL the service runs with.
Run it from `backend/` with PYTHONPATH=. so `app` resolves.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("change_id")
    ap.add_argument("partner_id")
    ap.add_argument("--role", default="",
                    help="the role the partner certifies for (scopes the case "
                         "set to that actor's workbook sheet), e.g. "
                         "LENDING_LIBRARY / REMITTER_BANK")
    ap.add_argument("--advance", action="store_true",
                    help="walk the assignment to CERTIFYING first")
    ap.add_argument("--modes", default="",
                    help="npci=simulator|application,partner=simulator|application")
    ap.add_argument("--harness", default="",
                    help="override the harness for this dispatch (sim_pack | "
                         "precert | cert_agent); default = what the pack declares")
    args = ap.parse_args()

    from app.core.database import SessionLocal
    from app.models.phase_c import AssignmentStatus, ChangePartnerAssignment
    from app.services.assignment_status import set_status
    from app.services.certification_dispatch import run_certification

    db = SessionLocal()
    try:
        if args.advance:
            assignment = (
                db.query(ChangePartnerAssignment)
                .filter(ChangePartnerAssignment.change_request_id == args.change_id,
                        ChangePartnerAssignment.partner_id == args.partner_id)
                .one_or_none())
            if assignment is None:
                print(f"no assignment for change={args.change_id} "
                      f"partner={args.partner_id} — assign the partner first "
                      f"(POST /api/changes/{{id}}/partners)", file=sys.stderr)
                return 2
            for target in (AssignmentStatus.ACCEPTED, AssignmentStatus.APPLIED,
                           AssignmentStatus.TESTED,
                           AssignmentStatus.READY_FOR_CERTIFICATION,
                           AssignmentStatus.CERTIFYING):
                if assignment.status != target:
                    set_status(assignment, target, db,
                               actor_partner_id=args.partner_id,
                               reason="operator dispatch (dispatch_certification.py)")
            db.commit()
            print(f"assignment -> {assignment.status.value}")

        test_data: dict = {}
        if args.modes:
            modes = dict(p.split("=", 1) for p in args.modes.split(",") if "=" in p)
            test_data["modes"] = {f"{k}_mode": v for k, v in modes.items()}

        meta = {"dispatched_by": "operator"}
        if args.harness:
            meta["harness"] = args.harness

        result = asyncio.run(run_certification(
            args.change_id, args.partner_id, role=args.role,
            test_data=test_data, dispatch_meta=meta))
    finally:
        db.close()

    if result is None:
        print("certification SKIPPED — the active domain declares no "
              "certification harness (add `certification_harness:` to the pack, "
              "or check DOMAIN_PACK)", file=sys.stderr)
        return 1

    details = result.details or {}
    print(json.dumps(details, indent=2, default=str))

    status = details.get("status")
    if status == "awaiting_partner":
        print(f"\nRound {details.get('run_number')} dispatched. "
              f"{len(details.get('awaiting_partner_cases') or [])} case(s) are "
              f"the partner's to execute and report; the run stays RUNNING until "
              f"their reports arrive or the suite deadline "
              f"(cert_suite_deadline_s) expires. It is NOT certified yet.")
    elif details.get("skipped"):
        print(f"\nRefused: {details.get('error')} — {details.get('detail')}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
