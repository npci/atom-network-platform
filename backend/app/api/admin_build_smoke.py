# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin Build Host API — verify `build_and_deploy.sh` without a change request.

Backs Admin → Build Host. The real Build step is buried inside a change at
Phase B BUILD behind the governance gate; this exposes the same runner wiring
standalone so an operator can answer "does the build host work?" on a new
environment before any change exists.

Preflight is synchronous (seconds). A full build is a background task the UI
polls, because it runs for minutes and must survive the browser navigating away.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.deps import AdminUser
from app.services import build_smoke

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/build-smoke", tags=["admin-build-smoke"])


class FullRunRequest(BaseModel):
    core_branch: str = "master"
    app_branch: str = "master"
    timeout_seconds: float = 7200.0


@router.get("")
def get_config(_: AdminUser):
    """Resolved runner wiring + whether it can work, without running anything."""
    return {
        "config": build_smoke.resolve_config(),
        "recent": [
            {
                "id": r.id,
                "kind": r.kind,
                "status": r.status,
                "started_at": r.started_at.isoformat(),
                "elapsed_seconds": round(r.elapsed, 1),
                "exit_code": r.exit_code,
            }
            for r in build_smoke.recent_runs()
        ],
    }


@router.post("/preflight")
async def run_preflight(_: AdminUser):
    """Fast probe: reachable? script present? git/mvn/java on PATH?"""
    cfg = build_smoke.resolve_config()
    if not cfg["ready"]:
        return {"status": "blocked", "blocker": cfg["blocker"], "config": cfg}
    run = await build_smoke.run_preflight()
    return {"status": run.status, "config": cfg, "run": run.to_dict()}


@router.post("/run")
async def start_full_run(body: FullRunRequest | None, _: AdminUser):
    """Start the REAL clone + build + deploy in the background. Poll /run/{id}."""
    body = body or FullRunRequest()
    cfg = build_smoke.resolve_config()
    if not cfg["ready"]:
        return {"status": "blocked", "blocker": cfg["blocker"], "config": cfg}

    active = [r for r in build_smoke.recent_runs() if r.kind == "full" and r.status == "running"]
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"A build smoke run is already in progress ({active[0].id}).",
        )

    run = await build_smoke.start_background_run(
        body.core_branch, body.app_branch, timeout=body.timeout_seconds,
    )
    return {"status": "started", "config": cfg, "run": run.to_dict()}


@router.get("/run/{run_id}")
def poll_run(run_id: str, _: AdminUser, since: int = 0):
    """Poll a run. *since* is the caller's last `next_index` — only new lines come back."""
    run = build_smoke.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown or expired smoke run")
    return run.to_dict(since=since)
