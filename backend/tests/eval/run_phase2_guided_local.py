# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Guided local Phase 2 gate test (advisory -> soft_gate -> hard_gate).

Run inside backend container:
  docker compose exec backend python tests/eval/run_phase2_guided_local.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request

from app.core.database import SessionLocal
from app.models.brd import BRD
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.tech_spec import TechSpec
from app.services.evaluation.checkpoints import CheckpointId, PolicyMode, VerdictValue
from app.services.evaluation.contracts import get_contract
from app.services.evaluation.policy import set_policy_mode
from app.services.evaluation.runner import DETERMINISTIC_VERSION, run_advisory
from app.services.evaluation.schemas import EvalVerdict
from app.services.evaluation.store import get_latest, save_verdict

API_BASE = "http://127.0.0.1:8000/api"

GOOD_BRD = """## Background
The network change for local eval harness test.

## Functional Requirements
- FR-01: Validate payer VPA format before transaction routing.
- FR-02: Return standardized error codes on invalid VPI.

## Compliance
RBI and the Authority compliance notes included.
"""

BAD_TECH_SPEC = """## Overview
This tech spec is intentionally incomplete for gate testing.
"""


def _request(method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    url = f"{API_BASE}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {"raw": raw}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def _print_step(title: str) -> None:
    print(f"\n=== {title} ===")


def _login() -> str:
    status, payload = _request(
        "POST",
        "/auth/login",
        {"username": "admin", "password": "Admin@1234"},
    )
    if status != 200:
        raise RuntimeError(f"Login failed ({status}): {payload}")
    return payload["access_token"]


def _create_change(token: str, title: str) -> str:
    status, created = _request(
        "POST",
        "/changes",
        {"title": title, "initial_prompt": "Phase 2 guided gate validation"},
        token=token,
    )
    if status != 201:
        raise RuntimeError(f"Create change failed ({status}): {created}")
    return created["id"]


def _seed_tech_spec_gate(change_id: str, tech_spec_content: str) -> None:
    db = SessionLocal()
    try:
        cr = db.get(ChangeRequest, change_id)
        if not cr:
            raise RuntimeError(f"Change {change_id} not found")
        cr.status = ChangeStatus.TECH_SPEC
        db.add(BRD(change_request_id=change_id, content=GOOD_BRD, version=1))
        db.add(
            TechSpec(
                change_request_id=change_id,
                content=tech_spec_content,
                version=1,
            )
        )
        db.commit()
    finally:
        db.close()


def _set_policy(mode: PolicyMode) -> None:
    db = SessionLocal()
    try:
        set_policy_mode(db, CheckpointId.BRD_TO_TECH_SPEC, mode)
    finally:
        db.close()


async def _run_eval(change_id: str, tech_spec_content: str) -> dict:
    db = SessionLocal()
    try:
        await run_advisory(
            db=db,
            change_request_id=change_id,
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": {"type": "brd", "content": GOOD_BRD}},
            target_artifacts={
                "tech_spec_document": {"type": "tech_spec", "content": tech_spec_content},
            },
        )
        latest = get_latest(db, change_id, CheckpointId.BRD_TO_TECH_SPEC)
        return {
            "latest_verdict": getattr(latest, "verdict", None) if latest else None,
            "latest_id": getattr(latest, "id", None) if latest else None,
            "latest_passed": getattr(latest, "passed", None) if latest else None,
        }
    finally:
        db.close()


def _inject_warn_verdict(change_id: str) -> str:
    contract = get_contract(CheckpointId.BRD_TO_TECH_SPEC)
    db = SessionLocal()
    try:
        row = save_verdict(
            db,
            change_id,
            EvalVerdict(
                checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
                verdict=VerdictValue.WARN,
                passed=True,
                policy_mode=PolicyMode.SOFT_GATE,
                confidence=0.6,
                warn_codes=["GUIDED_TEST_WARN"],
                reasons=["Injected WARN for soft_gate acknowledgement demo."],
                rubric_version=contract.rubric_version,
                deterministic_version=DETERMINISTIC_VERSION,
            ),
        )
        if row is None:
            raise RuntimeError("Failed to save injected WARN verdict")
        return row.id
    finally:
        db.close()


def _advance(token: str, change_id: str, ack_id: str | None = None) -> tuple[int, dict]:
    body = {"eval_acknowledged_verdict_id": ack_id} if ack_id else {}
    return _request("POST", f"/changes/{change_id}/advance", body, token=token)


def _gate_detail(payload: dict) -> dict:
    detail = payload.get("detail")
    return detail if isinstance(detail, dict) else payload


def main() -> int:
    print("Phase 2 guided local test starting...")
    token = _login()
    print("login: ok")

    # 1) Advisory — real FAIL eval must not block advance
    _print_step("1) Advisory mode (FAIL eval still advances)")
    _set_policy(PolicyMode.ADVISORY)
    change_advisory = _create_change(token, "Phase2 Advisory Gate Test")
    _seed_tech_spec_gate(change_advisory, BAD_TECH_SPEC)
    eval1 = asyncio.run(_run_eval(change_advisory, BAD_TECH_SPEC))
    print("eval:", eval1)
    st, adv = _advance(token, change_advisory)
    print(f"advance status={st}", adv)
    if st != 200:
        print("FAIL: advisory advance should not block")
        return 1
    print("PASS: advisory advance allowed")

    # 2) Soft gate — WARN requires acknowledgement
    _print_step("2) Soft gate (WARN requires ack)")
    _set_policy(PolicyMode.SOFT_GATE)
    change_soft = _create_change(token, "Phase2 Soft Gate Test")
    _seed_tech_spec_gate(change_soft, BAD_TECH_SPEC)
    verdict_id = _inject_warn_verdict(change_soft)
    print(f"injected WARN verdict_id={verdict_id}")

    st_block, blocked = _advance(token, change_soft)
    gate = _gate_detail(blocked)
    print(f"advance without ack: status={st_block}", gate)
    if st_block != 409 or not gate.get("blocked"):
        print("FAIL: expected 409 blocked without ack")
        return 1
    if not gate.get("requires_ack"):
        print("FAIL: expected requires_ack in response")
        return 1
    print("PASS: soft_gate blocked without acknowledgement")

    st_ok, allowed = _advance(token, change_soft, ack_id=verdict_id)
    print(f"advance with ack: status={st_ok}", allowed)
    if st_ok != 200:
        print("FAIL: soft_gate advance with ack should pass")
        return 1
    print("PASS: soft_gate advance allowed after ack")

    # 3) Hard gate — FAIL blocks; override unblocks
    _print_step("3) Hard gate (FAIL blocks, override unblocks)")
    _set_policy(PolicyMode.HARD_GATE)
    change_hard = _create_change(token, "Phase2 Hard Gate Test")
    _seed_tech_spec_gate(change_hard, BAD_TECH_SPEC)
    eval3 = asyncio.run(_run_eval(change_hard, BAD_TECH_SPEC))
    print("eval:", eval3)
    if eval3["latest_verdict"] != "FAIL":
        print("FAIL: expected FAIL verdict for incomplete tech spec")
        return 1
    fail_verdict_id = eval3["latest_id"]

    st_fail, fail_payload = _advance(token, change_hard)
    gate_fail = _gate_detail(fail_payload)
    print(f"advance under FAIL: status={st_fail}", gate_fail)
    verdict_label = gate_fail.get("verdict")
    if st_fail != 409 or verdict_label != "FAIL":
        print("FAIL: expected hard_gate block on FAIL")
        return 1
    print("PASS: hard_gate blocked on FAIL")

    ov_status, ov_payload = _request(
        "POST",
        f"/changes/{change_hard}/eval/override",
        {
            "checkpoint_id": CheckpointId.BRD_TO_TECH_SPEC.value,
            "reason": "Risk accepted for local guided test after manual review.",
            "previous_verdict_id": fail_verdict_id,
        },
        token=token,
    )
    print(f"override: status={ov_status}", ov_payload)
    if ov_status != 200:
        print("FAIL: override should succeed")
        return 1

    st_after, after = _advance(token, change_hard)
    print(f"advance after override: status={st_after}", after)
    if st_after != 200:
        print("FAIL: advance should pass after override")
        return 1
    print("PASS: advance allowed after override")

    print("\nAll guided Phase 2 checks passed.")
    print(f"change_ids: advisory={change_advisory} soft={change_soft} hard={change_hard}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
