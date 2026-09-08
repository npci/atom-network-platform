# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""The integration-testing tunnel ingress — an H3 interface.

H3 = externally reachable and hostile (security skill §4): a Simulator points
a real HTTP client at this route and everything it sends is carried to the far
platform. So the tightest posture applies — off by default, size-capped in the
agent (not only at nginx), aggressive timeouts, strict rejection.

**Authorization.** v1 deliberately had none, on the reasoning that the tunnel is
dev-only and that the control that matters is on the RECEIVING side — the far
platform resolves the alias against its own allowlist and refuses anything else.
That argument covers *which targets* are reachable; it does not cover *who* may
drive the tunnel. Anyone who could reach this API could use the platform as a
relay into partner networks, carrying the platform's identity, on a route that
recorded no caller — and the exchange rows are attributed to `partner_id`, so an
anonymous caller wrote history against a partner.

It now requires an authenticated operator (`AdminUser`) — the level the
`/exchanges` listing beside it already demanded, the catch-all being the single
unauthenticated route in the module. `integration_testing_enabled` plus the
production startup assertion remain the primary control; this is the second one,
so a tunnel mistakenly enabled outside production is not open to the network.

The route is a catch-all so the tunnel is transparent: whatever method, path,
query and headers the Simulator sends are what the target sees.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request, Response

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
    # The far egress protecting its target — transient, retry later.
    "bulkhead_saturated": 503,
    "circuit_open": 503,
}


@router.get("/exchanges")
async def list_exchanges(db: DbDep, user: "AdminUser", limit: int = Query(50, ge=1, le=200)) -> dict:
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
    _: AdminUser,
) -> Response:
    """Carry one HTTP exchange to `partner_id`, addressed to `alias`.

    `alias` is a NAME, not a URL, and this side never resolves it — the far
    platform does, against its own allowlist.
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
        # "certified against baseline".
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
        # append, not assign: repeats must survive.
        #
        # Set-Cookie is the exception and is dropped. The far side is an
        # arbitrary tunnel target; a cookie it sets would be attached to THIS
        # platform's origin, which is a session-fixation primitive and not
        # something any legitimate tunnel client consumes.
        if name.lower() == "set-cookie":
            continue
        out.headers.append(name, value)

    # Neutralise the body as an active-content vector. The response is attacker-
    # influenced (a hostile or compromised target chooses its Content-Type) yet
    # is returned from the platform's own origin, so an HTML+script reply would
    # execute here. The tunnel's clients are Simulators reading bytes, not
    # browsers rendering them, so forcing an opaque download costs nothing.
    out.headers["Content-Type"] = "application/octet-stream"
    out.headers["X-Content-Type-Options"] = "nosniff"
    out.headers["Content-Disposition"] = "attachment"
    out.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    out.headers["X-Tunnel-Content-Type"] = str(
        response.headers.get("content-type") or "")[:200]

    out.headers["X-Tunnel-Exchange"] = result.exchange_id
    return out
