# Copyright 2026 The ATOM Authors
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

    # FAIL CLOSED when the token is unset.
    #
    # This used to read `if expected and not compare_digest(...)`, so an empty
    # `cert_agent_internal_token` — which is its DEFAULT ("") — short-circuited
    # the whole check and left POST /sim/execute open to any anonymous caller.
    # The config validator that would have caught it only raises when
    # `app_env == "production"`, and startup_validation merely warns, so every
    # UAT/staging deployment that never set the token ran this endpoint
    # unauthenticated. An unset credential is a misconfiguration, not a
    # licence to skip authentication.
    if not expected:
        logger.error(
            "cert_agent_internal_token is not set — refusing internal simulator "
            "requests. Set CERT_AGENT_INTERNAL_TOKEN (and the matching "
            "CERTSIM_INTERNAL_TOKEN) to enable this endpoint.")
        raise HTTPException(
            status_code=503,
            detail="internal simulator endpoint is not configured")

    # compare_digest, not `!=` — every other secret comparison in this tree is
    # timing-safe (hmac_signer.py, auth.py); this one was the outlier.
    if not secrets.compare_digest(
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
