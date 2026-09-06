# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The integration-testing tunnel ingress — an H3 interface (ITA I-1).

H3 = externally reachable and hostile (security skill §4): a Simulator points
a real HTTP client at this route and everything it sends is carried to the far
platform. So the tightest posture applies — off by default, size-capped in the
agent (not only at nginx), aggressive timeouts, strict rejection.

**Why there is no per-caller authorization in v1, and why that is a decision
rather than an omission:** the tunnel is confirmed dev-only, and the control
that matters is on the RECEIVING side — the far platform resolves the alias
against its own allowlist and refuses anything else. Adding per-partner authz
here would not change what a caller can reach. If this ever ships beyond dev,
that assumption breaks and this route needs authentication before anything
else.

The route is a catch-all so the tunnel is transparent: whatever method, path,
query and headers the Simulator sends are what the target sees.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app.core.config import settings
from app.core.deps import AdminUser, DbDep
from app.models.phase_c import PartnerAgent
from app.services.integration_testing.ingress import forward_exchange

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integration-testing", tags=["integration-testing"])

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Mapped so a tunnel failure reaches the caller as a plausible HTTP status
# rather than a blanket 500 — the Simulator asserts on `X-Tunnel-Error` for the
# precise code.
_STATUS_FOR = {
    "tunnel_disabled": 503,
    "unknown_alias": 404,
    "path_not_allowed": 403,
    "payload_too_large": 413,
    "target_timeout": 504,
    "target_unreachable": 502,
    "hop_limit_exceeded": 508,
    "digest_mismatch": 502,
    "malformed_exchange": 400,
    # ITA-5: the far egress protecting its target — transient, retry later.
    "bulkhead_saturated": 503,
    "circuit_open": 503,
}


@router.get("/exchanges")
async def list_exchanges(db: DbDep, user: "AdminUser", limit: int = 50) -> dict:
    """The I-9 admin view: recent tunnelled exchanges, newest first — each row
    diagnosable without logs. Registered BEFORE the catch-all so the literal
    path wins; admin-authed, unlike the tunnel itself (which is dev-only and
    deliberately unauthenticated — see the module docstring)."""
    from app.models.integration_exchange import IntegrationExchange

    rows = (db.query(IntegrationExchange)
            .order_by(IntegrationExchange.created_at.desc())
            .limit(max(1, min(int(limit), 200))).all())
    return {"exchanges": [
        {
            "exchange_id": r.exchange_id, "direction": r.direction,
            "alias": r.alias, "method": r.method, "path": r.path,
            "status": r.status, "error_code": r.error_code,
            "request_bytes": r.request_bytes, "response_bytes": r.response_bytes,
            "elapsed_ms": r.elapsed_ms, "dropped_headers": r.dropped_headers,
            "correlation_id": r.correlation_id, "cert_context": r.cert_context,
            "at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]}


@router.api_route("/{partner_id}/{alias}/{target_path:path}", methods=_METHODS)
async def tunnel_exchange(
    partner_id: str,
    alias: str,
    target_path: str,
    request: Request,
    db: DbDep,
) -> Response:
    """Carry one HTTP exchange to `partner_id`, addressed to `alias`.

    `alias` is a NAME, not a URL, and this side never resolves it — the far
    platform does, against its own allowlist (ITA §2).
    """
    if not settings.integration_testing_enabled:
        return Response(status_code=503, content=b"integration testing tunnel is disabled",
                        headers={"X-Tunnel-Error": "tunnel_disabled"})

    # PartnerAgent has no slug column; the id is the stable handle, and `name`
    # is accepted as a convenience for hand-driven testing.
    partner = db.query(PartnerAgent).filter(PartnerAgent.id == partner_id).first()
    if partner is None:
        partner = db.query(PartnerAgent).filter(PartnerAgent.name == partner_id).first()
    if partner is None:
        return Response(status_code=404, content=b"unknown partner",
                        headers={"X-Tunnel-Error": "unknown_partner"})

    body = await request.body()
    if len(body) > settings.integration_testing_max_body_bytes:
        # Enforced HERE and not only at nginx: §16 — no gateway-only security.
        return Response(status_code=413, content=b"request body too large",
                        headers={"X-Tunnel-Error": "payload_too_large"})

    result = await forward_exchange(
        db=db,
        partner=partner,
        alias=alias,
        method=request.method,
        # Rebuilt with the leading slash the target expects; the path segment
        # arrives without one.
        path="/" + (target_path or ""),
        # VERBATIM. `request.url.query` is the raw string Starlette parsed off
        # the request line — not a re-encoding of parsed parameters. Contract
        # selection rides on `?pack=`, so normalising here would present as
        # "certified against baseline" (ITA §12.5).
        query=request.url.query or "",
        # `.raw` preserves repeats and original casing; `.items()` would not.
        headers=[(k.decode("latin-1"), v.decode("latin-1"))
                 for k, v in request.headers.raw],
        body=body,
    )

    if result.failed:
        code = str(result.error.get("code") or "target_unreachable")
        detail = str(result.error.get("detail") or "")
        return Response(
            status_code=_STATUS_FOR.get(code, 502),
            content=detail.encode("utf-8"),
            headers={"X-Tunnel-Error": code, "X-Tunnel-Exchange": result.exchange_id},
        )

    response = result.response
    # Hop-by-hop and length headers are dropped on the way back for the same
    # reason as on the way out: they described the far connection. Starlette
    # recomputes Content-Length for the body we return.
    from app.a2a_common.integration_contract import classify_headers

    forwarded, _dropped = classify_headers(response.headers)
    out = Response(status_code=response.status, content=response.body)
    for name, value in forwarded:
        # append, not assign: repeats such as Set-Cookie must survive.
        out.headers.append(name, value)
    out.headers["X-Tunnel-Exchange"] = result.exchange_id
    return out
