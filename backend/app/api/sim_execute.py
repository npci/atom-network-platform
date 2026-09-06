# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The simulator's execute surface — the HTTP edge over `simulator/runtime`.

`POST /sim/execute?pack=<ref>&tc_id=&variant_id=` with the wire body. The
whole behaviour (§3.1 binding, validation, scenarios, the 504-for-
`no_response` choice) lives in `services/simulator/runtime.handle` — ONE
implementation, shared with the in-process sim_pack certification harness,
so the harness certifies against exactly what a partner's stack would hit.
Every response names the contract that produced it (`X-Sim-Pack`) and how it
was chosen (`X-Sim-Scenario`).

Auth: `X-Internal-Token` against `settings.cert_agent_internal_token` when
set; unset = the repo's dev/UAT posture (permit + startup warning owns the
complaint). Operator cookies never reach this path — partner stacks call it
through the tunnel.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request, Response

from app.core.config import settings
from app.core.deps import DbDep
from app.services.simulator import runtime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sim", tags=["simulator"])


def _check_token(request: Request) -> None:
    expected = settings.cert_agent_internal_token
    # compare_digest, not `!=` — every other secret comparison in this tree is
    # timing-safe (hmac_signer.py, auth.py); this one was the outlier.
    if expected and not secrets.compare_digest(
        request.headers.get("X-Internal-Token") or "", expected
    ):
        raise HTTPException(status_code=401, detail="invalid internal token")


@router.post("/execute")
async def execute(request: Request, db: DbDep,
                  pack: str | None = None,
                  tc_id: str | None = None,
                  variant_id: str | None = None) -> Response:
    _check_token(request)
    body = await request.body()
    try:
        reply = await runtime.handle(db, body=body, pack=pack, tc_id=tc_id,
                                     variant_id=variant_id)
    except runtime.SimRefusal as exc:
        headers = {"X-Sim-Pack": exc.pack_header} if exc.pack_header else None
        raise HTTPException(status_code=exc.status, detail=exc.payload,
                            headers=headers)
    return Response(content=reply.content, media_type=reply.media_type,
                    headers={"X-Sim-Pack": reply.pack_header,
                             "X-Sim-Scenario": reply.scenario})
